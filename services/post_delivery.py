"""Durable private-customer delivery sequence, shared with the sales CRM."""
import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId
from db import get_db
from mobilemessage import send_sms, normalise_au, MOBILEMESSAGE_FROM
from services.account_access import site_key

log = logging.getLogger('post_delivery')
MELBOURNE = ZoneInfo('Australia/Melbourne')
STEPS = [('delivery_day', 0), ('sales_followup', 3), ('day_23', 23), ('survey', 31)]
DEFAULT_TEMPLATES = {
    'delivery_day': 'Hi [First Name], congratulations on collecting your new [Make] [Model] today! We hope you love driving it and wish you many happy miles ahead. If you need anything at all, just reply to this message. Enjoy your car — The Harmony Group team.',
    'sales_followup': "Follow-up reminder: [Customer Name] collected their [Make] [Model] 3 days ago. Please call to check how they're settling in with the vehicle, confirm everything is going well, and record your notes plus any actions required in the CRM.",
    'day_23': "Hi [First Name], just checking in on your new [Make] [Model]. We hope you're really enjoying it so far. If there's anything we can help with — questions, advice, or service booking — simply reply to this text and we'll take care of it. Harmony Group.",
    'survey': "Hi [First Name], we hope you're loving your [Make] [Model]. Your feedback really helps us improve. A short survey about your delivery and vehicle experience is now available — we'd be grateful if you could take a moment to complete it. Thank you, Harmony Group.",
}


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def private_customer(client):
    sale = str(client.get('sale_type') or '').strip().lower()
    deal = str(client.get('deal_type') or '').lower()
    if any(word in sale or word in deal for word in ('fleet', 'demo', 'government', 'rental')):
        return False
    if client.get('registered_operator_type') == 'Company' or client.get('business_client_id'):
        return False
    category = client.get('customer_type')
    return category == 'Private' if category else sale in ('private', 'retail', 'cash') and not client.get('fleet_company')


def delivery_transition(existing, update, now=None):
    """Only a new operational Delivered transition enrolls; no historical backfill."""
    merged = {**existing, **update}
    if merged.get('stage') != 'Delivered' or existing.get('stage') == 'Delivered' or existing.get('post_delivery_enrollment_id'):
        return {}
    now = now or datetime.now(timezone.utc)
    return {'delivered_at': now, 'post_delivery_enrollment_id': uuid.uuid4().hex}


def render(template, client):
    vehicle = client.get('vehicle') or ''
    make = client.get('vehicle_make') or client.get('make')
    model = client.get('vehicle_model') or client.get('model')
    if not make:
        match = re.match(r'^(BYD|Denza)\s+(.+)$', vehicle, re.I)
        make, inferred = (match.group(1), match.group(2)) if match else ('', vehicle)
        model = model or inferred
    elif not model:
        model = re.sub(r'^' + re.escape(make) + r'\s+', '', vehicle, flags=re.I)
    if make and model:
        model = re.sub(r'^' + re.escape(make) + r'\s+', '', model, flags=re.I)
    values = {'[First Name]': str(client.get('name') or 'there').split()[0], '[Customer Name]': client.get('name') or 'Customer',
              '[Make]': make or '', '[Model]': model or vehicle or 'vehicle'}
    for key, value in values.items():
        template = template.replace(key, str(value))
    return re.sub(r' {2,}', ' ', template).strip()


async def settings(db, site):
    saved = await db.post_delivery_settings.find_one({'_id': site_key(site)}) or {}
    return {'enabled': saved.get('enabled', True), 'templates': {**DEFAULT_TEMPLATES, **saved.get('templates', {})}}


async def ensure_jobs(db, client):
    enrollment = client.get('post_delivery_enrollment_id')
    if not enrollment or not client.get('delivered_at') or client.get('post_delivery_scheduled'):
        return
    delivered = utc(client['delivered_at']).astimezone(MELBOURNE)
    for step, days in STEPS:
        identity = f'post-delivery:{client["id"]}:{enrollment}:{step}'
        await db.post_delivery_jobs.update_one({'_id': identity}, {'$setOnInsert': {
            'client_id': client['id'], 'enrollment_id': enrollment, 'step': step, 'day': days,
            'due_at': (delivered + timedelta(days=days)).astimezone(timezone.utc),
            'status': 'pending' if private_customer(client) else 'skipped',
            'reason': None if private_customer(client) else 'Private customers only; business, fleet and demo excluded',
            'attempts': 0, 'created_at': datetime.now(timezone.utc),
        }}, upsert=True)
    await db.clients.update_one({'id': client['id'], 'post_delivery_enrollment_id': enrollment}, {'$set': {'post_delivery_scheduled': True}})


