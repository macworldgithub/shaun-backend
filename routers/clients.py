from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from typing import Optional, List
from datetime import datetime, timezone
import uuid

from db import get_db
from models import (
    Client, ClientBase, ClientUpdate, Comment, CommentCreate,
    Accessory, AccessoryCreate, AccessoryUpdate, User,
)
from auth import get_current_user, require_admin

router = APIRouter(prefix='/api/clients', tags=['clients'])


def _strip(doc):
    if doc:
        doc.pop('_id', None)
    return doc


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

