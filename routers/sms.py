from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional, List
from datetime import datetime, timezone
import uuid

from db import get_db
from models import (
    Message, SendSmsRequest, BulkSmsRequest,
    Template, TemplateCreate, TemplateUpdate, User,
)
from auth import get_current_user, require_admin
from mobilemessage import send_sms, render_template, normalise_au

router = APIRouter(prefix='/api', tags=['sms'])


def _strip(d):
    if d: d.pop('_id', None)
    return d


# ----- Messages -----
@router.get('/messages', response_model=List[Message])
async def list_messages(
    user: User = Depends(get_current_user),
    client_id: Optional[str] = None,
    limit: int = Query(200, le=1000),
):
    db = get_db()
    q = {}
    if client_id: q['client_id'] = client_id
    cursor = db.messages.find(q).sort('sent_at', -1).limit(limit)
    out = []
    async for d in cursor:
        out.append(Message(**_strip(d)))
    return out


async def _record_message(client_id: Optional[str], client_name: Optional[str], phone: str, body: str, user: User, result: dict) -> Message:
    db = get_db()
    msg = Message(
        client_id=client_id,
        client_name=client_name,
        phone=phone,
        body=body,
        direction='outbound',
        status=result.get('status', 'queued'),
        provider_message_id=result.get('message_id'),
        provider_response=result.get('response'),
        sent_by_id=user.id,
    )
    await db.messages.insert_one(msg.model_dump(mode='json'))
    if client_id:
        await db.clients.update_one(
            {'id': client_id},
            {'$set': {
                'last_contacted_at': datetime.now(timezone.utc),
                'contact_status': 'Awaiting Reply',
                'updated_at': datetime.now(timezone.utc),
            }},
        )
    return msg


@router.post('/messages/send', response_model=Message)
async def send_message(payload: SendSmsRequest, user: User = Depends(get_current_user)):
    db = get_db()
    client_doc = None
    phone = payload.phone
    client_name = None
    client_id = payload.client_id
    if client_id:
        client_doc = await db.clients.find_one({'id': client_id})
        if not client_doc:
            raise HTTPException(404, 'Client not found')
        phone = client_doc['phone']
        client_name = client_doc['name']
    if not phone:
        raise HTTPException(400, 'phone or client_id required')
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, 'body cannot be empty')
    result = await send_sms(phone, body)
    msg = await _record_message(client_id, client_name, phone, body, user, result)
    if not result.get('success'):
        # Surface provider error to client (FastAPI still returns 200 with status='error')
        msg.status = result.get('status', 'failed')
        msg.provider_response = result.get('response')
    return msg


@router.post('/messages/bulk')
async def send_bulk(payload: BulkSmsRequest, user: User = Depends(get_current_user)):
    db = get_db()
    sent, failed = [], []
    # Batch-fetch all clients in one query to avoid N+1
    client_docs = {}
    async for c in db.clients.find({'id': {'$in': payload.client_ids}}):
        client_docs[c['id']] = c
    for cid in payload.client_ids:
        c = client_docs.get(cid)
        if not c:
            failed.append({'client_id': cid, 'error': 'Not found'}); continue
        body = render_template(payload.body, {
            'name': c['name'].split(' ')[0],
            'vehicle': c.get('vehicle', ''),
            'date': c.get('delivery_date', ''),
            'rego': c.get('rego') or '',
            'agent': c.get('salesperson') or '',
        })
        result = await send_sms(c['phone'], body)
        msg = await _record_message(cid, c['name'], c['phone'], body, user, result)
        if result.get('success'):
            sent.append(msg.id)
        else:
            failed.append({'client_id': cid, 'error': result.get('error', result.get('status'))})
    return {'sent': len(sent), 'failed': failed, 'total': len(payload.client_ids)}


# ----- Templates -----
@router.get('/templates', response_model=List[Template])
async def list_templates(user: User = Depends(get_current_user)):
    db = get_db()
    rows = []
    async for d in db.templates.find().sort('name', 1):
        rows.append(Template(**_strip(d)))
    return rows


@router.post('/templates', response_model=Template)
async def create_template(payload: TemplateCreate, user: User = Depends(get_current_user)):
    db = get_db()
    tpl = Template(**payload.model_dump())
    await db.templates.insert_one(tpl.model_dump(mode='json'))
    return tpl


@router.patch('/templates/{tid}', response_model=Template)
async def update_template(tid: str, payload: TemplateUpdate, user: User = Depends(get_current_user)):
    db = get_db()
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update['updated_at'] = datetime.now(timezone.utc)
    res = await db.templates.find_one_and_update({'id': tid}, {'$set': update}, return_document=True)
    if not res:
        raise HTTPException(404, 'Template not found')
    return Template(**_strip(res))


@router.delete('/templates/{tid}')
async def delete_template(tid: str, user: User = Depends(get_current_user)):
    db = get_db()
    res = await db.templates.delete_one({'id': tid})
    return {'success': res.deleted_count > 0}


# ----- Inbound webhook (MobileMessage replies + delivery receipts) -----
@router.post('/messages/inbound')
async def mobilemessage_inbound(request: Request):
    """Public webhook endpoint hit by MobileMessage for inbound replies and delivery receipts.

    Inbound reply payload (typical):
      {"to": "BYDMELB", "from": "+61...", "message": "...", "received_at": "...",
       "type": "inbound", "original_message_id": "...", "original_custom_ref": "..."}

    Delivery receipt payload (typical):
      {"message_id": "...", "to": "+61...", "status": "delivered|failed|...",
       "custom_ref": "...", "received_at": "..."}
    """
    db = get_db()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, 'Invalid JSON')

    event_type = (payload.get('type') or '').lower()
    is_inbound = event_type == 'inbound' or bool(payload.get('message') and payload.get('from'))

    if is_inbound:
        from_phone = normalise_au(payload.get('from') or payload.get('sender') or '')
        body = payload.get('message') or ''
        # Try to attach to a known client by phone
        client_doc = None
        if from_phone:
            client_doc = await db.clients.find_one({'phone': from_phone})

        msg = Message(
            client_id=(client_doc or {}).get('id'),
            client_name=(client_doc or {}).get('name'),
            phone=from_phone or 'unknown',
            body=body,
            direction='inbound',
            status='received',
            provider='mobilemessage',
            provider_message_id=payload.get('original_message_id') or payload.get('message_id'),
            provider_response=payload,
        )
        await db.messages.insert_one(msg.model_dump(mode='json'))
        if client_doc:
            await db.clients.update_one(
                {'id': client_doc['id']},
                {'$set': {
                    'contact_status': 'Contacted',
                    'last_contacted_at': datetime.now(timezone.utc),
                    'updated_at': datetime.now(timezone.utc),
                }},
            )
        return {'success': True, 'event': 'inbound', 'recorded_id': msg.id}

    # Delivery receipt path
    message_id = payload.get('message_id')
    status_val = (payload.get('status') or 'unknown').lower()
    if message_id:
        await db.messages.update_one(
            {'provider_message_id': message_id},
            {'$set': {
                'status': status_val,
                'provider_response': payload,
            }},
        )
    return {'success': True, 'event': 'status', 'status': status_val}