async def linked_customer(db, client):
    if not client.get('crm_customer_id'):
        return None
    return await db.customers.find_one({'customer_id': client['crm_customer_id']})


def opted_out(record):
    return bool(record and any(record.get(key) for key in ('do_not_contact', 'sms_opt_out', 'opted_out', 'unsubscribed')))


async def create_sales_reminder(db, job, client, customer, body, now):
    assignee_id = (customer or {}).get('owner_user_id')
    name = client.get('salesperson') or (customer or {}).get('owner_name') or ''
    if name:
        users = await db.users.find({'active': {'$ne': False}, 'name': {'$regex': '^' + re.escape(name) + '$', '$options': 'i'}}).to_list(50)
        site = site_key(client.get('site_location'))
        candidates = [user for user in users if not user.get('deleted_at') and user.get('role') != 'contractor'
                      and (user.get('platform_access') is None or any(platform in user['platform_access'] for platform in ('crm', 'sales_floor')))
                      and (user.get('all_sites') or site_key(user.get('site') or user.get('active_site') or user.get('locked_site')) == site
                           or any(site_key(value) == site for value in user.get('allowed_sites') or []))]
        user = next((user for user in candidates if str(user['_id']) == str(assignee_id)), None)
        if user is None and len(candidates) == 1:
            user = candidates[0]
        if user:
            assignee_id = str(user['_id'])
    identity = hashlib.sha256(job['_id'].encode()).hexdigest()
    reminder = {'id': identity, 'client_id': client['id'], 'customer_id': client.get('crm_customer_id'),
                'customer_name': client.get('name'), 'phone': client.get('phone'), 'vehicle': client.get('vehicle'),
                'site': client.get('site_location'), 'salesperson_id': str(assignee_id) if assignee_id else None,
                'salesperson_name': name, 'body': body, 'due_at': job['due_at'], 'status': 'open', 'created_at': now}
    await db.post_delivery_followups.update_one({'_id': identity}, {'$setOnInsert': reminder}, upsert=True)
    if client.get('crm_customer_id'):
        await db.timelineevents.update_one({'_id': ObjectId(identity[:24])}, {'$setOnInsert': {
            'event_id': 'PD-' + identity, 'customer_id': client['crm_customer_id'], 'type': 'follow_up', 'event_type': 'follow_up',
            'title': 'Day 3 post-delivery call reminder', 'content': body, 'body': body,
            'author_name': 'Harmony Delivery Centre', 'source_system': 'delivery', 'visibility': 'internal',
            'occurred_at': now, 'createdAt': now, 'timestamp': now,
        }}, upsert=True)


