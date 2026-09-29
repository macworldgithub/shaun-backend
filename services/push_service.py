import os
import io
import json
import base64
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import webpush, WebPushException

log = logging.getLogger(__name__)

# Cached VAPID state
_CACHED_VAPID: Optional[Vapid] = None
_CACHED_PUB_B64: Optional[str] = None
_CACHED_CLAIMS_EMAIL: str = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@fairfield-deliverycentre.com.au')


async def get_or_init_vapid(db) -> tuple[Vapid, str]:
    """
    Returns (vapid_instance, public_key_b64).
    Initializes from environment variables, or persists in db.app_settings so keys remain stable.
    """
    global _CACHED_VAPID, _CACHED_PUB_B64
    if _CACHED_VAPID is not None and _CACHED_PUB_B64 is not None:
        return _CACHED_VAPID, _CACHED_PUB_B64

    # 1. Check environment variables
    env_priv = os.environ.get('VAPID_PRIVATE_KEY')
    env_pub = os.environ.get('VAPID_PUBLIC_KEY')
    if env_priv and env_pub:
        try:
            vapid = Vapid.from_pem(env_priv.encode('utf-8'))
            _CACHED_VAPID = vapid
            _CACHED_PUB_B64 = env_pub
            return _CACHED_VAPID, _CACHED_PUB_B64
        except Exception as e:
            log.warning("Could not parse VAPID from environment: %s. Falling back to DB.", e)

    # 2. Check DB app_settings
    settings = await db.app_settings.find_one({'key': 'vapid_keys'})
    if settings and settings.get('private_pem') and settings.get('public_key_b64'):
        try:
            pem_bytes = settings['private_pem'].encode('utf-8')
            vapid = Vapid.from_pem(pem_bytes)
            _CACHED_VAPID = vapid
            _CACHED_PUB_B64 = settings['public_key_b64']
            return _CACHED_VAPID, _CACHED_PUB_B64
        except Exception as e:
            log.warning("Could not parse stored VAPID keys: %s. Generating fresh pair.", e)

    # 3. Generate a new keypair
    vapid = Vapid()
    vapid.generate_keys()
    raw_pub = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint
    )
    pub_b64 = base64.urlsafe_b64encode(raw_pub).decode('utf-8').rstrip('=')
    priv_pem = vapid.private_pem().decode('utf-8')

    await db.app_settings.update_one(
        {'key': 'vapid_keys'},
        {
            '$set': {
                'key': 'vapid_keys',
                'private_pem': priv_pem,
                'public_key_b64': pub_b64,
                'claims_email': _CACHED_CLAIMS_EMAIL,
                'created_at': datetime.now(timezone.utc)
            }
        },
        upsert=True
    )

    _CACHED_VAPID = vapid
    _CACHED_PUB_B64 = pub_b64
    log.info("Initialized and persisted fresh VAPID keypair for Web Push")
    return _CACHED_VAPID, _CACHED_PUB_B64


async def get_vapid_public_key(db) -> str:
    """Returns the base64url encoded VAPID public key for browser subscription."""
    _, pub_key = await get_or_init_vapid(db)
    return pub_key


async def save_push_subscription(
    db,
    user_id: str,
    role: str,
    subscription_dict: dict,
    device_info: Optional[str] = None
) -> dict:
    """Saves or updates a Web Push subscription in MongoDB."""
    endpoint = subscription_dict.get('endpoint')
    if not endpoint:
        raise ValueError("Subscription must have an endpoint")

    doc = {
        'user_id': user_id,
        'role': role,
        'subscription': subscription_dict,
        'device_info': device_info,
        'updated_at': datetime.now(timezone.utc)
    }

    await db.push_subscriptions.update_one(
        {'endpoint': endpoint},
        {
            '$set': doc,
            '$setOnInsert': {'created_at': datetime.now(timezone.utc)}
        },
        upsert=True
    )
    return doc


async def remove_push_subscription(db, user_id: str, endpoint: Optional[str] = None) -> int:
    """Deletes push subscription(s) for a user."""
    query: Dict[str, Any] = {'user_id': user_id}
    if endpoint:
        query['endpoint'] = endpoint
    res = await db.push_subscriptions.delete_many(query)
    return res.deleted_count


