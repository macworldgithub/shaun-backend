from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException, Query, Request, status
from fastapi.responses import Response
from typing import Optional, List
from datetime import datetime, timezone, timedelta
import re
import secrets
import os
import uuid
import hashlib
import httpx
from io import BytesIO
from PIL import Image

from db import get_db
from models import (
    Client, ClientBase, ClientUpdate, Comment, CommentCreate,
    Accessory, AccessoryCreate, AccessoryUpdate, User,
    ClientDocument, ClientDocumentUpdate, DocumentType, OfferRecord, OfferCreate, OfferUpdate,
)
from auth import get_current_user, require_admin
from services.email import send_email
from services.storage import put_bytes, get_bytes, exists

router = APIRouter(prefix='/api/clients', tags=['clients'])


def _strip(doc):
    if doc:
        doc.pop('_id', None)
    return doc


async def _refresh_document_completeness(db, client_id: str):
    client = await db.clients.find_one({'id': client_id}, {'documents': 1})
    documents = client.get('documents', []) if client else []
    present = {doc.get('document_type') for doc in documents if doc.get('status') == 'complete' and doc.get('storage_path')}
    required = {'ATR signed', 'Licence front', 'Licence back'}
    completeness = 'Complete' if required.issubset(present) else 'Partial' if documents else 'Requested'
    await db.clients.update_one({'id': client_id}, {'$set': {'document_completeness': completeness, 'updated_at': datetime.now(timezone.utc)}})
    return completeness


def _validate_document_completion(document: dict, client_documents: List[dict]):
    if document.get('status') != 'complete':
        return
    if not document.get('storage_path'):
        raise HTTPException(400, 'A stored file is required before marking a document complete')
    document_type = document.get('document_type')
    if document_type == 'ATR signed' and (not document.get('signature_present') or document.get('date_left_blank') is False):
        raise HTTPException(400, 'ATR completion requires a signature and a blank registration date')
    if document_type == 'EFT form':
        required = ('bank_name', 'bsb', 'account_number', 'amount', 'reference')
        if any(not document.get(field) for field in required) or not document.get('signature_present'):
            raise HTTPException(400, 'EFT completion requires bank, amount, reference, and signature checks')
        if document.get('customer_present') is False and float(document.get('amount') or 0) > 500 and not document.get('bank_statement_received'):
            raise HTTPException(400, 'A bank statement is required for EFT amounts over $500 when the customer is not present')


def _validate_document_file(document_type: str, data: bytes, content_type: Optional[str]):
    if content_type not in {'application/pdf', 'image/jpeg', 'image/png'}:
        raise HTTPException(400, 'Only PDF, JPEG, and PNG documents are accepted')
    if document_type in ('Licence front', 'Licence back') and content_type.startswith('image/'):
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
        except Exception as exc:
            raise HTTPException(400, f'Licence image could not be read: {exc}')


async def _rescore_open_deals(db):
    async for client in db.clients.find({'stage': {'$ne': 'Delivered'}, 'linked_offer_ids': {'$exists': True, '$ne': []}}):
        offer = await db.offers.find_one({'id': {'$in': client.get('linked_offer_ids', [])}, 'active': True})
        if not offer:
            continue
        sale_type = client.get('sale_type', 'Retail')
        order_date = client.get('order_date') or client.get('delivery_date') or datetime.now(timezone.utc).date().isoformat()
        model_name = (client.get('vehicle') or '').lower()
        exclusions = [str(value).lower() for value in offer.get('sale_type_exclusions', [])]
        models = [str(value).lower() for value in offer.get('eligible_models', [])]
        model_match = not models or any(value in model_name or model_name in value for value in models)
        order_match = not offer.get('order_from') or offer.get('order_from') <= order_date <= (offer.get('order_to') or '9999-12-31')
        delivery_match = not offer.get('deliver_by') or not client.get('delivery_date') or client['delivery_date'] <= offer['deliver_by']
        status_value, reason = 'Eligible', None
        if sale_type.lower() in exclusions:
            status_value, reason = 'Ineligible', f'{sale_type} sale excluded from {offer.get("name", "offer")}'
        elif not model_match or not order_match:
            status_value, reason = 'Ineligible', 'Model or order window does not match offer'
        elif not delivery_match:
            status_value, reason = 'At risk', 'Delivery deadline is after offer deadline'
        await db.clients.update_one({'id': client['id']}, {'$set': {'offer_status': status_value, 'offer_reason': reason, 'updated_at': datetime.now(timezone.utc)}})


async def _validate_offer_selection(db, offer_ids: List[str]):
    if not offer_ids:
        return
    offers = await db.offers.find({'id': {'$in': offer_ids}, 'active': True}).to_list(length=50)
    if len(offers) != len(set(offer_ids)):
        raise HTTPException(400, 'One or more selected offers are not active')
    if len(offers) > 1 and any(not offer.get('combinable', True) for offer in offers):
        raise HTTPException(400, 'Selected offers cannot be combined')


def _validate_sale_type(sale_type: str, fleet_company: Optional[str]):
    if sale_type in ('Novated lease', 'Fleet') and not fleet_company:
        raise HTTPException(400, 'Fleet / FMO company is required for Fleet and Novated lease deals')


