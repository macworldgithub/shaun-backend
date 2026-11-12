"""Reply notifications shared with the Sales Floor Node receiver, not SMS sends."""
import hashlib
from datetime import datetime, timezone
from fastapi import HTTPException
from auth import effective_site
from services.account_access import site_key as account_site_key
from services.thread_history import allowed_site, phone_key, history


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def site_key(value):
    key = account_site_key(value)
    return 'melbournecity' if key in ('melbournecbd', 'melbournecity', 'melbourne') else key


def reader(user):
    return digest(str(user.email or user.id).strip().lower())


async def record_reply(db, message, metadata=None):
    metadata = metadata or {}
    if message.get('direction') != 'inbound' or not message.get('site') or not phone_key(message.get('phone')) or not message.get('provider_message_id'):
        return
    identity = digest('sms-reply:' + str(message['provider_message_id']))
    at = message.get('sent_at') or datetime.now(timezone.utc)
    row = {'_id': identity, 'id': identity, 'message_id': str(message['provider_message_id']), 'type': 'sms_reply',
           'phone': message['phone'], 'phone_key': phone_key(message['phone']), 'site': site_key(message['site']), 'site_name': message['site'],
           'sender_name': message.get('client_name') or metadata.get('name') or message['phone'],
           'preview': str(message.get('body') or '')[:500], 'client_id': metadata.get('client_id') or message.get('client_id'),
           'lead_id': metadata.get('lead_id'), 'customer_id': metadata.get('customer_id'),
           'sent_at': at.isoformat() if isinstance(at, datetime) else at, 'created_at': datetime.now(timezone.utc), 'read_by': []}
    await db.sms_reply_notifications.update_one({'_id': identity}, {'$setOnInsert': row}, upsert=True)
    return identity


def scope(user, requested=None):
    if user.role == 'contractor':
        raise HTTPException(403, 'SMS access is not assigned')
    site = effective_site(user, requested if requested and not requested.lower().startswith('all') else None)
    if site:
        if not allowed_site(user, site):
            raise HTTPException(403, 'Site access denied')
        return {'site': site_key(site)}
    if user.allowed_sites is not None and not user.all_sites:
        return {'site': {'$in': [site_key(s) for s in user.allowed_sites]}}
    if user.all_sites or user.role in ('super_admin', 'admin', 'site_admin', 'delivery_specialist'):
        return {'site': {'$ne': ''}}
    return {'site': site_key(user.active_site or '__unassigned__')}


async def notification(db, user, identity):
    import re
    if not re.fullmatch('[a-f0-9]{64}', identity):
        raise HTTPException(404, 'Notification not found')
    row = await db.sms_reply_notifications.find_one({'_id': identity, **scope(user)})
    if not row or not allowed_site(user, row['site_name']):
        raise HTTPException(404, 'Notification not found')
    return row


async def conversation(db, user, identity):
    row = await notification(db, user, identity)
    messages = await history(user, requested_site=row['site_name'], limit=100, phone=row['phone'])
    return {'recipient': {'phone': row['phone'], 'site': row['site_name'], 'name': row['sender_name'], 'client_id': row.get('client_id')},
            'messages': [{'id': m.get('provider_message_id') or m['id'], 'body': m['body'], 'direction': m['direction'], 'sent_at': m.get('sent_at'), 'status': m.get('status')} for m in reversed(messages)]}
