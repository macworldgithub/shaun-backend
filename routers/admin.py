from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, Form, status
from typing import Optional, List
from datetime import datetime, timezone
import asyncio
import uuid

from db import get_db
from models import (
    User, UserInDB, UserCreate, UserUpdate, AuditEvent,
)
from auth import (
    get_current_user, require_admin, require_super,
    hash_password, generate_password,
)
from services.harmony_import import import_harmony_from_bytes

router = APIRouter(prefix='/api/admin', tags=['admin'])


def _strip(d):
    if d: d.pop('_id', None)
    return d


# ----- Users -----
@router.get('/users', response_model=List[User])
async def list_users(_: User = Depends(require_admin)):
    db = get_db()
    out = []
    async for d in db.users.find().sort('created_at', -1):
        d.pop('password_hash', None)
        out.append(User(**_strip(d)))
    return out


@router.post('/users')
async def create_user(payload: UserCreate, request: Request, admin: User = Depends(require_admin)):
    db = get_db()
    email = payload.email.lower()
    if await db.users.find_one({'email': email}):
        raise HTTPException(409, 'Email already in use')
    if payload.role == 'super_admin' and admin.role != 'super_admin':
        raise HTTPException(403, 'Only super admins can create super admins')
    pw = payload.password or generate_password()
    user_db = UserInDB(
        email=email,
        name=payload.name,
        role=payload.role,
        password_hash=hash_password(pw),
        must_change_password=payload.password is None,
    )
    await db.users.insert_one(user_db.model_dump(mode='json'))
    await db.audit.insert_one({
        'id': uuid.uuid4().hex,
        'actor_id': admin.id, 'actor_email': admin.email,
        'action': 'user.create',
        'entity': 'user', 'entity_id': user_db.id,
        'meta': {'email': email, 'role': payload.role},
        'ip': request.client.host if request.client else None,
        'created_at': datetime.now(timezone.utc),
    })
    user = User(**user_db.model_dump(exclude={'password_hash'}))
    return {'user': user, 'temp_password': pw if payload.password is None else None}


@router.patch('/users/{uid}', response_model=User)
async def update_user(uid: str, payload: UserUpdate, admin: User = Depends(require_admin)):
    db = get_db()
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if 'role' in update and update['role'] == 'super_admin' and admin.role != 'super_admin':
        raise HTTPException(403, 'Only super admins can grant super admin')
    res = await db.users.find_one_and_update({'id': uid}, {'$set': update}, return_document=True)
    if not res:
        raise HTTPException(404, 'User not found')
    res.pop('password_hash', None)
    return User(**_strip(res))


@router.post('/users/{uid}/reset-password')
async def reset_password(uid: str, admin: User = Depends(require_admin)):
    db = get_db()
    target = await db.users.find_one({'id': uid})
    if not target:
        raise HTTPException(404, 'User not found')
    new_pw = generate_password()
    await db.users.update_one(
        {'id': uid},
        {'$set': {'password_hash': hash_password(new_pw), 'must_change_password': True}},
    )
    return {'temp_password': new_pw}


@router.delete('/users/{uid}')
async def deactivate_user(uid: str, admin: User = Depends(require_admin)):
    db = get_db()
    if admin.id == uid:
        raise HTTPException(400, 'Cannot deactivate your own account')
    res = await db.users.update_one({'id': uid}, {'$set': {'active': False}})
    if res.matched_count == 0:
        raise HTTPException(404, 'User not found')
    return {'success': True}


# ----- Audit log -----
@router.get('/audit')
async def audit_log(_: User = Depends(require_admin), limit: int = Query(200, le=1000)):
    db = get_db()
    out = []
    async for d in db.audit.find().sort('created_at', -1).limit(limit):
        d.pop('_id', None)
        out.append(d)
    return out


# ----- Stats -----
@router.get('/stats')
async def stats(_: User = Depends(get_current_user)):
    db = get_db()
    total = await db.clients.count_documents({})
    by_stage = {}
    pipeline = [{'$group': {'_id': '$stage', 'count': {'$sum': 1}}}]
    async for d in db.clients.aggregate(pipeline):
        by_stage[d['_id'] or 'Unknown'] = d['count']
    arrived_pending = await db.clients.count_documents({
        'arrived': True,
        'stage': {'$nin': ['Delivered']},
    })
    not_contacted = await db.clients.count_documents({'contact_status': 'Not Contacted'})
    unassigned = await db.clients.count_documents({'$or': [{'assigned_agent_id': None}, {'assigned_agent_id': ''}]})
    sms_count = await db.messages.count_documents({})
    return {
        'total_clients': total,
        'by_stage': by_stage,
        'arrived_pending': arrived_pending,
        'not_contacted': not_contacted,
        'unassigned': unassigned,
        'sms_total': sms_count,
    }


# ----- Harmony Auto Excel import -----
@router.post('/imports/harmony')
async def import_harmony_xlsx(
    request: Request,
    file: UploadFile = File(...),
    replace: bool = Form(True),
    dry_run: bool = Form(False),
    admin: User = Depends(require_super),
):
    """Upload a Harmony Auto export (.xlsx) and import client records.

    - Only super_admin may run this endpoint.
    - `replace=true` (default) wipes prior `imported_from=harmony-xlsx` rows then re-imports.
    - `dry_run=true` parses the file and returns a summary WITHOUT writing to the DB.
    """
    fname = (file.filename or '').lower()
    if not fname.endswith('.xlsx'):
        raise HTTPException(400, 'File must be a .xlsx workbook')

    data = await file.read()
    if not data:
        raise HTTPException(400, 'Uploaded file is empty')
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, 'File too large (max 25 MB)')

    try:
        summary = await asyncio.to_thread(
            import_harmony_from_bytes, data, replace=replace, dry_run=dry_run,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f'Failed to parse spreadsheet: {e}')

    db = get_db()
    await db.audit.insert_one({
        'id': uuid.uuid4().hex,
        'actor_id': admin.id, 'actor_email': admin.email,
        'action': 'imports.harmony.dry_run' if dry_run else 'imports.harmony.run',
        'entity': 'client', 'entity_id': None,
        'meta': {
            'filename': file.filename,
            'size_bytes': len(data),
            'replace': replace,
            'inserted': summary.get('inserted', 0),
            'wiped': summary.get('wiped', 0),
        },
        'ip': request.client.host if request.client else None,
        'created_at': datetime.now(timezone.utc),
    })

    return {
        'success': True,
        'filename': file.filename,
        'size_bytes': len(data),
        'summary': summary,
    }
