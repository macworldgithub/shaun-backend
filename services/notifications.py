import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from db import get_db
from services.email import send_email
from services.retention import enforce_document_retention
from services.mailbox import fetch_unread_messages
from services.storage import put_bytes
from models import ClientDocument

log = logging.getLogger('notifications')
MELBOURNE = ZoneInfo('Australia/Melbourne')


def _days_until(value: str, today):
    try:
        return (datetime.strptime(value, '%Y-%m-%d').date() - today).days
    except (TypeError, ValueError):
        return None


async def _already_sent(db, key: str, day: str) -> bool:
    return await db.audit.find_one({'action': 'notification.sent', 'meta.key': key, 'meta.day': day}) is not None


async def _send_alert(db, client, key: str, message: str, day: str):
    if await _already_sent(db, key, day):
        return
    recipient = os.environ.get('NOTIFICATION_EMAIL') or os.environ.get('SMTP_FROM')
    if not recipient:
        return
    try:
        await send_email(recipient=recipient, subject='BYD Delivery Centre workflow alert', body=message)
    except Exception:
        log.exception('Could not send scheduled workflow alert for %s', client.get('id'))
        return
    await db.audit.insert_one({'id': f'notification-{key}-{day}', 'actor_id': None, 'actor_email': None, 'action': 'notification.sent', 'entity': 'client', 'entity_id': client.get('id'), 'meta': {'key': key, 'day': day, 'message': message}, 'created_at': datetime.now(timezone.utc)})


async def run_due_notifications(now=None):
    now = now or datetime.now(MELBOURNE)
    today = now.date()
    day = today.isoformat()
    db = get_db()
    async for client in db.clients.find({'stage': {'$ne': 'Delivered'}}):
        name = client.get('name', 'Client')
        trade_days = _days_until(client.get('trade_in_valid_until'), today)
        if trade_days in (10, 5, 1, 0):
            await _send_alert(db, client, f'trade-{client.get("id")}-{trade_days}', f'{name}: trade-in valuation expires in {trade_days} day(s).', day)
        for offer_id in client.get('linked_offer_ids', []):
            offer = await db.offers.find_one({'id': offer_id, 'active': True})
            if not offer:
                continue
            for field, label in (('order_to', 'order window'), ('deliver_by', 'delivery deadline')):
                offer_days = _days_until(offer.get(field), today)
                if offer_days in (14, 7, 3, 1, 0):
                    await _send_alert(db, client, f'offer-{client.get("id")}-{offer_id}-{field}-{offer_days}', f'{name}: {offer.get("name", "Offer")} {label} closes in {offer_days} day(s).', day)
                    # Claim pack incomplete check — alert if no complete EFT/claim document while window closing
                    if offer_days in (7, 3, 1, 0):
                        claim_types = set(offer.get('claim_doc_templates') or ([offer.get('claim_doc_template')] if offer.get('claim_doc_template') else []))
                        if claim_types or offer.get('cash_or_product') in ('cash', 'both'):
                            docs = client.get('documents', [])
                            has_complete_claim = any(
                                d.get('document_type') in ('EFT form', 'Bank statement') and d.get('status') == 'complete'
                                for d in docs
                            )
                            if not has_complete_claim:
                                await _send_alert(
                                    db, client,
                                    f'claim-pack-incomplete-{client.get("id")}-{offer_id}-{offer_days}',
                                    f'{name}: {offer.get("name", "Offer")} claim pack is incomplete with {offer_days} day(s) remaining on the {label}.',
                                    day,
                                )