def _make_pdf(lines: List[str]) -> bytes:
    def pdf_text(value: str) -> str:
        return value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

    content = ['BT', '/F1 16 Tf', '48 800 Td']
    for index, line in enumerate(lines):
        if index == 1:
            content.append('/F1 10 Tf')
        content.append(f'({pdf_text(line)}) Tj')
        content.append('0 -16 Td')
    content.append('ET')
    stream = '\n'.join(content).encode('latin-1', errors='replace')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
    ]
    output = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n'.encode())
        output.extend(obj)
        output.extend(b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(objects) + 1}\n'.encode())
    output.extend(b'0000000000 65535 f \n')
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def _has_signed_handover(client: dict) -> bool:
    return client.get('handover_checklist_status') == 'Signed copy on file' and any(
        document.get('document_type') == 'Handover checklist' and document.get('status') == 'complete'
        for document in client.get('documents', [])
    )


@router.get('', response_model=List[Client])
async def list_clients(
    user: User = Depends(get_current_user),
    stage: Optional[str] = None,
    contact_status: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
    arrived: Optional[bool] = None,
    unassigned: Optional[bool] = None,
    mine: Optional[bool] = None,
    search: Optional[str] = None,
):
    db = get_db()
    q: dict = {}
    if stage:
        q['stage'] = stage
    if contact_status:
        q['contact_status'] = contact_status
    if arrived is not None:
        q['arrived'] = arrived
    if unassigned:
        q['$or'] = [{'assigned_agent_id': None}, {'assigned_agent_id': ''}]
    if mine:
        q['assigned_agent_id'] = user.id
    elif assigned_agent_id:
        q['assigned_agent_id'] = assigned_agent_id
    if search:
        regex = {'$regex': search, '$options': 'i'}
        q['$or'] = (q.get('$or', [])) + [
            {'name': regex}, {'phone': regex}, {'vehicle': regex},
            {'rego': regex}, {'vy_order_id': regex}, {'email': regex},
        ]
    cursor = db.clients.find(q).sort([('delivery_date', 1), ('created_at', -1)]).limit(500)
    rows = []
    async for doc in cursor:
        rows.append(Client(**_strip(doc)))
    return rows


@router.get('/alerts')
async def client_alerts(user: User = Depends(get_current_user)):
    db = get_db()
    today = datetime.now(timezone.utc).date()
    alerts = []
    async for client in db.clients.find({'stage': {'$ne': 'Delivered'}}).limit(500):
        client_id = client.get('id')
        name = client.get('name', 'Client')
        valid_until = client.get('trade_in_valid_until')
        if valid_until:
            try:
                days = (datetime.strptime(valid_until, '%Y-%m-%d').date() - today).days
                if days <= 10:
                    alerts.append({'id': f'trade-{client_id}', 'type': 'trade-in', 'severity': 'danger' if days <= 0 else 'warning', 'client_id': client_id, 'message': f'{name}: trade-in valuation expires in {max(days, 0)} day(s)' if days >= 0 else f'{name}: trade-in valuation expired'})
            except ValueError:
                pass
        if client.get('document_completeness') in ('Requested', 'Partial'):
            alerts.append({'id': f'docs-{client_id}', 'type': 'documents', 'severity': 'warning', 'client_id': client_id, 'message': f'{name}: registration documents are {str(client.get("document_completeness")).lower()}'})
        for offer_id in client.get('linked_offer_ids', []):
            offer = await db.offers.find_one({'id': offer_id})
            if not offer:
                continue
            for field, label in (('order_to', 'order window'), ('deliver_by', 'delivery deadline')):
                try:
                    days = (datetime.strptime(offer[field], '%Y-%m-%d').date() - today).days
                except (KeyError, TypeError, ValueError):
                    continue
                if days in (14, 7, 3, 1) or days <= 0:
                    alerts.append({'id': f'offer-{client_id}-{offer_id}-{field}', 'type': 'offer', 'severity': 'danger' if days <= 0 else 'warning', 'client_id': client_id, 'message': f'{name}: {offer.get("name", "offer")} {label} is {"expired" if days <= 0 else f"closing in {days} day(s)"}'})
    return alerts


