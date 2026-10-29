"""Authenticated webhooks, encrypted configuration and a persistent delivery outbox."""
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from cryptography.fernet import Fernet, MultiFernet
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def cipher():
    keys = os.environ.get('CONTRACTOR_ENCRYPTION_KEYS') or os.environ.get('CONTRACTOR_ENCRYPTION_KEY')
    if not keys:
        raise RuntimeError('CONTRACTOR_ENCRYPTION_KEY must be configured for contractor media and credentials')
    return MultiFernet([Fernet(key.strip().encode()) for key in keys.split(',')])


def protect(value):
    return cipher().encrypt(value.encode()).decode() if value else ''


def unprotect(value):
    return cipher().decrypt(value.encode()).decode() if value else ''


def require_https(value):
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(400, 'Integration URLs must use HTTPS without embedded credentials')
    return value.rstrip('/')


async def integration_config(db, public=False):
    raw = await db.settings.find_one({'key': 'delivery_centre_integration'}) or {}
    config = {
        'api_url': raw.get('api_url', ''),
        'outbound_webhook_url': raw.get('outbound_webhook_url', ''),
        'webhook_url': os.environ.get('CONTRACTOR_PUBLIC_API_URL', '').rstrip('/') + '/api/contractor/integrations/webhook',
        'polling_interval_minutes': max(5, min(15, int(raw.get('polling_interval_minutes', 10)))),
        'auto_poll_enabled': raw.get('auto_poll_enabled', True),
    }
    for field in ('api_key', 'webhook_secret'):
        value = unprotect(raw[field + '_encrypted']) if raw.get(field + '_encrypted') else raw.get(field, '')
        if field == 'webhook_secret' and not value:
            value = os.environ.get('CONTRACTOR_WEBHOOK_SECRET', '')
        config[field] = '' if public else value
        config[field + '_configured'] = bool(value)
    return config


async def save_config(db, payload, actor):
    try:
        interval = int(payload.get('polling_interval_minutes', 10))
    except (TypeError, ValueError):
        raise HTTPException(400, 'Polling interval must be 5–15 minutes')
    if not 5 <= interval <= 15 or not isinstance(payload.get('auto_poll_enabled', True), bool):
        raise HTTPException(400, 'Polling interval must be 5–15 minutes and enabled must be boolean')
    current = await integration_config(db)
    data = {'key': 'delivery_centre_integration',
        'api_url': require_https(payload.get('api_url', current['api_url']).strip()),
        'outbound_webhook_url': require_https(payload.get('outbound_webhook_url', current['outbound_webhook_url']).strip()),
        'polling_interval_minutes': interval, 'auto_poll_enabled': payload.get('auto_poll_enabled', True),
        'updated_at': datetime.now(timezone.utc), 'updated_by': actor.id}
    for field in ('api_key', 'webhook_secret'):
        data[field + '_encrypted'] = protect(payload.get(field) or current[field])
    await db.settings.update_one({'key': data['key']}, {'$set': data, '$unset': {'api_key': '', 'webhook_secret': ''}}, upsert=True)
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'action': 'contractor.integration.configure',
        'actor_id': actor.id, 'created_at': data['updated_at'], 'meta': {'fields': list(data)}})
    return {'success': True, 'config': await integration_config(db, public=True)}


async def authenticate_webhook(request, db):
    config = await integration_config(db)
    secret = config['webhook_secret']
    if not secret:
        raise HTTPException(503, 'Integration webhook secret is not configured')
    supplied = request.headers.get('authorization', '')
    if supplied.startswith('Bearer ') and hmac.compare_digest(supplied[7:], secret):
        return
    timestamp = request.headers.get('x-contractor-timestamp', '')
    signature = request.headers.get('x-contractor-signature', '').removeprefix('sha256=')
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError()
    except ValueError:
        raise HTTPException(401, 'Valid webhook authentication required')
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + await request.body(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, 'Invalid webhook signature')


async def queue_job_event(db, event, job_id, payload):
    now = datetime.now(timezone.utc)
    config = await integration_config(db)
    external = bool(config['outbound_webhook_url'])
    doc = {'id': uuid.uuid4().hex, 'event': event, 'job_id': job_id,
        'payload': jsonable_encoder(payload), 'status': 'pending' if external else 'local', 'attempts': 0,
        'next_attempt_at': now, 'created_at': now}
    await db.contractor_outbox.insert_one(doc)
    await db.contractor_jobs.update_one({'id': job_id}, {'$set': {'outbound_sync_status': 'pending' if external else 'local'}})
    return doc['id']


