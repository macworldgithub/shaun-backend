from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from typing import Optional, List
from datetime import datetime, timezone, timedelta
import secrets
import uuid

from db import get_db
from models import (
    ShareLink, ShareLinkCreate, ShareAccessRequest, ShareAccessResponse, User,
)
from auth import (
    get_current_user, require_admin, get_share_payload,
    create_token,
)

router = APIRouter(prefix='/api', tags=['share'])


def _strip(d):
    if d: d.pop('_id', None)
    return d


def _new_token() -> str:
    return secrets.token_urlsafe(28)


# ----- Admin: manage share links -----
@router.get('/share-links', response_model=List[ShareLink])
async def list_share_links(_: User = Depends(require_admin)):
    db = get_db()
    out = []
    async for d in db.share_links.find().sort('created_at', -1):
        out.append(ShareLink(**_strip(d)))
    return out


@router.post('/share-links', response_model=ShareLink)
async def create_share_link(payload: ShareLinkCreate, admin: User = Depends(require_admin)):
    db = get_db()
    expires_at = None
    if payload.expires_in_hours and payload.expires_in_hours > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours)
    link = ShareLink(
        token=_new_token(),
        label=payload.label,
        allowed_emails=[e.lower() for e in payload.allowed_emails],
        created_by_id=admin.id,
        expires_at=expires_at,
    )
    await db.share_links.insert_one(link.model_dump(mode='json'))
    return link


@router.delete('/share-links/{link_id}')
async def revoke_share_link(link_id: str, _: User = Depends(require_admin)):
    db = get_db()
    res = await db.share_links.update_one({'id': link_id}, {'$set': {'active': False}})
    return {'success': res.matched_count > 0}


@router.get('/share-links/{link_id}/views')
async def list_views(link_id: str, _: User = Depends(require_admin), limit: int = 100):
    db = get_db()
    out = []
    async for d in db.share_views.find({'share_link_id': link_id}).sort('viewed_at', -1).limit(limit):
        d.pop('_id', None)
        out.append(d)
    return out


# ----- Public: viewer flow -----
@router.get('/share/{token}/info')
async def share_info(token: str):
    db = get_db()
    link = await db.share_links.find_one({'token': token})
    if not link:
        raise HTTPException(404, 'Share link not found')
    if not link.get('active', True):
        raise HTTPException(410, 'Share link revoked')
    if link.get('expires_at'):
        exp = link['expires_at']
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp.replace('Z', '+00:00'))
            except Exception:
                exp = None
        if exp and exp < datetime.now(timezone.utc):
            raise HTTPException(410, 'Share link expired')
    return {
        'label': link.get('label'),
        'requires_email': True,
        'restricted': len(link.get('allowed_emails', [])) > 0,
    }