@router.get('/{client_id}', response_model=Client)
async def get_client(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    doc = await db.clients.find_one({'id': client_id})
    if not doc:
        raise HTTPException(404, 'Client not found')
    return Client(**_strip(doc))


@router.post('', response_model=Client, status_code=status.HTTP_201_CREATED)
async def create_client(payload: ClientBase, user: User = Depends(get_current_user)):
    db = get_db()
    _validate_sale_type(payload.sale_type, payload.fleet_company)
    if payload.activation_status in ('Ready', 'Submitted to BYD', 'Active') and not _has_signed_handover(payload.model_dump()):
        raise HTTPException(400, 'Activation cannot be set to Ready while handover checklist is not on file')
    if payload.vy_order_id:
        existing = await db.clients.find_one({'vy_order_id': payload.vy_order_id})
        if existing:
            raise HTTPException(409, 'Client with this VY order ID already exists')
    client = Client(**payload.model_dump())
    await db.clients.insert_one(client.model_dump(mode='json'))
    return client


@router.patch('/{client_id}', response_model=Client)
async def update_client(
    client_id: str, payload: ClientUpdate, user: User = Depends(get_current_user),
):
    db = get_db()
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None or k in ('assigned_agent_id', 'aftermarket_notes', 'address', 'location', 'email', 'salesperson')}
    existing = await db.clients.find_one({'id': client_id}, {'handover_checklist_status': 1, 'activation_ready': 1, 'sale_type': 1, 'fleet_company': 1, 'documents': 1, 'trade_in_valid_until': 1, 'your_way_selection': 1})
    if not existing:
        raise HTTPException(404, 'Client not found')
    _validate_sale_type(update.get('sale_type', existing.get('sale_type', 'Retail')), update.get('fleet_company', existing.get('fleet_company')))
    if 'linked_offer_ids' in update:
        await _validate_offer_selection(db, update['linked_offer_ids'])
    if update.get('your_way_selection') and existing.get('your_way_selection') and update['your_way_selection'] != existing['your_way_selection']:
        raise HTTPException(400, 'Your Way selection is locked once confirmed')
    if update.get('activation_ready') is True and not _has_signed_handover({**existing, **({'handover_checklist_status': update.get('handover_checklist_status')} if 'handover_checklist_status' in update else {})}):
        raise HTTPException(400, 'Activation cannot be set to Ready while handover checklist is not on file')
    if update.get('activation_status') in ('Ready', 'Submitted to BYD', 'Active') and not _has_signed_handover({**existing, **({'handover_checklist_status': update.get('handover_checklist_status')} if 'handover_checklist_status' in update else {})}):
        raise HTTPException(400, 'Activation cannot be set to Ready while handover checklist is not on file')
    if update.get('handover_checklist_status') != 'Signed copy on file' and existing.get('handover_checklist_status') == 'Signed copy on file' and existing.get('activation_ready'):
        raise HTTPException(400, 'Handover checklist cannot be reverted while activation is ready')
    if update.get('activation_status') in ('Ready', 'Submitted to BYD', 'Active'):
        update['activation_ready'] = True
    elif update.get('activation_status') == 'Blocked':
        update['activation_ready'] = False
    if 'trade_in_sale_date' in update:
        try:
            sale_date = datetime.strptime(update['trade_in_sale_date'], '%Y-%m-%d').date()
            update['trade_in_valid_until'] = (sale_date + timedelta(days=30)).isoformat()
        except (TypeError, ValueError):
            raise HTTPException(400, 'Trade-in sale date must use YYYY-MM-DD')
    trade_in_valid_until = update.get('trade_in_valid_until', existing.get('trade_in_valid_until'))
    if update.get('trade_in_status') == 'Settled' and trade_in_valid_until and trade_in_valid_until < datetime.now(timezone.utc).date().isoformat():
        if not update.get('trade_in_manager_reason'):
            raise HTTPException(400, 'A manager reason is required to settle an expired trade-in')
        if user.role not in ('admin', 'super_admin'):
            raise HTTPException(403, 'Only a manager can settle an expired trade-in')
    # auto-stamp arrived_at, last_contacted_at
    if 'arrived' in update and update['arrived'] is True:
        update['arrived_at'] = datetime.now(timezone.utc)
    if 'contact_status' in update and update['contact_status'] in ('Contacted', 'Booked', 'Awaiting Reply'):
        update['last_contacted_at'] = datetime.now(timezone.utc)
    update['updated_at'] = datetime.now(timezone.utc)
    res = await db.clients.find_one_and_update(
        {'id': client_id}, {'$set': update}, return_document=True,
    )
    if not res:
        raise HTTPException(404, 'Client not found')
    return Client(**_strip(res))


@router.delete('/{client_id}', dependencies=[Depends(require_admin)])
async def delete_client(client_id: str):
    db = get_db()
    res = await db.clients.delete_one({'id': client_id})
    if res.deleted_count == 0:
        raise HTTPException(404, 'Client not found')
    return {'success': True}