async def refresh_catalogue_on_first_of_month(now=None):
    now = now or datetime.now(MELBOURNE)
    if now.day != 1:
        return
    db = get_db()
    month = now.strftime('%Y-%m')
    if await db.audit.find_one({'action': 'offers.monthly_refresh', 'meta.month': month}):
        return
    refreshed_at = datetime.now(timezone.utc)
    # Collect snapshot before refresh for change summary
    active_offers = []
    async for offer in db.offers.find({'active': True}):
        offer.pop('_id', None)
        active_offers.append(offer)
    result = await db.offers.update_many({'active': True}, {'$set': {'last_refreshed_at': refreshed_at}})
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': None, 'actor_email': None, 'action': 'offers.monthly_refresh', 'entity': 'offer_catalogue', 'entity_id': None, 'meta': {'month': month, 'active_offers': result.modified_count}, 'created_at': refreshed_at})
    # Send monthly catalogue summary email
    recipient = os.environ.get('NOTIFICATION_EMAIL') or os.environ.get('SMTP_FROM')
    if recipient and active_offers:
        offer_lines = '\n'.join([f"  - {o.get('name')} (order window: {o.get('order_from', '?')} to {o.get('order_to', '?')}, deliver by: {o.get('deliver_by', '?')})" for o in active_offers])
        summary_body = (
            f'BYD Delivery Centre — Monthly Offer Catalogue Refresh\n'
            f'Month: {month}\n'
            f'Active offers ({len(active_offers)}):\n'
            f'{offer_lines}\n\n'
            f'Please review the offer catalogue in Delivery Centre and confirm any additions or expirations.\n'
        )
        try:
            await send_email(recipient=recipient, subject=f'[BYD DC] Offer catalogue refreshed for {month}', body=summary_body)
        except Exception:
            log.exception('Could not send monthly catalogue summary email')


async def poll_inbound_mail():
    messages = await asyncio.to_thread(fetch_unread_messages)
    if not messages:
        return 0
    db = get_db()
    attached = 0
    for message in messages:
        client = None
        matched_by = None
        if message.get('po_number'):
            client = await db.clients.find_one({'po_number': message['po_number']})
            matched_by = 'po_number' if client else None
        if not client and message.get('sender'):
            client = await db.clients.find_one({'email': message['sender']})
            matched_by = 'email' if client else None
        if not client and message.get('phone'):
            client = await db.clients.find_one({'phone': message['phone']})
            matched_by = 'phone' if client else None
        if not client:
            await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': None, 'actor_email': message.get('sender'), 'action': 'document.receive.unmatched', 'entity': 'inbound_mail', 'entity_id': None, 'meta': {'subject': message.get('subject'), 'sender': message.get('sender')}, 'created_at': datetime.now(timezone.utc)})
            continue
        for attachment in message.get('attachments', []):
            safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', attachment['filename'])
            key = f'{client["id"]}/{safe_name}'
            await put_bytes(key, attachment['data'], attachment.get('content_type'))
            document = ClientDocument(client_id=client['id'], document_type=_classify_attachment(attachment['filename']), status='partial', file_name=attachment['filename'], source='email', storage_path=key, content_type=attachment.get('content_type'), size_bytes=len(attachment['data']), notes='Automatically received from tracked mailbox')
            await db.clients.update_one({'id': client['id']}, {'$push': {'documents': document.model_dump(mode='json')}, '$set': {'updated_at': datetime.now(timezone.utc)}})
            await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': None, 'actor_email': message.get('sender'), 'action': 'document.receive.mail_attach', 'entity': 'client', 'entity_id': client['id'], 'meta': {'document_id': document.id, 'matched_by': matched_by, 'file_name': attachment['filename'], 'document_type': document.document_type}, 'created_at': datetime.now(timezone.utc)})
            attached += 1
    return attached


def _classify_attachment(filename: str) -> str:
    value = filename.lower()
    if 'atr' in value or 'authority' in value:
        return 'ATR signed'
    if 'licence' in value or 'license' in value:
        return 'Licence front' if 'front' in value else 'Licence back' if 'back' in value else 'Other'
    if 'eft' in value or 'refund' in value:
        return 'EFT form'
    if 'bank' in value or 'statement' in value:
        return 'Bank statement'
    if 'handover' in value or 'checklist' in value:
        return 'Handover checklist'
    return 'Other'


async def scheduled_workflow_worker():
    while True:
        try:
            now = datetime.now(MELBOURNE)
            await run_due_notifications(now)
            await refresh_catalogue_on_first_of_month(now)
            await poll_inbound_mail()
            if now.hour == 3:
                await enforce_document_retention()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Scheduled workflow run failed')
        await asyncio.sleep(3600)