async def build_snapshot(db, job):
    snapshot = jsonable_encoder({k: v for k, v in job.items() if k != '_id'})
    for field, collection, sort in [('time_logs', 'contractor_time_logs', 'clock_in'),
                                    ('location_history', 'contractor_locations', 'recorded_at'),
                                    ('evidence', 'contractor_evidence', 'captured_at')]:
        snapshot[field] = []
        async for doc in getattr(db, collection).find({'job_id': job['id']}).sort(sort, 1):
            clean = {k: v for k, v in doc.items() if k not in ('_id', 'storage_path')}
            if field == 'evidence':
                clean['download_path'] = f"/api/contractor/jobs/{job['id']}/evidence/{doc['id']}/download"
            snapshot[field].append(jsonable_encoder(clean))
    snapshot['total_hours'] = round(job.get('total_time_seconds', 0) / 3600, 4)
    return snapshot


async def receive_event(db, request):
    from contractor_models import Job, JobCreate, JobTask, JobTemplate, JobStatusUpdate
    from services.contractor_service import create_job_from_delivery_client, sync_client_updates_to_contractor_jobs, log_job_activity
    from services.push_service import notify_urgent_issue_flagged, notify_contractor_job_assigned
    await authenticate_webhook(request, db)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Webhook body must be a JSON object')
    event = payload.get('event')
    data = payload.get('payload') or payload.get('data') or payload
    if not isinstance(data, dict):
        raise HTTPException(422, 'Webhook payload must be an object')
    client_id = payload.get('client_id') or data.get('client_id')
    vin = data.get('full_vin') or data.get('vin')
    query_parts = ([{'client_id': client_id}, {'dc_job_id': client_id}] if client_id else [])
    if vin:
        query_parts.append({'full_vin': vin})
    identity = data.get('job_id') or data.get('id')
    if identity:
        query_parts.append({'id': identity})
    creates = ('client.created', 'job.request', 'delivery.scheduled', 'accessory.added')
    updates = ('job.updated', 'client.updated', 'job.due_date_updated', 'due_date.updated', 'delivery_date.updated', 'job.urgent_flag', 'urgent.flagged', 'priority.urgent')
    if event not in (*creates, *updates, 'template.updated', 'job.snapshot', 'job.status_updated', 'job.issue_flagged', 'job.created'):
        raise HTTPException(400, 'Unsupported integration event')
    event_id = str(payload.get('event_id') or request.headers.get('idempotency-key') or hashlib.sha256(await request.body()).hexdigest())
    now = datetime.now(timezone.utc)
    try:
        await db.contractor_webhook_receipts.insert_one({'_id': hashlib.sha256(event_id.encode()).hexdigest(), 'event_id': event_id, 'status': 'processing', 'lease_until': now + timedelta(minutes=2)})
    except DuplicateKeyError:
        previous = await db.contractor_webhook_receipts.find_one({'event_id': event_id})
        if previous and previous.get('status') == 'success':
            return {'received': True, 'duplicate': True, 'event': event}
        claim = await db.contractor_webhook_receipts.find_one_and_update(
            {'event_id': event_id, '$or': [{'status': 'failed'}, {'lease_until': {'$lt': now}}]},
            {'$set': {'status': 'processing', 'lease_until': now + timedelta(minutes=2)}})
        if not claim:
            raise HTTPException(409, 'Event is already processing; retry later')
    try:
        jobs = [j async for j in db.contractor_jobs.find({'$or': query_parts})] if query_parts else []
        if event in ('job.snapshot', 'job.status_updated', 'job.issue_flagged', 'job.created'):
            # Delivery Centre receiver: persist the complete contractor state under
            # its job id, without generating another outbound event or sync loop.
            job_id = payload.get('job_id') or data.get('id')
            import re
            if job_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(job_id)):
                raise HTTPException(422, 'Invalid job identifier')
            if not job_id or not (client_id or data.get('full_vin')):
                raise HTTPException(422, 'Contractor updates require job_id and client_id or VIN')
            client = await db.clients.find_one({'id': client_id}) if client_id else None
            if not client and data.get('full_vin'):
                client = await db.clients.find_one({'vin': data['full_vin']})
            if not client:
                raise HTTPException(404, 'Delivery Centre client not found')
            client_id = client['id']
            await db.clients.update_one({'id': client_id}, {'$set': {f'contractor_jobs.{job_id}': data, 'updated_at': now}})
            action = 'contractor_state_received'
        elif event == 'template.updated':
            template = JobTemplate(**data)
            await db.contractor_job_templates.update_one({'id': template.id}, {'$set': template.model_dump(mode='json')}, upsert=True)
            action = 'template_updated'
        elif event in creates:
            if jobs:
                action = 'job_already_exists:' + jobs[0]['id']
            elif data.get('model_name') and data.get('full_vin'):
                job_input = JobCreate(**data)
                job = Job(**job_input.model_dump(exclude={'checklist'}), checklist=[JobTask(**t.model_dump()) for t in job_input.checklist or []], created_by_name='Delivery Centre', created_by_id='integration')
                # The event identity makes a retried insertion safe, even after a process crash.
                job.id = str(uuid.uuid5(uuid.NAMESPACE_URL, event_id))
                await db.contractor_jobs.update_one({'id': job.id}, {'$setOnInsert': job.model_dump(mode='json')}, upsert=True)
                action = 'job_created:' + job.id
                if job.assigned_contractor_id:
                    await notify_contractor_job_assigned(db, job.model_dump(mode='json'), job.assigned_contractor_id)
            elif client_id:
                job = await create_job_from_delivery_client(db, client_id, due_date=data.get('due_date') or data.get('delivery_date'), is_urgent=bool(data.get('is_urgent')))
                action = 'job_created:' + job.id
            else:
                raise HTTPException(422, 'New jobs require model_name and full_vin, or a Delivery Centre client_id')
        else:
            if not jobs:
                raise HTTPException(404, 'No matching contractor job')
            changes = dict(data)
            for source, target in [('notes', 'description'), ('vehicle', 'model_name'), ('vin', 'full_vin')]:
                if source in changes:
                    changes[target] = changes[source]
            if 'is_urgent' in changes:
                changes['priority'] = 'urgent' if changes['is_urgent'] else 'normal'
            elif 'priority' in changes:
                changes['is_urgent'] = changes['priority'] == 'urgent'
            if event in ('job.urgent_flag', 'urgent.flagged', 'priority.urgent'):
                changes.update(is_urgent=True, priority='urgent')
            if 'delivery_date' in changes:
                changes['delivery_date_time'] = changes['delivery_date']
                changes.setdefault('due_date', changes['delivery_date'])
            from contractor_models import JobUpdate
            changes = JobUpdate(**changes).model_dump(exclude_unset=True, mode='json')
            # Integration cannot forge local linkage or sync bookkeeping.
            for field in ('sync_status', 'sync_error', 'last_synced_at', 'dc_job_id'):
                changes.pop(field, None)
            changes['updated_at'] = now
            if changes.get('status') in ('completed', 'returned', 'invoiced'):
                changes[changes['status'] + '_at'] = now
            if changes.get('full_vin'):
                changes['last_6_vin'] = changes['full_vin'][-6:]
            for job in jobs:
                await db.contractor_jobs.update_one({'id': job['id']}, {'$set': changes})
                await log_job_activity(db, job['id'], 'integration_sync', 'integration', 'Delivery Centre', 'Delivery Centre updated job', {'event_id': event_id, 'fields': list(changes)})
                if changes.get('is_urgent') and not job.get('is_urgent'):
                    await notify_urgent_issue_flagged(db, {**job, **changes}, data.get('description') or 'Urgent Delivery Centre request', 'Delivery Centre')
            action = 'updated:' + str(len(jobs))
        await db.contractor_webhook_receipts.update_one({'event_id': event_id}, {'$set': {'status': 'success', 'processed_at': now}})
        await db.contractor_sync_logs.insert_one({'id': uuid.uuid4().hex, 'event': event, 'client_id': client_id, 'job_id': jobs[0]['id'] if jobs else (job.id if event in creates else None),
            'direction': 'inbound_to_contractor', 'status': 'success', 'synced_at': now})
        return {'received': True, 'event': event, 'action': action}
    except Exception as exc:
        await db.contractor_webhook_receipts.update_one({'event_id': event_id}, {'$set': {'status': 'failed'}})
        await db.contractor_sync_logs.insert_one({'id': uuid.uuid4().hex, 'event': event, 'client_id': client_id,
            'direction': 'inbound_to_contractor', 'status': 'failed', 'error_message': type(exc).__name__, 'synced_at': now})
        from pydantic import ValidationError
        if isinstance(exc, (ValidationError, ValueError)):
            raise HTTPException(422, 'Invalid integration payload') from exc
        raise