# ===== Comments =====
@router.post('/{client_id}/comments', response_model=Comment)
async def add_comment(client_id: str, payload: CommentCreate, user: User = Depends(get_current_user)):
    db = get_db()
    comment = Comment(author_id=user.id, author_name=user.name, body=payload.body)
    res = await db.clients.update_one(
        {'id': client_id},
        {'$push': {'comments': comment.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, 'Client not found')
    return comment


@router.delete('/{client_id}/comments/{comment_id}')
async def delete_comment(
    client_id: str, comment_id: str,
    user: User = Depends(require_admin),
):
    db = get_db()
    res = await db.clients.update_one(
        {'id': client_id},
        {'$pull': {'comments': {'id': comment_id}}},
    )
    return {'success': res.modified_count > 0}


# ===== Accessories =====
@router.post('/{client_id}/accessories', response_model=Accessory)
async def add_accessory(client_id: str, payload: AccessoryCreate, user: User = Depends(get_current_user)):
    db = get_db()
    acc = Accessory(**payload.model_dump())
    res = await db.clients.update_one(
        {'id': client_id},
        {'$push': {'accessories': acc.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, 'Client not found')
    return acc


@router.patch('/{client_id}/accessories/{accessory_id}')
async def update_accessory(
    client_id: str, accessory_id: str, payload: AccessoryUpdate,
    user: User = Depends(get_current_user),
):
    db = get_db()
    sets = {}
    for k, v in payload.model_dump(exclude_unset=True).items():
        if v is not None:
            sets[f'accessories.$.{k}'] = v
    sets['accessories.$.updated_at'] = datetime.now(timezone.utc)
    res = await db.clients.update_one(
        {'id': client_id, 'accessories.id': accessory_id},
        {'$set': sets},
    )
    if res.matched_count == 0:
        raise HTTPException(404, 'Accessory or client not found')
    return {'success': True}


@router.delete('/{client_id}/accessories/{accessory_id}')
async def delete_accessory(
    client_id: str, accessory_id: str,
    user: User = Depends(get_current_user),
):
    db = get_db()
    res = await db.clients.update_one(
        {'id': client_id},
        {'$pull': {'accessories': {'id': accessory_id}}},
    )
    return {'success': res.modified_count > 0}


# ===== BYD registration / offer / trade-in workflow =====
@router.post('/{client_id}/generate-pack')
async def generate_client_pack(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    required = ('name', 'phone', 'email', 'vehicle', 'po_number')
    missing = [field for field in required if not client.get(field)]
    if missing:
        raise HTTPException(400, f'Cannot generate pack; missing: {", ".join(missing)}')
    generated_at = datetime.now(timezone.utc)
    file_name = f'generated-pack-{generated_at.strftime("%Y%m%d%H%M%S")}.pdf'
    pack_data = _make_pdf([
        'Harmony BYD Fairfield document pack',
        f'Generated: {generated_at.isoformat()}',
        '',
        'Order details',
        f'Customer: {client["name"]}',
        f'Mobile: {client["phone"]}',
        f'Email: {client["email"]}',
        f'Vehicle: {client["vehicle"]}',
        f'PO / INV: {client["po_number"]}',
        '',
        'Authority to Register - customer section',
        f'Registered operator: {client.get("registered_operator_type", "Individual")}',
        'Registration date: ______________________________',
        '',
        'Documents requested',
        '- Signed Authority to Register with the date left blank',
        '- Driver licence front and back',
        '- Pickup confirmation for BYD Fairfield, 415 Heidelberg Rd, Fairfield VIC 3078',
    ])
    await put_bytes(f'{client_id}/{file_name}', pack_data, 'application/pdf')
    doc = ClientDocument(client_id=client_id, document_type='Other', status='requested', file_name=file_name, source='manual', uploaded_by=user.id, storage_path=f'{client_id}/{file_name}', content_type='application/pdf', size_bytes=len(pack_data), notes='Generated working pack. Replace overlays with approved Harmony/VicRoads templates when supplied.')
    await db.clients.update_one({'id': client_id}, {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': generated_at}})
    await _refresh_document_completeness(db, client_id)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.pack.generate', 'entity': 'client', 'entity_id': client_id, 'meta': {'document_id': doc.id, 'file_name': file_name, 'merge_fields': {field: client.get(field) for field in required}, 'registration_date': None}, 'created_at': generated_at})
    return {'success': True, 'document': doc, 'email_subject': f"Your {client['vehicle']} order {client['po_number']} - documents required for registration"}


@router.post('/{client_id}/upload-link')
async def create_client_upload_link(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id}, {'id': 1, 'email': 1})
    if not client:
        raise HTTPException(404, 'Client not found')
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=72)
    await db.upload_links.insert_one({'id': uuid.uuid4().hex, 'token': token, 'client_id': client_id, 'created_by_id': user.id, 'expires_at': expires_at, 'active': True, 'created_at': datetime.now(timezone.utc)})
    app_url = os.environ.get('APP_URL', 'http://localhost:3000').rstrip('/')
    return {'url': f'{app_url}/upload/{token}', 'expires_at': expires_at}


@router.get('/upload/{token}')
async def upload_link_info(token: str):
    db = get_db()
    link = await db.upload_links.find_one({'token': token, 'active': True})
    if not link:
        raise HTTPException(404, 'Upload link not found')
    if link['expires_at'] < datetime.now(timezone.utc):
        raise HTTPException(410, 'Upload link expired')
    client = await db.clients.find_one({'id': link['client_id']}, {'name': 1, 'vehicle': 1})
    return {'client_name': client.get('name') if client else 'Customer', 'vehicle': client.get('vehicle') if client else None, 'expires_at': link['expires_at']}


@router.post('/upload/{token}', response_model=ClientDocument)
async def upload_from_customer_link(
    token: str,
    document_type: DocumentType = Form(...),
    file: UploadFile = File(...),
):
    db = get_db()
    link = await db.upload_links.find_one({'token': token, 'active': True})
    if not link:
        raise HTTPException(404, 'Upload link not found')
    if link['expires_at'] < datetime.now(timezone.utc):
        raise HTTPException(410, 'Upload link expired')
    data = await file.read()
    if not data:
        raise HTTPException(400, 'Uploaded file is empty')
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, 'Document too large (max 15 MB)')
    _validate_document_file(document_type, data, file.content_type)
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', file.filename or 'customer-document')
    doc = ClientDocument(client_id=link['client_id'], document_type=document_type, status='partial', file_name=file.filename, source='email', storage_path=f'{link["client_id"]}/{safe_name}', content_type=file.content_type, size_bytes=len(data), notes='Received through customer upload link')
    await put_bytes(f'{link["client_id"]}/{safe_name}', data, file.content_type)
    await db.clients.update_one({'id': link['client_id']}, {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}})
    await _refresh_document_completeness(db, link['client_id'])
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': None, 'actor_email': None, 'action': 'document.receive.portal_attach', 'entity': 'client', 'entity_id': link['client_id'], 'meta': {'document_id': doc.id, 'document_type': document_type, 'file_name': file.filename, 'upload_link_id': link['id']}, 'created_at': datetime.now(timezone.utc)})
    return doc


@router.post('/{client_id}/send-pack')
async def send_client_pack(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    if not client.get('email'):
        raise HTTPException(400, 'Client email is required before sending the pack')
    pack = next((doc for doc in reversed(client.get('documents', [])) if str(doc.get('file_name', '')).startswith('generated-pack-')), None)
    if not pack or not pack.get('storage_path'):
        raise HTTPException(400, 'Generate the document pack before sending it')
    if not await exists(pack['storage_path']):
        raise HTTPException(404, 'Generated pack file is missing')
    first_name = client['name'].split()[0]
    vehicle_full = client['vehicle']
    po_number = client.get('po_number', '')
    payment_method = client.get('payment_method') or 'To be confirmed'
    subject = f"Your {vehicle_full} order {po_number} - documents required for registration"
    body = f'''Hi {first_name},

Thank you again for choosing Harmony BYD, and congratulations on your new {vehicle_full}.

To keep everything simple and easy to track, we'll use this email thread for all updates relating to your order.

Your Order Details
Vehicle: {vehicle_full}
Purchase Order Number: {po_number}
Payment Method: {payment_method}

Throughout the process you may also hear from our Aftercare Team and Finance Team regarding your delivery.

Next Steps — please send through:

1. VicRoads Authority to Register — complete and sign the attached form, but leave the date blank. We will date the form when the vehicle is registered. Please make sure all highlighted areas are filled out.

2. Driver's Licence — a clear copy or photo of the front and back.

If you would like the vehicle registered under a company name instead of your personal name, please tell us before completing the form.

3. Vehicle pick-up — because of the CBD location space constraint, pick-up is from BYD Fairfield, 415 Heidelberg Rd, Fairfield VIC 3078 (unless your delivery record states otherwise).

Kind regards,
{user.name}
Delivery Team · 03 9356 8888 · deliveries@bydfairfield.com.au
Harmony BYD Fairfield
'''
    try:
        await send_email(recipient=client['email'], subject=subject, body=body, attachments=[(pack['file_name'], await get_bytes(pack['storage_path']), pack.get('content_type') or 'application/pdf')])
    except Exception as exc:
        raise HTTPException(502, f'Email delivery failed: {exc}')
    sent_at = datetime.now(timezone.utc)
    await db.clients.update_one({'id': client_id, 'documents.id': pack['id']}, {'$set': {'documents.$.sent_at': sent_at, 'updated_at': sent_at}})
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.pack.send', 'entity': 'client', 'entity_id': client_id, 'meta': {'document_id': pack['id'], 'recipient': client['email'], 'subject': subject}, 'created_at': sent_at})
    return {'success': True, 'sent_at': sent_at, 'recipient': client['email'], 'subject': subject}


@router.post('/{client_id}/generate-claim-pack', response_model=ClientDocument)
async def generate_offer_claim_pack(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    offer = await db.offers.find_one({'id': {'$in': client.get('linked_offer_ids', [])}, 'active': True})
    if not offer:
        raise HTTPException(400, 'Link an active offer before generating a claim pack')
    claim_types = offer.get('claim_doc_templates') or ([offer.get('claim_doc_template')] if offer.get('claim_doc_template') else [])
    if not claim_types and offer.get('cash_or_product') == 'product':
        raise HTTPException(400, 'This offer has no claim document rule configured')
    generated_at = datetime.now(timezone.utc)
    file_name = f'claim-pack-{generated_at.strftime("%Y%m%d%H%M%S")}.pdf'
    amount = client.get('your_way_note') or 'To be confirmed'
    pack_data = _make_pdf([
        'Harmony BYD offer claim pack',
        f'Offer: {offer.get("name", "")}',
        f'Customer: {client.get("name", "")}',
        f'PO / INV: {client.get("po_number", "")}',
        f'Your Way selection: {client.get("your_way_selection") or "Not selected"}',
        f'Amount / selection note: {amount}',
        'Being for: BYD vehicle offer claim',
        'Dealer: Harmony BYD Fairfield',
        'Bank verification: signature must match the contract; email alone is not bank-detail verification.',
        'If the customer is not present and the amount exceeds $500, attach a bank statement or deposit slip.',
    ])
    await put_bytes(f'{client_id}/{file_name}', pack_data, 'application/pdf')
    doc = ClientDocument(client_id=client_id, document_type='EFT form', status='requested', file_name=file_name, source='manual', uploaded_by=user.id, storage_path=f'{client_id}/{file_name}', content_type='application/pdf', size_bytes=len(pack_data), notes=f'Claim artefacts required: {", ".join(claim_types) if claim_types else "offer rule"}')
    await db.clients.update_one({'id': client_id}, {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': generated_at}})
    await _refresh_document_completeness(db, client_id)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'offer.claim.generate', 'entity': 'client', 'entity_id': client_id, 'meta': {'offer_id': offer['id'], 'document_id': doc.id, 'claim_types': claim_types}, 'created_at': generated_at})
    return doc


@router.post('/inbound-document', response_model=ClientDocument)
async def receive_inbound_document(
    document_type: DocumentType = Form(...),
    file: UploadFile = File(...),
    po_number: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
):
    db = get_db()
    client = None
    matched_by = None
    if po_number:
        client = await db.clients.find_one({'po_number': po_number})
        matched_by = 'po_number' if client else None
    if not client and email and phone:
        client = await db.clients.find_one({'email': email, 'phone': phone})
        matched_by = 'email_and_phone' if client else None
    if not client and email:
        client = await db.clients.find_one({'email': email})
        matched_by = 'email' if client else None
    if not client and phone:
        client = await db.clients.find_one({'phone': phone})
        matched_by = 'phone' if client else None
    if not client:
        raise HTTPException(422, 'No client matched by PO / INV, email, or phone')
    data = await file.read()
    if not data:
        raise HTTPException(400, 'Uploaded file is empty')
    _validate_document_file(document_type, data, file.content_type)
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', file.filename or 'inbound-document')
    doc = ClientDocument(client_id=client['id'], document_type=document_type, status='partial', file_name=file.filename, source='email', uploaded_by=user.id, storage_path=f'{client["id"]}/{safe_name}', content_type=file.content_type, size_bytes=len(data), notes=notes)
    await put_bytes(f'{client["id"]}/{safe_name}', data, file.content_type)
    await db.clients.update_one({'id': client['id']}, {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}})
    await _refresh_document_completeness(db, client['id'])
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.receive.classify.attach', 'entity': 'client', 'entity_id': client['id'], 'meta': {'document_id': doc.id, 'document_type': document_type, 'file_name': file.filename, 'matched_by': matched_by}, 'created_at': datetime.now(timezone.utc)})
    return doc


@router.post('/{client_id}/documents', response_model=ClientDocument)
async def add_client_document(client_id: str, payload: ClientDocumentUpdate, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    if payload.status == 'complete':
        raise HTTPException(400, 'Complete documents must be uploaded through the document upload endpoint')
    doc = ClientDocument(client_id=client_id, **payload.model_dump(exclude_unset=True))
    await db.clients.update_one({'id': client_id}, {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}})
    await _refresh_document_completeness(db, client_id)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.attach', 'entity': 'client', 'entity_id': client_id, 'meta': {'document_id': doc.id, 'document_type': doc.document_type, 'file_name': doc.file_name}, 'created_at': datetime.now(timezone.utc)})
    return doc


@router.post('/{client_id}/documents/upload', response_model=ClientDocument)
async def upload_client_document(
    client_id: str,
    document_type: DocumentType = Form(...),
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
):
    db = get_db()
    if not await db.clients.find_one({'id': client_id}, {'id': 1}):
        raise HTTPException(404, 'Client not found')
    if not file.filename:
        raise HTTPException(400, 'A file is required')
    data = await file.read()
    if not data:
        raise HTTPException(400, 'Uploaded file is empty')
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, 'Document too large (max 15 MB)')
    _validate_document_file(document_type, data, file.content_type)
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', file.filename)
    doc = ClientDocument(
        client_id=client_id,
        document_type=document_type,
        status='requested',
        file_name=file.filename,
        source='staff-upload',
        uploaded_by=user.id,
        storage_path=f'{client_id}/{safe_name}',
        content_type=file.content_type,
        size_bytes=len(data),
        notes=notes,
    )
    await put_bytes(f'{client_id}/{safe_name}', data, file.content_type)
    await db.clients.update_one({'id': client_id}, {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}})
    await _refresh_document_completeness(db, client_id)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.receive.attach', 'entity': 'client', 'entity_id': client_id, 'meta': {'document_id': doc.id, 'document_type': document_type, 'file_name': file.filename, 'size_bytes': len(data)}, 'created_at': datetime.now(timezone.utc)})
    return doc


@router.get('/{client_id}/documents', response_model=List[ClientDocument])
async def list_client_documents(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    rows = client.get('documents', [])
    return [ClientDocument(**d) for d in rows]


@router.get('/{client_id}/documents/{document_id}/download')
async def download_client_document(client_id: str, document_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    document = next((item for item in client.get('documents', []) if item.get('id') == document_id), None)
    if not document or not document.get('storage_path'):
        raise HTTPException(404, 'Document file not found')
    if not await exists(document['storage_path']):
        raise HTTPException(404, 'Document file not found')
    return Response(content=await get_bytes(document['storage_path']), media_type=document.get('content_type') or 'application/octet-stream', headers={'Content-Disposition': f'attachment; filename="{document.get("file_name") or "document"}"'})


@router.patch('/{client_id}/documents/{document_id}', response_model=ClientDocument)
async def update_client_document(
    client_id: str, document_id: str, payload: ClientDocumentUpdate,
    user: User = Depends(get_current_user),
):
    db = get_db()
    update = {f'documents.$.{key}': value for key, value in payload.model_dump(exclude_unset=True).items()}
    if not update:
        raise HTTPException(400, 'No document changes supplied')
    existing_document = await db.clients.find_one({'id': client_id, 'documents.id': document_id}, {'documents.$': 1})
    document = (existing_document or {}).get('documents', [{}])[0]
    merged_document = {**document, **{key.split('.', 2)[-1]: value for key, value in update.items()}}
    _validate_document_completion(merged_document, (existing_document or {}).get('documents', []))
    res = await db.clients.find_one_and_update(
        {'id': client_id, 'documents.id': document_id},
        {'$set': {**update, 'updated_at': datetime.now(timezone.utc)}},
        return_document=True,
    )
    if not res:
        raise HTTPException(404, 'Document or client not found')
    document = next(item for item in res.get('documents', []) if item.get('id') == document_id)
    await _refresh_document_completeness(db, client_id)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.status_change', 'entity': 'client', 'entity_id': client_id, 'meta': {'document_id': document_id, 'changes': payload.model_dump(exclude_unset=True)}, 'created_at': datetime.now(timezone.utc)})
    return ClientDocument(**document)


@router.patch('/{client_id}/trade-in')
async def update_trade_in(client_id: str, payload: dict, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    update = {'updated_at': datetime.now(timezone.utc)}
    if 'trade_in_attached' in payload:
        update['trade_in_attached'] = payload['trade_in_attached']
    if 'trade_in_sale_date' in payload:
        update['trade_in_sale_date'] = payload['trade_in_sale_date']
        try:
            sale_date = datetime.strptime(payload['trade_in_sale_date'], '%Y-%m-%d').date()
            update['trade_in_valid_until'] = (sale_date + timedelta(days=30)).isoformat()
        except (TypeError, ValueError):
            raise HTTPException(400, 'Trade-in sale date must use YYYY-MM-DD')
    if 'trade_in_flag' in payload:
        update['trade_in_flag'] = payload['trade_in_flag']
    if 'trade_in_valid_until' in payload:
        update['trade_in_valid_until'] = payload['trade_in_valid_until']
    if 'trade_in_status' in payload:
        if payload['trade_in_status'] not in ('Pending', 'Quoted', 'Accepted', 'Vehicle received', 'Settled', 'Valid', 'Expiring soon', 'Expiring', 'Expired', 'Cancelled'):
            raise HTTPException(400, 'Invalid trade-in status')
        update['trade_in_status'] = payload['trade_in_status']
        valid_until = update.get('trade_in_valid_until', client.get('trade_in_valid_until'))
        if payload['trade_in_status'] == 'Settled' and valid_until and valid_until < datetime.now(timezone.utc).date().isoformat() and not payload.get('trade_in_manager_reason'):
            raise HTTPException(400, 'A manager reason is required to settle an expired trade-in')
        if payload['trade_in_status'] == 'Settled' and valid_until and valid_until < datetime.now(timezone.utc).date().isoformat() and user.role not in ('admin', 'super_admin'):
            raise HTTPException(403, 'Only a manager can settle an expired trade-in')
    if 'trade_in_manager_reason' in payload:
        update['trade_in_manager_reason'] = payload['trade_in_manager_reason']
        update['trade_in_override_by'] = user.id
        update['trade_in_override_at'] = datetime.now(timezone.utc)
    await db.clients.update_one({'id': client_id}, {'$set': update})
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'trade_in.status_change', 'entity': 'client', 'entity_id': client_id, 'meta': {k: v for k, v in update.items() if k != 'updated_at'}, 'created_at': datetime.now(timezone.utc)})
    return {'success': True}


@router.patch('/{client_id}/activation')
async def update_activation_gate(client_id: str, payload: dict, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    gate_value = payload.get('activation_ready')
    status_value = payload.get('activation_status')
    handover = _has_signed_handover(client)
    activating = gate_value is True or status_value in ('Ready', 'Submitted to BYD', 'Active')
    if status_value is not None and status_value not in ('Blocked', 'Ready', 'Submitted to BYD', 'Active'):
        raise HTTPException(400, 'Invalid activation status')
    override_reason = payload.get('activation_override_reason')
    if activating and not handover and (user.role not in ('admin', 'super_admin') or not override_reason):
        raise HTTPException(400, 'Activation cannot be set to Ready while handover checklist is not on file')
    if status_value is not None:
        update = {'activation_status': status_value, 'updated_at': datetime.now(timezone.utc)}
        if status_value in ('Ready', 'Submitted to BYD', 'Active'):
            update['activation_ready'] = True
        elif status_value == 'Blocked':
            update['activation_ready'] = False
    else:
        update = {'activation_ready': bool(gate_value), 'updated_at': datetime.now(timezone.utc)}
    if activating and not handover:
        update['activation_override_reason'] = override_reason
        update['activation_override_by'] = user.id
        update['activation_override_at'] = datetime.now(timezone.utc)
    await db.clients.update_one({'id': client_id}, {'$set': update})
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'activation.status_change', 'entity': 'client', 'entity_id': client_id, 'meta': update, 'created_at': datetime.now(timezone.utc)})
    return {'success': True}


@router.post('/offers', response_model=OfferRecord)
async def create_offer(payload: OfferCreate, user: User = Depends(get_current_user)):
    db = get_db()
    record = OfferRecord(**payload.model_dump())
    await db.offers.insert_one(record.model_dump(mode='json'))
    await _rescore_open_deals(db)
    return record


@router.get('/offers', response_model=List[OfferRecord])
async def list_offers(user: User = Depends(get_current_user)):
    db = get_db()
    rows = []
    async for doc in db.offers.find({'active': True}).sort('name', 1):
        doc.pop('_id', None)
        rows.append(OfferRecord(**doc))
    return rows


@router.post('/offers/website-snapshot')
async def fetch_offer_website_snapshot(user: User = Depends(get_current_user)):
    url = os.environ.get('OFFERS_PAGE_URL', 'https://bydfairfield.com.au/offers')
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            response = await client.get(url)
            response.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f'Offer page fetch failed: {exc}')
    content = response.text
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    db = get_db()
    previous = await db.offer_snapshots.find_one(sort=[('fetched_at', -1)])
    snapshot = {'id': uuid.uuid4().hex, 'url': url, 'sha256': digest, 'fetched_at': datetime.now(timezone.utc), 'content_preview': re.sub(r'\s+', ' ', content)[:2000], 'changed': not previous or previous.get('sha256') != digest, 'fetched_by': user.id}
    await db.offer_snapshots.insert_one(snapshot)
    snapshot.pop('_id', None)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'offers.website_snapshot', 'entity': 'offer_catalogue', 'entity_id': None, 'meta': {'url': url, 'sha256': digest, 'changed': snapshot['changed']}, 'created_at': datetime.now(timezone.utc)})
    return snapshot


