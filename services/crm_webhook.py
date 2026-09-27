"""
crm_webhook.py – Delivery Centre to Sales CRM Webhook Dispatcher (§5.2, §5.8, §7.3, §8.3, AC-3, AC-9)
Dispatches asynchronous webhooks to the Sales CRM backend when comments or stages are updated.
"""
import os
import uuid
import logging
import asyncio
import httpx

log = logging.getLogger('crm_webhook')

CRM_WEBHOOK_URL = os.environ.get(
    'CRM_WEBHOOK_URL',
    os.environ.get('CRM_BACKEND_URL', 'http://localhost:5000') + '/api/webhooks/delivery'
)

async def _send_webhook_async(payload: dict):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(CRM_WEBHOOK_URL, json=payload)
            if resp.status_code >= 400:
                log.warning("CRM webhook returned %s: %s", resp.status_code, resp.text)
            else:
                log.info("CRM webhook dispatched successfully: event=%s", payload.get('event'))
    except Exception as exc:
        log.debug("CRM webhook dispatch failed (non-blocking): %s", exc)

def dispatch_crm_webhook(event: str, client_id: str, payload_data: dict, crm_customer_id: str = None):
    """
    Fire-and-forget async webhook dispatcher to CRM backend.
    """
    webhook_payload = {
        "event_id": f"EVT-DC-{uuid.uuid4().hex[:12].upper()}",
        "event": event,
        "client_id": client_id,
        "crm_customer_id": crm_customer_id,
        "payload": payload_data,
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_webhook_async(webhook_payload))
    except RuntimeError:
        pass