async def poll_remote_requests(db, config):
    """Page through the authenticated Delivery Centre request feed, without truncation."""
    from contractor_models import Job, JobTask
    now = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            skip = 0
            while True:
                response = await client.get(config['api_url'] + '/api/contractor/integrations/requests',
                    params={'skip': skip, 'limit': 200}, headers={'Authorization': 'Bearer ' + config['api_key']})
                response.raise_for_status()
                result = response.json()
                records = result.get('requests', [])
                for record in records:
                    client_id = record.get('client_id')
                    if not client_id:
                        raise ValueError('Remote request has no client_id')
                    existing = await db.contractor_jobs.find_one({'dc_job_id': client_id})
                    if existing:
                        updates = {k: record[k] for k in ('model_name', 'full_vin', 'last_6_vin', 'delivery_date_time', 'due_date', 'description', 'is_urgent', 'priority') if k in record}
                        await db.contractor_jobs.update_one({'id': existing['id']}, {'$set': updates})
                    else:
                        job = Job(**record)
                        job.id = str(uuid.uuid5(uuid.NAMESPACE_URL, config['api_url'] + ':' + client_id))
                        await db.contractor_jobs.update_one({'id': job.id}, {'$setOnInsert': job.model_dump(mode='json')}, upsert=True)
                next_skip = result.get('next_skip')
                if next_skip is None:
                    break
                if not isinstance(next_skip, int) or next_skip <= skip:
                    raise ValueError('Invalid polling cursor')
                skip = next_skip
        await db.settings.update_one({'key': 'contractor_integration_health'}, {'$set': {'checked_at': now, 'reachable': True}}, upsert=True)
    except Exception:
        await db.settings.update_one({'key': 'contractor_integration_health'}, {'$set': {'checked_at': now, 'reachable': False}}, upsert=True)
        await db.contractor_sync_logs.insert_one({'id': uuid.uuid4().hex, 'direction': 'inbound_to_contractor',
            'event': 'poll.failed', 'status': 'failed', 'synced_at': now, 'error_message': 'Remote request feed unavailable'})
        raise


