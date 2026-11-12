"""Shared customer-profile handoff and internal Sales ↔ Delivery conversation."""
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from auth import get_current_user, effective_site, assert_site_access
from db import get_db
from models import User

router = APIRouter(prefix='/api/clients', tags=['Sales handoff'])


class ChatMessage(BaseModel):
    body: str = Field(min_length=1, max_length=2000)

    @field_validator('body')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('Enter a message')
        return value.strip()


class ReadMessage(BaseModel):
    last_message_id: Optional[str] = None


async def client_for(client_id, user):
    client = await get_db().clients.find_one({'id': client_id}, {
        'id': 1, 'name': 1, 'site_location': 1, 'delivery_handoff': 1, 'handoff_status': 1,
        'comments': 1, 'last_sales_message_id': 1, 'crm_customer_id': 1, 'delivery_unread': 1,
    })
    if not client:
        raise HTTPException(404, 'Client not found')
    assert_site_access(user, client)
    return client


@router.get('/handoff-notifications')
async def notifications(user: User = Depends(get_current_user)):
    query = {'$or': [{'handoff_status': 'new'}, {'delivery_unread': {'$gt': 0}}]}
    site = effective_site(user)
    if site:
        query['site_location'] = {'$regex': f'^{re.escape(site.strip())}$', '$options': 'i'}
    return await get_db().clients.find(query, {
        '_id': 0, 'id': 1, 'name': 1, 'handoff_status': 1, 'delivery_unread': 1,
    }).limit(100).to_list(100)


@router.get('/{client_id}/sales-chat')
async def conversation(client_id: str, user: User = Depends(get_current_user)):
    client = await client_for(client_id, user)
    return {'handoff': client.get('delivery_handoff'), 'status': client.get('handoff_status'),
            'linked_to_sales': bool(client.get('crm_customer_id')), 'unread': client.get('delivery_unread', 0),
            'last_sales_message_id': client.get('last_sales_message_id'),
            'messages': [c for c in client.get('comments', []) if c.get('channel') == 'sales_delivery'][-100:]}


@router.post('/{client_id}/sales-chat/read')
async def mark_read(client_id: str, payload: ReadMessage, user: User = Depends(get_current_user)):
    await client_for(client_id, user)
    query = {'id': client_id}
    if payload.last_message_id:
        query['last_sales_message_id'] = payload.last_message_id
    else:
        query['last_sales_message_id'] = None
    await get_db().clients.update_one(query, {'$set': {'delivery_unread': 0, 'handoff_status': 'acknowledged'}})
    return {'success': True}


@router.post('/{client_id}/sales-chat', status_code=201)
async def send_message(client_id: str, payload: ChatMessage, user: User = Depends(get_current_user)):
    client = await client_for(client_id, user)
    if not client.get('crm_customer_id'):
        raise HTTPException(409, 'Link this client to a CRM customer before messaging Sales')
    message = {'id': uuid.uuid4().hex, 'author_id': user.id, 'author_name': user.name,
               'team': 'delivery', 'channel': 'sales_delivery', 'body': payload.body,
               'created_at': datetime.now(timezone.utc).isoformat()}
    await get_db().clients.update_one({'id': client_id}, {
        '$push': {'comments': message}, '$inc': {'sales_unread': 1},
        '$set': {'last_delivery_message_id': message['id'], 'updated_at': datetime.now(timezone.utc)},
    })
    return message