@router.post('/share/{token}/access', response_model=ShareAccessResponse)
async def share_access(token: str, payload: ShareAccessRequest, request: Request):
    db = get_db()
    link = await db.share_links.find_one({'token': token})
    if not link or not link.get('active', True):
        raise HTTPException(404, 'Share link not found')
    expires_at = link.get('expires_at')
    if expires_at:
        exp = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
        if exp < datetime.now(timezone.utc):
            raise HTTPException(410, 'Share link expired')
    email = payload.email.lower()
    allowed = [e.lower() for e in link.get('allowed_emails', [])]
    if allowed and email not in allowed:
        raise HTTPException(403, 'This email is not authorised to view this dashboard')

    # Log view
    now = datetime.now(timezone.utc)
    view = {
        'id': uuid.uuid4().hex,
        'share_link_id': link['id'],
        'viewer_email': email,
        'ip': request.client.host if request.client else None,
        'user_agent': request.headers.get('user-agent'),
        'viewed_at': now,
    }
    await db.share_views.insert_one(view)
    await db.share_links.update_one(
        {'id': link['id']},
        {'$inc': {'view_count': 1}, '$set': {'last_viewed_at': now, 'last_viewer_email': email}},
    )

    # Issue short-lived share token
    hours = 8
    if expires_at:
        exp = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
        remaining = int((exp - now).total_seconds() // 3600)
        if remaining > 0:
            hours = min(hours, max(1, remaining))
    token_str = create_token(
        subject=link['id'],
        role=f'share:{link["id"]}',
        extra={'email': email, 'token': link['token']},
        expires_in_hours=hours,
    )
    return ShareAccessResponse(
        access_token=token_str,
        label=link.get('label', 'Live dashboard'),
        expires_at=now + timedelta(hours=hours),
    )


@router.get('/share/{token}/data')
async def share_data(token: str, payload: dict = Depends(get_share_payload)):
    if payload.get('token') != token:
        raise HTTPException(403, 'Token mismatch')
    db = get_db()
    link = await db.share_links.find_one({'id': payload['sub'], 'active': True})
    if not link:
        raise HTTPException(410, 'Share link revoked')

    # Compose live dashboard data
    total = await db.clients.count_documents({})
    by_stage = {}
    async for d in db.clients.aggregate([{'$group': {'_id': '$stage', 'count': {'$sum': 1}}}]):
        by_stage[d['_id'] or 'Unknown'] = d['count']
    by_contact = {}
    async for d in db.clients.aggregate([{'$group': {'_id': '$contact_status', 'count': {'$sum': 1}}}]):
        by_contact[d['_id'] or 'Unknown'] = d['count']
    arrived_pending = await db.clients.count_documents({'arrived': True, 'stage': {'$nin': ['Delivered']}})
    not_contacted = await db.clients.count_documents({'contact_status': 'Not Contacted'})
    unassigned = await db.clients.count_documents({'$or': [{'assigned_agent_id': None}, {'assigned_agent_id': ''}]})
    delivered_week = await db.clients.count_documents({
        'stage': 'Delivered',
        'updated_at': {'$gte': datetime.now(timezone.utc) - timedelta(days=7)},
    })

    upcoming = []
    upcoming_projection = {
        '_id': 0, 'id': 1, 'name': 1, 'vehicle': 1, 'rego': 1,
        'delivery_date': 1, 'stage': 1, 'salesperson': 1, 'location': 1,
        'arrived': 1, 'contact_status': 1,
    }
    async for d in db.clients.find({'stage': {'$ne': 'Delivered'}}, upcoming_projection).sort('delivery_date', 1).limit(15):
        upcoming.append({
            'id': d.get('id'),
            'name': d.get('name'),
            'vehicle': d.get('vehicle'),
            'rego': d.get('rego'),
            'delivery_date': d.get('delivery_date'),
            'stage': d.get('stage'),
            'salesperson': d.get('salesperson'),
            'location': d.get('location'),
            'arrived': d.get('arrived'),
            'contact_status': d.get('contact_status'),
        })

    by_agent = {}
    async for d in db.clients.aggregate([
        {'$match': {'stage': {'$ne': 'Delivered'}}},
        {'$group': {'_id': '$assigned_agent_id', 'count': {'$sum': 1}}},
    ]):
        by_agent[d['_id'] or 'unassigned'] = d['count']

    # Resolve agent names
    agent_ids = [k for k in by_agent.keys() if k != 'unassigned']
    agent_map = {}
    if agent_ids:
        async for u in db.users.find({'id': {'$in': agent_ids}}):
            agent_map[u['id']] = u.get('name', u.get('email', 'Agent'))
    by_agent_named = [
        {'agent_id': k, 'agent_name': agent_map.get(k, 'Unassigned' if k == 'unassigned' else 'Unknown'), 'count': v}
        for k, v in by_agent.items()
    ]

    return {
        'label': link.get('label'),
        'generated_at': datetime.now(timezone.utc),
        'totals': {
            'total_clients': total,
            'arrived_pending': arrived_pending,
            'not_contacted': not_contacted,
            'unassigned': unassigned,
            'delivered_this_week': delivered_week,
        },
        'by_stage': by_stage,
        'by_contact': by_contact,
        'by_agent': by_agent_named,
        'upcoming': upcoming,
    }