@router.patch('/offers/{offer_id}', response_model=OfferRecord)
async def update_offer(offer_id: str, payload: OfferUpdate, user: User = Depends(require_admin)):
    db = get_db()
    update = {key: value for key, value in payload.model_dump(exclude_unset=True).items()}
    update['updated_at'] = datetime.now(timezone.utc)
    record = await db.offers.find_one_and_update({'id': offer_id}, {'$set': update}, return_document=True)
    if not record:
        raise HTTPException(404, 'Offer not found')
    await _rescore_open_deals(db)
    record.pop('_id', None)
    return OfferRecord(**record)


@router.post('/offers/refresh')
async def refresh_offers(user: User = Depends(get_current_user)):
    db = get_db()
    refreshed_at = datetime.now(timezone.utc)
    result = await db.offers.update_many({'active': True}, {'$set': {'last_refreshed_at': refreshed_at}})
    await _rescore_open_deals(db)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'offers.refresh', 'entity': 'offer_catalogue', 'entity_id': None, 'meta': {'active_offers': result.modified_count, 'refreshed_at': refreshed_at}, 'created_at': refreshed_at})
    return {'success': True, 'refreshed_at': refreshed_at, 'active_offers': result.modified_count}


@router.get('/{client_id}/offer-matches')
async def match_client_offers(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    sale_type = client.get('sale_type', 'Retail')
    model_name = (client.get('vehicle') or '').lower()
    order_date = client.get('order_date') or client.get('delivery_date') or datetime.now(timezone.utc).date().isoformat()
    matches = []
    async for offer in db.offers.find({'active': True}).sort('name', 1):
        offer.pop('_id', None)
        models = [str(model).lower() for model in offer.get('eligible_models', [])]
        exclusions = [str(value).lower() for value in offer.get('sale_type_exclusions', [])]
        model_match = not models or any(model in model_name or model_name in model for model in models)
        excluded = sale_type.lower() in exclusions
        in_window = not offer.get('order_from') or offer.get('order_from') <= order_date <= (offer.get('order_to') or '9999-12-31')
        delivery_ok = not offer.get('deliver_by') or not client.get('delivery_date') or client['delivery_date'] <= offer['deliver_by']
        eligible = model_match and not excluded and in_window and delivery_ok
        reason = f'{sale_type} sale excluded' if excluded else 'Model or order window does not match' if not model_match or not in_window else 'Delivery deadline is after offer deadline' if not delivery_ok else None
        matches.append({'offer': offer, 'eligible': eligible, 'reason': reason})
    return matches


@router.patch('/{client_id}/offer-status')
async def update_client_offer_status(client_id: str, payload: dict, user: User = Depends(get_current_user)):
    db = get_db()
    update = {'offer_status': payload.get('offer_status', 'Eligible'), 'offer_reason': payload.get('offer_reason'), 'updated_at': datetime.now(timezone.utc)}
    await db.clients.update_one({'id': client_id}, {'$set': update})
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'offer.status_change', 'entity': 'client', 'entity_id': client_id, 'meta': update, 'created_at': datetime.now(timezone.utc)})
    return {'success': True}