async def send_web_push(
    db,
    user_id: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    tag: Optional[str] = None
) -> int:
    """
    Sends a Web Push notification to all active devices registered by user_id.
    Removes expired subscriptions (HTTP 404 / 410).
    """
    vapid, _ = await get_or_init_vapid(db)
    payload_json = json.dumps({
        'title': title,
        'body': body,
        'tag': tag or 'byd-contractor-notice',
        'data': data or {},
        'icon': '/icons/icon-192.png',
        'badge': '/icons/badge-72.png',
        'timestamp': int(datetime.now(timezone.utc).timestamp() * 1000)
    })

    claims = {'sub': _CACHED_CLAIMS_EMAIL}
    success_count = 0

    cursor = db.push_subscriptions.find({'user_id': user_id})
    async for sub_doc in cursor:
        sub_info = sub_doc.get('subscription')
        if not sub_info:
            continue
        try:
            webpush(
                subscription_info=sub_info,
                data=payload_json,
                vapid_private_key=vapid,
                vapid_claims=claims,
                timeout=5
            )
            success_count += 1
        except WebPushException as ex:
            status = getattr(ex.response, 'status_code', None)
            if status in (404, 410):
                log.info("Removing expired push subscription for endpoint %s", sub_doc.get('endpoint'))
                await db.push_subscriptions.delete_one({'_id': sub_doc['_id']})
            else:
                log.warning("WebPush error for user %s: %s (status=%s)", user_id, ex, status)
        except Exception as e:
            log.warning("Push send exception for user %s: %s", user_id, e)

    return success_count


async def broadcast_role_push(
    db,
    role: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    tag: Optional[str] = None
) -> int:
    """Broadcasts a push notification to all users matching a role (e.g. 'admin' or 'super_admin')."""
    # Find all users with this role
    matching_users = []
    async for u in db.users.find({'role': role}):
        matching_users.append(u.get('id') or str(u.get('_id')))

    total_sent = 0
    for uid in matching_users:
        sent = await send_web_push(db, uid, title, body, data=data, tag=tag)
        total_sent += sent

    # Also check subscriptions directly registered with role
    async for s in db.push_subscriptions.find({'role': role}):
        uid = s.get('user_id')
        if uid not in matching_users:
            sent = await send_web_push(db, uid, title, body, data=data, tag=tag)
            total_sent += sent

    return total_sent


# ---------------------------------------------------------------------------
# Business event triggers
# ---------------------------------------------------------------------------

async def notify_contractor_job_assigned(db, job: dict, contractor_id: str):
    """Notify contractor when a job is assigned to them."""
    if not contractor_id:
        return
    model_name = job.get('model_name', 'Vehicle')
    vin = job.get('last_6_vin') or job.get('full_vin', '')
    priority = job.get('priority', 'normal')
    urgent_prefix = '🚨 [URGENT] ' if job.get('is_urgent') or priority == 'urgent' else ''

    title = f"{urgent_prefix}New Job Assigned: {model_name}"
    body = f"VIN: {vin} | Due: {job.get('due_date') or 'ASAP'} | Location: {job.get('site_location') or 'Fairfield'}"
    await send_web_push(
        db,
        user_id=contractor_id,
        title=title,
        body=body,
        data={'job_id': job.get('id'), 'action': 'open_job'},
        tag=f"job-{job.get('id')}-assign"
    )


async def notify_urgent_issue_flagged(db, job: dict, description: str, reporter_name: str):
    """Notify all admins when a contractor flags an urgent issue."""
    model_name = job.get('model_name', 'Vehicle')
    vin = job.get('last_6_vin') or job.get('full_vin', '')

    title = f"🚨 Issue Flagged: {model_name} ({vin})"
    body = f"{reporter_name}: {description[:120]}"
    data = {'job_id': job.get('id'), 'issue': True, 'action': 'open_job'}

    # Broadcast to admins and super_admins
    await broadcast_role_push(db, 'admin', title, body, data=data, tag=f"issue-{job.get('id')}")
    await broadcast_role_push(db, 'super_admin', title, body, data=data, tag=f"issue-{job.get('id')}")


async def notify_due_date_approaching(db, job: dict, days_left: int):
    """Notify assigned contractor and admins when job due date is near or overdue."""
    model_name = job.get('model_name', 'Vehicle')
    vin = job.get('last_6_vin') or job.get('full_vin', '')

    if days_left < 0:
        title = f"⚠️ Overdue Job: {model_name} ({vin})"
        body = f"Job is {abs(days_left)} day(s) overdue! Status: {job.get('status')}"
    elif days_left == 0:
        title = f"⏰ Job Due Today: {model_name} ({vin})"
        body = f"Job is due today! Status: {job.get('status')}"
    else:
        title = f"📅 Job Due in {days_left} Day(s): {model_name} ({vin})"
        body = f"Due: {job.get('due_date')} | Contractor: {job.get('assigned_contractor_name') or 'Unassigned'}"

    data = {'job_id': job.get('id'), 'action': 'open_job'}
    cid = job.get('assigned_contractor_id')
    if cid:
        await send_web_push(db, cid, title, body, data=data, tag=f"due-{job.get('id')}-{days_left}")

    # For overdue or due today, alert admins
    if days_left <= 0:
        await broadcast_role_push(db, 'admin', title, body, data=data, tag=f"admin-due-{job.get('id')}-{days_left}")
