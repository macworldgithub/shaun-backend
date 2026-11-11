"""Bounded dashboard queries: aggregate counts once; fetch only visible previews."""
import asyncio
import re
from datetime import datetime, timezone, timedelta

from auth import effective_site


async def dashboard(db, user, site_location=None):
    site = effective_site(user, site_location)
    base = {}
    if site and site.strip().lower() not in ('all', 'all sites'):
        base['site_location'] = {'$regex': f'^{re.escape(site.strip())}$', '$options': 'i'}
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    eq = lambda field, value: {'$eq': [f'${field}', value]}
    is_open = {'$ne': ['$stage', 'Delivered']}
    expressions = {
        'today_bookings': eq('delivery_date', today_iso),
        'today_completed': {'$and': [eq('delivery_date', today_iso), eq('stage', 'Delivered')]},
        'tomorrow_bookings': eq('delivery_date', (today + timedelta(days=1)).isoformat()),
        'plus_one_bookings': eq('delivery_date', (today + timedelta(days=2)).isoformat()),
        'arrived_pending': {'$and': [eq('arrived', True), is_open]},
        'not_contacted': eq('contact_status', 'Not Contacted'),
        'unassigned': {'$in': [{'$ifNull': ['$assigned_agent_id', '']}, ['', None]]},
        'docs_outstanding': {'$in': ['$document_completeness', ['Requested', 'Partial']]},
        'offers_at_risk': {'$in': ['$offer_status', ['At risk', 'Ineligible']]},
        'trade_ins_at_risk': {'$in': ['$trade_in_status', ['Expiring', 'Expired', 'At risk']]},
        'ready_to_register': eq('registration_status', 'Ready to register'),
        'ready_to_handover': {'$and': [eq('handover_checklist_status', 'Signed copy on file'), eq('activation_ready', False)]},
        'ready_for_delivery': {'$and': [eq('ready_for_delivery', True), is_open]},
        'unallocated_stock': {'$and': [is_open, {'$or': [
            {'$in': [{'$ifNull': [f'${field}', '']}, ['', None]]} for field in ('vin', 'rego')]}]},
        'overdue_updates': {'$and': [is_open, {'$gt': ['$delivery_date', '']}, {'$lt': ['$delivery_date', today_iso]}]},
    }
    group = {'_id': '$stage', 'count': {'$sum': 1}, **{
        name: {'$sum': {'$cond': [expression, 1, 0]}} for name, expression in expressions.items()
    }}
    message_base = {'site': base['site_location']} if base else {}
    preview = {key: 1 for key in ('id', 'name', 'vehicle', 'delivery_date', 'delivery_time', 'booking_type',
                                 'stage', 'site_location', 'arrived', 'contact_status', 'payment_complete',
                                 'trade_in_flag', 'trade_in_attached', 'trade_in_docs_complete', 'trade_in_status',
                                 'pdi_complete', 'registration_docs_complete', 'registration_status', 'document_completeness')}
    preview['_id'] = 0

    async def rows(query, limit, order):
        return await db.clients.find({**base, **query}, preview).sort(order).limit(limit).to_list(limit)

    counts, upcoming, arrived, uncontacted, messages, documents, sms_count = await asyncio.gather(
        db.clients.aggregate([{'$match': base}, {'$group': group}]).to_list(None),
        rows({'stage': {'$ne': 'Delivered'}}, 6, [('delivery_date', 1)]),
        rows({'arrived': True, 'stage': {'$ne': 'Delivered'}}, 5, [('updated_at', -1)]),
        rows({'contact_status': 'Not Contacted'}, 5, [('updated_at', -1)]),
        db.messages.find({**message_base, 'direction': 'inbound'}, {'_id': 0, 'provider_response': 0}).sort('sent_at', -1).limit(5).to_list(5),
        db.clients.find({**base, 'documents': {'$exists': True, '$ne': []}},
                        {'_id': 0, 'id': 1, 'name': 1, 'documents': 1}).sort('updated_at', -1).limit(30).to_list(30),
        db.messages.count_documents(message_base),
    )
    result = {name: sum(row[name] for row in counts) for name in expressions}
    result.update(total_clients=sum(row['count'] for row in counts),
                  by_stage={row['_id'] or 'Unknown': row['count'] for row in counts},
                  sms_total=sms_count, recent_messages=messages, daily_target=10,
                  upcoming=upcoming, needs_attention={'arrivedPending': arrived, 'notContacted': uncontacted})
    result.update(pipeline_today=result['today_bookings'], pipeline_tomorrow=result['tomorrow_bookings'],
                  pipeline_plus_one=result['plus_one_bookings'])
    uploads = []
    for client in documents:
        for doc in client.get('documents', []):
            if doc.get('status') == 'complete' or doc.get('storage_path'):
                uploads.append({'client_id': client['id'], 'client_name': client.get('name'),
                                **{key: doc.get(key) for key in ('document_type', 'file_name', 'created_at')}})
    result['recent_doc_uploads'] = sorted(uploads, key=lambda row: str(row.get('created_at') or ''), reverse=True)[:5]
    return result