# ===== Bulk import (paste / email) =====
@router.post('/import/bulk', response_model=List[Client])
async def bulk_import(items: List[ClientBase], user: User = Depends(get_current_user)):
    db = get_db()
    created = []
    for item in items:
        if item.vy_order_id:
            existing = await db.clients.find_one({'vy_order_id': item.vy_order_id})
            if existing:
                continue
        data = item.model_dump()
        data['imported_from'] = data.get('imported_from') or 'paste'
        data['imported_at'] = datetime.now(timezone.utc)
        client = Client(**data)
        await db.clients.insert_one(client.model_dump(mode='json'))
        created.append(client)
    return created


# ===== Unassigned inbound queue =====
@router.get('/inbound-queue')
async def inbound_queue(user: User = Depends(get_current_user), limit: int = 100):
    """Return unmatched inbound documents with 4-hour SLA tracking."""
    db = get_db()
    now = datetime.now(timezone.utc)
    sla_hours = 4
    items = []
    async for event in db.audit.find({'action': 'document.receive.unmatched'}).sort('created_at', -1).limit(limit):
        event.pop('_id', None)
        created_at = event.get('created_at')
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except ValueError:
                created_at = None
        hours_open = (now - created_at).total_seconds() / 3600 if created_at else None
        sla_breached = hours_open is not None and hours_open > sla_hours
        items.append({
            **event,
            'hours_open': round(hours_open, 1) if hours_open is not None else None,
            'sla_breached': sla_breached,
            'sla_hours': sla_hours,
        })
    return items


