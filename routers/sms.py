from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from typing import Optional, List
from datetime import datetime, timezone
import uuid

from db import get_db, get_lead_db
from services.thread_history import history, assert_message_access, phone_key, allowed_site, site_key
from services.sms_event import phone_pattern, inbound_id, received_at, inbound_document_id
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
    limit: int = Query(200, ge=1, le=1000),
    site_location: Optional[str] = None,
    phone: Optional[str] = None,
):
    return [Message(**row) for row in await history(user, client_id, site_location, limit, phone)]



async def _record_message(client_id: Optional[str], client_name: Optional[str], phone: str, body: str, user: User, result: dict, site: Optional[str] = None) -> Message:
    db = get_db()
    client_doc = await db.clients.find_one({'id': client_id}) if client_id else None
    msg = Message(
        client_id=client_id,
        client_name=client_name,
        phone=phone,
        site=site or (client_doc or {}).get('site_location') or (client_doc or {}).get('site'),
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
    source_site = None
    if client_id:
        client_doc = await db.clients.find_one({'id': client_id})
        if not client_doc:
            raise HTTPException(404, 'Client not found')
        assert_message_access(user, client_doc)
        phone = client_doc['phone']
        client_name = client_doc['name']
    if not phone:
        raise HTTPException(400, 'phone or client_id required')
    if not client_doc:
        clients = await db.clients.find({}).to_list(None)
        client_doc = next((c for c in clients if phone_key(c.get('phone')) == phone_key(phone) and allowed_site(user, c.get('site_location') or c.get('site')) and (not payload.site or site_key(c.get('site_location') or c.get('site')) == site_key(payload.site))), None)
        if client_doc:
            client_id = client_doc['id']
            client_name = client_doc['name']
        else:
            leads = await get_lead_db().leads.find({'phone': {'$regex': phone_pattern(phone)}}).to_list(None)
            lead_doc = next((lead for lead in leads if allowed_site(user, lead.get('dealer')) and (not payload.site or site_key(lead.get('dealer')) == site_key(payload.site))), None)
            if not lead_doc:
                raise HTTPException(404, 'Recipient not found')
            source_site = lead_doc.get('dealer')
            client_name = lead_doc.get('name')
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, 'body cannot be empty')
    result = await send_sms(phone, body)
    msg = await _record_message(client_id, client_name, phone, body, user, result, source_site)
    if not result.get('success'):
        return JSONResponse(status_code=502, content={
            'detail': result.get('error') or 'SMS provider could not accept the message',
            'message': msg.model_dump(mode='json'),
        })
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
        try:
            assert_message_access(user, c)
        except HTTPException:
            failed.append({'client_id': cid, 'error': 'Client not found'}); continue
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
    is_inbound = event_type in ('inbound', 'unsubscribe') or bool((payload.get('message') or payload.get('text') or payload.get('body')) and (payload.get('from') or payload.get('sender') or payload.get('phone')))

    if is_inbound:
        from_phone = normalise_au(payload.get('from') or payload.get('sender') or payload.get('phone') or '')
        body = str(payload.get('message') or payload.get('body') or payload.get('text') or '').strip()
        if not from_phone or not body:
            raise HTTPException(400, 'Missing phone or message body')
        # Try to attach to a known client by phone
        client_doc = None
        if from_phone:
            client_doc = await db.clients.find_one({'phone': {'$regex': phone_pattern(from_phone)}})

        msg = Message(
            client_id=(client_doc or {}).get('id'),
            client_name=(client_doc or {}).get('name'),
            phone=from_phone or 'unknown',
            body=body,
            direction='inbound',
            status='received',
            provider='mobilemessage',
            provider_message_id=inbound_id(payload, from_phone, body),
            sent_at=received_at(payload),
            provider_response=payload,
        )
        record = msg.model_dump(mode='json')
        original_id = payload.get('original_message_id')
        original = await db.messages.find_one({'provider_message_id': original_id, 'direction': 'outbound'}) if original_id else None
        source = await get_lead_db().conversations.find_one({'messages.id': original_id}) if original_id else None
        record['site'] = (original or {}).get('site') or (source or {}).get('dealer') or (client_doc or {}).get('site_location') or (client_doc or {}).get('site') or ''
        if msg.provider_message_id:
            from bson import ObjectId
            await db.messages.update_one({'_id': ObjectId(inbound_document_id(msg.provider_message_id))}, {'$setOnInsert': record}, upsert=True)
        else:
            await db.messages.insert_one(record)
        if client_doc:
            await db.clients.update_one(
                {'id': client_doc['id']},
                {'$set': {
                    'contact_status': 'Contacted',
                    'last_contacted_at': datetime.now(timezone.utc),
                    'updated_at': datetime.now(timezone.utc),
                }},
            )
        return {'success': True, 'event': 'inbound', 'recorded_id': msg.provider_message_id or msg.id}

    # Delivery receipt path
    message_id = payload.get('message_id')
    status_val = (payload.get('status') or 'unknown').lower()
    if message_id:
        await db.messages.update_many(
            {'provider_message_id': message_id},
            {'$set': {
                'status': status_val,
                'provider_response': payload,
            }},
        )
    if message_id:
        await get_lead_db().conversations.update_many({'messages.id': message_id}, {'$set': {'messages.$[m].status': status_val}}, array_filters=[{'m.id': message_id}])
    return {'success': True, 'event': 'status', 'status': status_val}
