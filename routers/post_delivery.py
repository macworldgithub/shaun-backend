from datetime import datetime, timezone
from typing import Dict
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from auth import get_current_user, require_admin, effective_site, assert_site_access
from db import get_db
from models import User
from services import post_delivery as service
from services.account_access import site_key

router = APIRouter(prefix='/api/post-delivery', tags=['Post-delivery sequence'])


def selected_site(user, requested=None):
    site = effective_site(user, requested) or user.active_site
    if not site or site.lower().startswith('all'):
        raise HTTPException(400, 'Select a dealership site to view or edit its sequence')
    return site


class SequenceSettings(BaseModel):
    enabled: bool
    templates: Dict[str, str]

    @field_validator('templates')
    @classmethod
    def templates_valid(cls, value):
        if set(value) != set(service.DEFAULT_TEMPLATES):
            raise ValueError('Provide all four sequence templates')
        for text in value.values():
            if not text.strip() or len(text) > 600:
                raise ValueError('Templates must contain between 1 and 600 characters')
            import re
            if any(token not in ('[First Name]', '[Customer Name]', '[Make]', '[Model]') for token in re.findall(r'\[[^\]]+\]', text)):
                raise ValueError('Supported placeholders: [First Name], [Customer Name], [Make], [Model]')
        return {key: text.strip() for key, text in value.items()}


@router.get('/settings')
async def settings(request: Request, user: User = Depends(get_current_user)):
    site = selected_site(user, request.headers.get('x-site'))
    return {'site': site, **await service.settings(get_db(), site)}


@router.put('/settings')
async def update_settings(payload: SequenceSettings, request: Request, user: User = Depends(require_admin)):
    site = selected_site(user, request.headers.get('x-site'))
    await get_db().post_delivery_settings.update_one({'_id': site_key(site)}, {'$set': {
        **payload.model_dump(), 'site': site, 'updated_by': user.id, 'updated_at': datetime.now(timezone.utc),
    }}, upsert=True)
    return {'site': site, **payload.model_dump()}


@router.get('/clients/{client_id}')
async def client_sequence(client_id: str, user: User = Depends(get_current_user)):
    db = get_db()
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise HTTPException(404, 'Client not found')
    assert_site_access(user, client)
    jobs = await db.post_delivery_jobs.find({'client_id': client_id}, {'_id': 0, 'step': 1, 'day': 1, 'due_at': 1, 'status': 1, 'reason': 1, 'sent_at': 1}).sort('day', 1).to_list(4)
    customer = await service.linked_customer(db, client)
    return {'eligible': service.private_customer(client), 'opted_out': service.opted_out(client) or service.opted_out(customer),
            'delivered_at': client.get('delivered_at'), 'enrolled': bool(client.get('post_delivery_enrollment_id')),
            'jobs': jobs}