@router.post('/inbound-queue/{event_id}/resolve')
async def resolve_inbound_item(event_id: str, payload: dict, user: User = Depends(get_current_user)):
    """Manually match an unassigned inbound document to a client."""
    db = get_db()
    client_id = payload.get('client_id')
    document_type = payload.get('document_type', 'Other')
    file_name = payload.get('file_name', 'inbound-document')
    notes = payload.get('notes', 'Manually matched from unassigned inbound queue')
    if not client_id:
        raise HTTPException(400, 'client_id is required')
    client = await db.clients.find_one({'id': client_id}, {'id': 1})
    if not client:
        raise HTTPException(404, 'Client not found')
    doc = ClientDocument(
        client_id=client_id,
        document_type=document_type,
        status='partial',
        file_name=file_name,
        source='email',
        uploaded_by=user.id,
        notes=notes,
    )
    await db.clients.update_one(
        {'id': client_id},
        {'$push': {'documents': doc.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}},
    )
    await _refresh_document_completeness(db, client_id)
    await db.audit.update_one({'id': event_id}, {'$set': {'action': 'document.receive.unmatched.resolved', 'meta.resolved_by': user.id, 'meta.resolved_client_id': client_id, 'meta.resolved_at': datetime.now(timezone.utc).isoformat()}})
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'actor_email': user.email, 'action': 'document.receive.unmatched.resolved', 'entity': 'client', 'entity_id': client_id, 'meta': {'event_id': event_id, 'document_id': doc.id, 'document_type': document_type, 'file_name': file_name}, 'created_at': datetime.now(timezone.utc)})
    return {'success': True, 'document': doc}