async def drain_outbox(db, config=None):
    config = config or await integration_config(db)
    url = config['outbound_webhook_url']
    if not url:
        return
    if not config['webhook_secret']:
        raise RuntimeError('A webhook secret is required for outbound delivery')
    for _ in range(50):
        now = datetime.now(timezone.utc)
        item = await db.contractor_outbox.find_one_and_update(
            {'$or': [{'status': {'$in': ['pending', 'retry']}, 'next_attempt_at': {'$lte': now}},
                     {'status': 'sending', 'lease_until': {'$lt': now}}]},
            {'$set': {'status': 'sending', 'lease_until': now + timedelta(minutes=2)}, '$inc': {'attempts': 1}},
            sort=[('created_at', 1)], return_document=ReturnDocument.AFTER)
        if not item:
            break
        try:
            job = await db.contractor_jobs.find_one({'id': item['job_id']})
            payload = {'event_id': item['id'], 'event': item['event'], 'job_id': item['job_id'],
                'payload': await build_snapshot(db, job) if job else item['payload']}
            body = json.dumps(jsonable_encoder(payload), separators=(',', ':')).encode()
            stamp = str(int(time.time()))
            signature = hmac.new(config['webhook_secret'].encode(), stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
            headers = {'Content-Type': 'application/json', 'Idempotency-Key': item['id'],
                'X-Contractor-Timestamp': stamp, 'X-Contractor-Signature': 'sha256=' + signature}
            if config['api_key']:
                headers['Authorization'] = 'Bearer ' + config['api_key']
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(url, content=body, headers=headers)
                response.raise_for_status()
            await db.contractor_outbox.update_one({'id': item['id']}, {'$set': {'status': 'delivered', 'delivered_at': now, 'last_error': None}})
            await db.contractor_jobs.update_one({'id': item['job_id']}, {'$set': {'outbound_sync_status': 'delivered', 'outbound_last_synced_at': now, 'outbound_sync_error': None, 'sync_status': 'synced', 'sync_error': None, 'last_synced_at': now}})
            status, error = 'success', None
        except Exception as exc:
            # Keep every failed event available for retry; no retry limit silently discards work.
            error = type(exc).__name__ + ': delivery failed'
            delay = min(3600, 15 * (2 ** min(item.get('attempts', 1), 8)))
            await db.contractor_outbox.update_one({'id': item['id']}, {'$set': {'status': 'retry',
                'last_error': error, 'next_attempt_at': now + timedelta(seconds=delay)}})
            await db.contractor_jobs.update_one({'id': item['job_id']}, {'$set': {'outbound_sync_status': 'failed', 'outbound_sync_error': error}})
            status = 'failed'
            try:
                from services.push_service import broadcast_role_push
                for role in ('admin', 'super_admin'):
                    await broadcast_role_push(db, role, 'Delivery Centre sync failed', 'An update is queued for automatic retry.', {'url': '/sync'})
            except Exception:
                pass
        await db.contractor_sync_logs.insert_one({'id': uuid.uuid4().hex, 'job_id': item['job_id'],
            'direction': 'outbound_to_delivery', 'event': item['event'], 'status': status,
            'error_message': error, 'synced_at': now})


async def run_integration_worker():
    import asyncio
    import logging
    from db import get_db
    from services.contractor_service import poll_delivery_centre_integration
    last_poll = 0
    while True:
        try:
            db = get_db()
            config = await integration_config(db)
            if config['auto_poll_enabled'] and time.monotonic() - last_poll >= config['polling_interval_minutes'] * 60:
                await poll_delivery_centre_integration(db)
                last_poll = time.monotonic()
            await drain_outbox(db, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception('Contractor integration worker failed')
        await asyncio.sleep(15)
