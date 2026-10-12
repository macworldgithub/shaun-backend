"""Shared SMS history from Lead Centre threads and Delivery Centre messages."""
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from db import get_db, get_lead_db

def phone_key(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return '61' + digits[1:] if re.fullmatch(r'0\d{9}', digits) else '61' + digits if re.fullmatch(r'4\d{8}', digits) else digits

def site_key(value):
    value = re.sub(r'^byd\s+', '', str(value or '').lower())
    return re.sub(r'^melbourne(cbd|city)$', 'melbourne', re.sub(r'vic$', '', re.sub(r'[^a-z0-9]', '', value)))

def allowed_site(user, site):
    if user.role == 'contractor':
        return False
    if user.locked_site:
        return bool(site_key(site)) and site_key(site) == site_key(user.locked_site)
    if user.role in ('super_admin', 'admin'):
        return True
    return bool(site_key(user.active_site)) and site_key(site) == site_key(user.active_site)

def assert_message_access(user, doc):
    if not doc or not allowed_site(user, doc.get('site_location') or doc.get('site')):
        raise HTTPException(404, 'Client not found')

def merge_rows(rows):
    result = {}
    ranks = {'failed': 6, 'error': 6, 'delivered': 5, 'received': 5, 'sent': 3, 'success': 3, 'simulated': 2, 'queued': 1}
    for row in rows:
        key = (row['direction'], row.get('provider_message_id') or row['id'])
        previous = result.get(key)
        if not previous:
            result[key] = row
        elif ranks.get(row.get('status'), 0) > ranks.get(previous.get('status'), 0):
            previous['status'] = row['status']
    def timestamp(row):
        value = row.get('sent_at')
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not value:
            return 0
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    return sorted(result.values(), key=timestamp, reverse=True)

async def history(user, client_id=None, requested_site=None, limit=200, phone=None):
    db = get_db()
    lead_db = get_lead_db()
    from services.sms_event import phone_pattern
    target = None
    if client_id:
        target = await db.clients.find_one({'id': client_id})
        if not target:
            from bson import ObjectId
            if ObjectId.is_valid(client_id):
                target = await db.clients.find_one({'_id': ObjectId(client_id)})
        assert_message_access(user, target)
    focus = target.get('phone') if target else phone
    query = {'phone': {'$regex': phone_pattern(focus)}} if focus else {}
    clients = await db.clients.find(query, {'id': 1, 'name': 1, 'phone': 1, 'site_location': 1, 'site': 1}).to_list(None)
    for c in clients:
        c['site_location'] = c.get('site_location') or c.get('site')
    if target:
        target['site_location'] = target.get('site_location') or target.get('site')
    conversations = await lead_db.conversations.find(query).to_list(None)
    leads = await lead_db.leads.find(query, {'phone': 1, 'dealer': 1, 'name': 1}).to_list(None)
    records = clients + [{**l, 'site_location': l.get('dealer')} for l in leads]
    messages = await db.messages.find(query).to_list(None)
    out = []
    def visible(phone, site):
        return (allowed_site(user, site)
                and (not requested_site or requested_site == 'All Sites' or site_key(site) == site_key(requested_site))
                and (not focus or phone_key(phone) == phone_key(focus)))
    for msg in messages:
        client = next((c for c in clients if msg.get('client_id') and msg['client_id'] in (c.get('id'), str(c.get('_id')))), None)
        matched = [r for r in records if phone_key(r.get('phone')) and phone_key(r.get('phone')) == phone_key(msg.get('phone'))]
        sites = {site_key(r.get('site_location')) for r in matched if r.get('site_location')}
        convo_sites = {c.get('dealer') for c in conversations if phone_key(c.get('phone')) == phone_key(msg.get('phone')) and c.get('dealer')}
        correlation = (msg.get('provider_response') or {}).get('original_message_id') if msg.get('direction') == 'inbound' else None
        original = next((row for row in messages if row.get('direction') == 'outbound' and str(row.get('provider_message_id')) == str(correlation)), None) if correlation else None
        original_client = next((c for c in clients if (original or {}).get('client_id') in (c.get('id'), str(c.get('_id')))), None) if (original or {}).get('client_id') else None
        correlated_thread = next((c for c in conversations if any(m.get('sender') != 'user' and str(m.get('id')) == str(correlation) for m in c.get('messages', []))), None) if correlation else None
        source_thread = next((c for c in conversations if any(str(m.get('id')) == str(msg.get('provider_message_id')) for m in c.get('messages', []))), None) if msg.get('provider_message_id') else None
        site = msg.get('site') or (original or {}).get('site') or (original_client or {}).get('site_location') or (correlated_thread or {}).get('dealer') or (source_thread or {}).get('dealer') or (client or {}).get('site_location')
        if not site and len(sites) == 1:
            site = next(r.get('site_location') for r in matched if r.get('site_location'))
        if not site and len(sites) <= 1 and len({site_key(s) for s in convo_sites}) == 1:
            site = next(iter(convo_sites))
        if visible(msg.get('phone'), site):
            msg['id'] = str(msg.get('id') or msg['_id'])
            msg.pop('_id', None)
            msg['client_id'] = (target or client or {}).get('id') or msg.get('client_id')
            msg['phone'] = '+' + phone_key(msg.get('phone'))
            msg['site'] = site
            out.append(msg)
    for convo in conversations:
        if not visible(convo.get('phone'), convo.get('dealer')):
            continue
        client = next((c for c in clients if phone_key(c.get('phone')) == phone_key(convo.get('phone')) and site_key(c.get('site_location')) == site_key(convo.get('dealer'))), None)
        for msg in convo.get('messages', []):
            if msg.get('sender') == 'system':
                continue
            out.append({
                'id': str(msg['id']), 'client_id': (target or client or {}).get('id'),
                'client_name': convo.get('prospectName'), 'site': convo.get('dealer'), 'phone': '+' + phone_key(convo.get('phone')),
                'body': msg['text'], 'direction': 'inbound' if msg['sender'] == 'user' else 'outbound',
                'status': msg.get('status', 'sent'), 'provider_message_id': str(msg['id']),
                'sent_at': msg.get('createdAt') or convo.get('createdAt') or datetime.now(timezone.utc),
            })
    return merge_rows(out)[:limit]