async def process_job(db, identity, now=None, send=None):
    now = now or datetime.now(timezone.utc)
    send = send or send_sms
    token = uuid.uuid4().hex
    job = await db.post_delivery_jobs.find_one_and_update({
        '_id': identity, 'due_at': {'$lte': now}, '$or': [
            {'status': 'pending'}, {'status': 'retry', 'retry_at': {'$lte': now}},
            {'status': 'processing', 'lease_until': {'$lt': now}},
        ],
    }, {'$set': {'status': 'processing', 'lease_token': token, 'lease_until': now + timedelta(minutes=2)}}, return_document=True)
    if not job:
        return
    match = {'_id': identity, 'lease_token': token}

    async def finish(state, **fields):
        await db.post_delivery_jobs.update_one(match, {'$set': {'status': state, 'updated_at': now, **fields}, '$unset': {'lease_token': '', 'lease_until': ''}})

    try:
        client = await db.clients.find_one({'id': job['client_id']})
        if not client or client.get('stage') != 'Delivered' or client.get('post_delivery_enrollment_id') != job['enrollment_id']:
            return await finish('skipped', reason='Delivery was removed or reversed')
        if not private_customer(client):
            return await finish('skipped', reason='Private customers only; business, fleet and demo excluded')
        config = await settings(db, client.get('site_location'))
        if not config['enabled']:
            return await finish('retry', retry_at=now + timedelta(minutes=5), reason='Sequence is paused for this site')
        customer = await linked_customer(db, client)
        if client.get('do_not_contact') or (customer or {}).get('do_not_contact'):
            return await finish('skipped', reason='Customer has requested no contact')
        body = job.get('body') or render(config['templates'][job['step']], client)
        if job['step'] == 'sales_followup':
            await create_sales_reminder(db, job, client, customer, body, now)
            return await finish('reminder', body=body)
        if opted_out(client) or opted_out(customer):
            return await finish('skipped', reason='Customer opted out of SMS')
        if now - utc(job['due_at']) > timedelta(days=1):
            return await finish('skipped', reason='Message delivery window has passed')
        phone = job.get('phone') or normalise_au(client.get('phone') or '')
        if job.get('phone') and phone != normalise_au(client.get('phone') or ''):
            return await finish('review', reason='Recipient number changed during a send; check delivery before retrying')
        if not re.fullmatch(r'\+614\d{8}', phone):
            return await finish('skipped', reason='No valid Australian mobile number')
        first_attempt = utc(job.get('first_attempt_at') or now)
        if now - first_attempt >= timedelta(hours=23):
            return await finish('review', reason='Provider retry window expired; check delivery before retrying')
        sender = job.get('sender') or os.environ.get('MOBILEMESSAGE_SENDER_ID') or os.environ.get('MOBILEMESSAGE_FROM') or MOBILEMESSAGE_FROM or 'HARMONY'
        # Freeze the exact provider payload and key before issuing an external request.
        job['attempts'] += 1
        await db.post_delivery_jobs.update_one(match, {'$set': {'body': body, 'phone': phone, 'sender': sender, 'first_attempt_at': first_attempt, 'attempts': job['attempts']}})
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        result = await send(phone, body, custom_ref=hashlib.sha256(identity.encode()).hexdigest(), idempotency_key=key, sender=sender)
        if result.get('success'):
            message_id = hashlib.sha256(identity.encode()).hexdigest()
            await db.messages.update_one({'_id': ObjectId(message_id[:24])}, {'$setOnInsert': {
                'id': message_id, 'client_id': client['id'], 'client_name': client.get('name'), 'phone': phone,
                'site': client.get('site_location'), 'body': body, 'direction': 'outbound', 'status': result.get('status', 'queued'),
                'provider_message_id': result.get('message_id'), 'provider_response': result.get('response'),
                'sent_at': now.isoformat(), 'sent_by_id': 'post_delivery_sequence', 'automation_step': job['step'],
            }}, upsert=True)
            return await finish('sent', provider_message_id=result.get('message_id'), sent_at=now, reason=None)
        retryable = result.get('retryable') or result.get('status') in ('unconfigured', 'rate_limited', 'error')
        state = 'retry' if retryable and job['attempts'] < 6 else 'failed'
        await finish(state, reason=result.get('error') or 'SMS was not accepted', retry_at=now + timedelta(minutes=min(2 ** job['attempts'], 60)))
    except asyncio.CancelledError:
        raise
    except Exception:
        # The lease expires and replays the identical provider payload/key after a crash.
        log.exception('Post-delivery job failed: %s', identity)


async def process_client(db, client_id):
    client = await db.clients.find_one({'id': client_id})
    if not client:
        return
    await ensure_jobs(db, client)
    async for job in db.post_delivery_jobs.find({'client_id': client_id, 'status': {'$in': ['pending', 'retry', 'processing']}}):
        await process_job(db, job['_id'])


async def run_due(db=None, now=None):
    db = db if db is not None else get_db()
    now = now or datetime.now(timezone.utc)
    # Repair an interrupted enrollment without enrolling historical Delivered records.
    async for client in db.clients.find({'stage': 'Delivered', 'post_delivery_enrollment_id': {'$exists': True}, 'post_delivery_scheduled': {'$ne': True}}):
        await ensure_jobs(db, client)
    async for job in db.post_delivery_jobs.find({'due_at': {'$lte': now}, 'status': {'$in': ['pending', 'retry', 'processing']}}).sort('due_at', 1).limit(100):
        await process_job(db, job['_id'], now)


async def worker():
    while True:
        try:
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Post-delivery sequence poll failed')
        await asyncio.sleep(60)
