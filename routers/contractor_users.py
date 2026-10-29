"""Contractor administration, separate from owner-only cross-platform accounts."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from auth import require_admin, effective_site, assert_site_access, hash_password, generate_password
from db import get_db
from services.contractor_access import site_filter
from models import User, UserInDB, UserCreate, UserUpdate

router = APIRouter(prefix='/api/contractor/users', tags=['contractor users'])
ROLES = ('contractor', 'admin', 'super_admin')


async def audit(db, actor, action, uid):
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': actor.id,
        'actor_email': str(actor.email), 'action': action, 'entity': 'user',
        'entity_id': uid, 'created_at': datetime.now(timezone.utc)})


async def editable(db, actor, uid):
    target = await db.users.find_one({'id': uid})
    if not target or target.get('role') not in ROLES or target.get('deleted_at'):
        raise HTTPException(404, 'User not found')
    assert_site_access(actor, {'site_location': target.get('locked_site') or target.get('active_site')})
    if actor.role != 'super_admin' and target.get('role') != 'contractor':
        raise HTTPException(403, 'Admins can manage contractors only')
    if str(target.get('email', '')).lower() == 'devs@omnisuiteai.com':
        raise HTTPException(403, 'The owner account is protected')
    if isinstance(target.get('platform_access'), list) and target.get('platform_access') != ['delivery_centre']:
        raise HTTPException(409, 'Manage shared accounts in Sales CRM → Users & Access')
    return target


@router.get('')
async def list_users(role: str = None, active_only: bool = False, actor: User = Depends(require_admin)):
    db = get_db()
    permitted = ROLES if actor.role == 'super_admin' else ('contractor',)
    query = {'role': {'$in': list(permitted)}, 'deleted_at': None}
    if role:
        if role not in permitted:
            return []
        query['role'] = role
    if active_only:
        query['active'] = True
    site = effective_site(actor)
    if site:
        query['$or'] = [{'locked_site': site_filter(site)}, {'locked_site': None, 'active_site': site_filter(site)}]
    return [User(**doc) async for doc in db.users.find(query).sort('name', 1)]


@router.post('')
async def create_user(payload: UserCreate, actor: User = Depends(require_admin)):
    if payload.role not in ROLES or (actor.role != 'super_admin' and payload.role != 'contractor'):
        raise HTTPException(403, 'Only Super Admins can grant Admin rights')
    db = get_db()
    if await db.users.find_one({'email': str(payload.email).lower()}):
        raise HTTPException(409, 'Email already in use')
    site = effective_site(actor, payload.active_site) or 'Fairfield'
    password = payload.password or generate_password()
    if len(password) < 8:
        raise HTTPException(400, 'Password must contain at least 8 characters')
    data = payload.model_dump(exclude={'password'})
    data.update(email=str(payload.email).lower(), active_site=site, locked_site=site,
        platform_access=['delivery_centre'], password_hash=hash_password(password),
        must_change_password=not bool(payload.password))
    if payload.role == 'super_admin':
        data.update(locked_site=None, all_sites=True)
    user = UserInDB(**data)
    await db.users.insert_one(user.model_dump(mode='json'))
    await audit(db, actor, 'contractor.user.create', user.id)
    return {'user': User(**user.model_dump()), 'temp_password': password if not payload.password else None}


@router.patch('/{uid}')
async def update_user(uid: str, payload: UserUpdate, actor: User = Depends(require_admin)):
    db = get_db()
    target = await editable(db, actor, uid)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if updates.get('role') not in (None, *ROLES) or (actor.role != 'super_admin' and updates.get('role', 'contractor') != 'contractor'):
        raise HTTPException(403, 'Only Super Admins can grant Admin rights')
    if uid == actor.id and (updates.get('active') is False or updates.get('role', actor.role) != actor.role):
        raise HTTPException(400, 'You cannot deactivate or demote your own account')
    for key in ('active_site', 'locked_site'):
        if key in updates and effective_site(actor, updates[key]) != updates[key]:
            raise HTTPException(403, 'Site access denied')
    if updates.get('role') == 'super_admin':
        updates.update(locked_site=None, all_sites=True)
    elif target.get('role') == 'super_admin' and updates.get('role'):
        updates.update(locked_site=target.get('active_site') or 'Fairfield', all_sites=False)
    doc = await db.users.find_one_and_update({'id': uid}, {'$set': updates, '$inc': {'auth_version': 1}}, return_document=True)
    await audit(db, actor, 'contractor.user.update', uid)
    return User(**doc)


@router.post('/{uid}/reset-password')
async def reset_password(uid: str, actor: User = Depends(require_admin)):
    db = get_db()
    await editable(db, actor, uid)
    password = generate_password()
    await db.users.update_one({'id': uid}, {'$set': {'password_hash': hash_password(password), 'must_change_password': True}, '$inc': {'auth_version': 1}})
    await audit(db, actor, 'contractor.user.password_reset', uid)
    return {'temp_password': password}


@router.delete('/{uid}')
async def deactivate_user(uid: str, actor: User = Depends(require_admin)):
    return await update_user(uid, UserUpdate(active=False), actor)
