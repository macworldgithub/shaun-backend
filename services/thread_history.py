"""Shared SMS history from Lead Centre threads and Delivery Centre messages."""
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from db import get_db, get_lead_db
import logging
import asyncio

def phone_key(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return '61' + digits[1:] if re.fullmatch(r'0\d{9}', digits) else '61' + digits if re.fullmatch(r'4\d{8}', digits) else digits

def site_key(value):
    value = re.sub(r'^byd\s+', '', str(value or '').lower())
    return re.sub(r'^melbourne(cbd|city)$', 'melbourne', re.sub(r'vic$', '', re.sub(r'[^a-z0-9]', '', value)))

def allowed_site(user, site):
    if user.role == 'contractor':
        return False
    locked = getattr(user, 'request_site', None) or user.locked_site
    if locked:
        return bool(site_key(site)) and site_key(site) == site_key(locked)
    sites = getattr(user, 'allowed_sites', None)
    if sites is not None and not getattr(user, 'all_sites', False):
        return bool(site_key(site)) and any(site_key(site) == site_key(s) for s in sites)
    if user.role in ('super_admin', 'admin', 'site_admin', 'delivery_specialist'):
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
    try:
        lead_db = get_lead_db()
    except RuntimeError:
        lead_db = None
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
    async def shared_conversations():
        if lead_db is None:
            return []
        try:
            return await asyncio.wait_for(lead_db.conversations.find(query, {
                'phone': 1, 'dealer': 1, 'prospectName': 1, 'createdAt': 1,
                'messages.id': 1, 'messages.sender': 1, 'messages.text': 1,
                'messages.status': 1, 'messages.createdAt': 1,
            }).to_list(None), timeout=1.5)
        except Exception:
            logging.getLogger(__name__).warning('Shared history unavailable; serving Delivery Centre messages', exc_info=True)
            return []

    messages, conversations = await asyncio.gather(
        db.messages.find(query).to_list(None), shared_conversations(),
    )
    phones = {phone_key(row.get('phone')) for row in messages + conversations if phone_key(row.get('phone'))}
    if not messages and not conversations:
        return []
    # Resolve ownership only for phones actually present in this inbox. Loading
    # every customer and lead added seconds of database transfer on each poll.
    contact_clauses = [
        {'phone': {'$regex': phone_pattern(phone)}} for phone in sorted(phones)
    ]
    client_ids = list({message['client_id'] for message in messages if message.get('client_id')})
    if client_ids:
        from bson import ObjectId
        contact_clauses.append({'id': {'$in': client_ids}})
        object_ids = [ObjectId(value) for value in client_ids if ObjectId.is_valid(value)]
        if object_ids:
            contact_clauses.append({'_id': {'$in': object_ids}})
    contacts_query = query if focus else {'$or': contact_clauses}

    async def shared_contacts():
        if lead_db is None or not phones:
            return []
        try:
            return await asyncio.wait_for(lead_db.leads.find({'$or': [
                {'phone': {'$regex': phone_pattern(phone)}} for phone in sorted(phones)
            ]}, {
                'phone': 1, 'dealer': 1, 'name': 1,
            }).to_list(None), timeout=1.5)
        except Exception:
            logging.getLogger(__name__).warning('Shared contact lookup unavailable', exc_info=True)
            return []

    clients, leads = await asyncio.gather(
        db.clients.find(contacts_query, {'id': 1, 'name': 1, 'phone': 1, 'site_location': 1, 'site': 1}).to_list(None) if contact_clauses else asyncio.sleep(0, result=[]),
        shared_contacts(),
    )
    # Joining whole inboxes used to repeatedly scan every contact and every
    # message array for each SMS. Run the linear-time join off the event loop.
    return await asyncio.to_thread(_merge_history, user, target, focus, requested_site,
                                   limit, clients, messages, conversations, leads)


def _merge_history(user, target, focus, requested_site, limit, clients, messages, conversations, leads):
    clients_by_id, clients_by_phone_site = {}, {}
    records_by_phone, sites_by_phone, convo_sites_by_phone = {}, {}, {}
    originals_by_id, threads_by_id, outbound_threads_by_id = {}, {}, {}
    for client in clients:
        client['site_location'] = client.get('site_location') or client.get('site')
        for identifier in (client.get('id'), str(client.get('_id'))):
            if identifier:
                clients_by_id.setdefault(identifier, client)
        clients_by_phone_site.setdefault((phone_key(client.get('phone')), site_key(client.get('site_location'))), client)
    if target:
        target['site_location'] = target.get('site_location') or target.get('site')
    records = clients + [{**lead, 'site_location': lead.get('dealer')} for lead in leads]
    for record in records:
        phone = phone_key(record.get('phone'))
        if phone:
            records_by_phone.setdefault(phone, []).append(record)
            if record.get('site_location'):
                sites_by_phone.setdefault(phone, set()).add(site_key(record['site_location']))
    for message in messages:
        if message.get('direction') == 'outbound':
            originals_by_id.setdefault(str(message.get('provider_message_id')), message)
    for conversation in conversations:
        phone = phone_key(conversation.get('phone'))
        if conversation.get('dealer'):
            convo_sites_by_phone.setdefault(phone, set()).add(conversation['dealer'])
        for message in conversation.get('messages', []):
            identifier = str(message.get('id'))
            threads_by_id.setdefault(identifier, conversation)
            if message.get('sender') != 'user':
                outbound_threads_by_id.setdefault(identifier, conversation)
    out = []
    def visible(phone, site):
        return (allowed_site(user, site)
                and (not requested_site or requested_site == 'All Sites' or site_key(site) == site_key(requested_site))
                and (not focus or phone_key(phone) == phone_key(focus)))
    for msg in messages:
        client = clients_by_id.get(msg.get('client_id')) if msg.get('client_id') else None
        normalized_phone = phone_key(msg.get('phone'))
        matched = records_by_phone.get(normalized_phone, [])
        sites = sites_by_phone.get(normalized_phone, set())
        convo_sites = convo_sites_by_phone.get(normalized_phone, set())
        correlation = (msg.get('provider_response') or {}).get('original_message_id') if msg.get('direction') == 'inbound' else None
        original = originals_by_id.get(str(correlation)) if correlation else None
        original_client = clients_by_id.get((original or {}).get('client_id'))
        correlated_thread = outbound_threads_by_id.get(str(correlation)) if correlation else None
        source_thread = threads_by_id.get(str(msg.get('provider_message_id'))) if msg.get('provider_message_id') else None
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
        client = clients_by_phone_site.get((phone_key(convo.get('phone')), site_key(convo.get('dealer'))))
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
