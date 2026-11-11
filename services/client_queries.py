"""Database filters for paginated client views; preserve operational readiness rules."""
import re
from datetime import datetime, timezone
from auth import effective_site


def ready_filter(today):
    return {'$and': [
        {'stage': {'$ne': 'Delivered'}}, {'payment_complete': True},
        {'$or': [
            {'trade_in_flag': {'$ne': True}, 'trade_in_attached': {'$ne': True}},
            {'trade_in_docs_complete': True},
            {'trade_in_status': {'$in': ['Settled', 'Accepted', 'Vehicle received']}},
            {'trade_in_status': 'Valid', '$or': [
                {'trade_in_valid_until': {'$gte': today}}, {'trade_in_valid_until': {'$in': [None, '']}},
            ]},
        ]},
        {'$or': [{'pdi_complete': True}, {'stage': {'$in': ['Ready for Pickup', 'Delivered']}}]},
        {'$or': [{'registration_docs_complete': True}, {'registration_status': 'Complete'}, {'document_completeness': 'Complete'}]},
    ]}


def client_page_query(user, site_location=None, search=None, stage=None,
                      contact_status=None, assigned_agent_id=None, unassigned=False,
                      mine=False, exception=None):
    today = datetime.now(timezone.utc).date().isoformat()
    site = effective_site(user, site_location)
    base = {}
    if site and site.lower() not in ('all', 'all sites'):
        base['site_location'] = {'$regex': '^' + re.escape(site.strip()) + '$', '$options': 'i'}
    clauses = [base]
    if search and search.strip():
        regex = {'$regex': re.escape(search.strip()), '$options': 'i'}
        clauses.append({'$or': [{key: regex} for key in ('name', 'vehicle', 'rego', 'phone', 'vin', 'email', 'vy_order_id')]})
    if stage:
        clauses.append({'stage': stage})
    if contact_status:
        clauses.append({'contact_status': contact_status})
    if mine:
        clauses.append({'assigned_agent_id': user.id})
    elif assigned_agent_id:
        clauses.append({'assigned_agent_id': assigned_agent_id})
    if unassigned:
        clauses.append({'assigned_agent_id': {'$in': [None, '']}})
    if exception == 'ready_for_delivery':
        clauses.append(ready_filter(today))
    elif exception == 'overdue':
        clauses.append({'stage': {'$ne': 'Delivered'}, '$or': [
            {'arrived': True}, {'delivery_date': {'$gt': '', '$lt': today}},
        ]})
    elif exception == 'trade_in':
        clauses.append({'$or': [
            {'trade_in_flag': True}, {'trade_in_status': {'$in': ['Expiring', 'Expired', 'At risk']}},
            {'trade_in_valid_until': {'$gt': '', '$lt': today},
             'trade_in_status': {'$in': ['Pending', 'Valid', 'Expiring soon']}},
        ]})
    elif exception == 'unallocated':
        clauses.append({'stage': {'$ne': 'Delivered'}, '$or': [
            {'vin': {'$in': [None, '']}}, {'rego': {'$in': [None, '']}},
        ]})
    elif exception == 'docs':
        clauses.append({'document_completeness': {'$in': ['Requested', 'Partial']}})
    return {'$and': clauses}, {'$and': [base, ready_filter(today)]}
