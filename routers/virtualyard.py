from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Optional, List
import re
from datetime import datetime, timezone
from pathlib import Path

from db import get_db
from models import User
from auth import get_current_user, require_admin, effective_site
from scripts.import_virtualyard_deals import run_import, DEFAULT_JSON_PATH

router = APIRouter(prefix='/api/virtualyard', tags=['virtualyard'])


def _strip(doc):
    if doc:
        doc.pop('_id', None)
    return doc


@router.get('/deals')
async def list_deals(
    q: Optional[str] = Query(None, description="Search customer, vehicle, phone, email, proposal #, invoice ID, stock #, rego"),
    site: Optional[str] = Query(None, description="Filter by location/yard name"),
    status: Optional[str] = Query(None, description="Filter by deal status"),
    settlement_status: Optional[str] = Query(None, description="Filter by settlement status (Paid, Partially Paid, Unpaid)"),
    payment_method: Optional[str] = Query(None, description="Filter by payment method (e.g. Credit Card, EFT, Finance)"),
    has_payments: Optional[bool] = Query(None, description="Filter deals that have at least one payment recorded"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=200),
    sort_by: str = Query('date', pattern='^(date|total|paid|due|proposal|customer)$'),
    sort_order: str = Query('desc', pattern='^(asc|desc)$'),
    user: User = Depends(get_current_user),
):
    db = get_db()
    query = {}

    # Handle site scoping
    eff_site = effective_site(user, site)
    if eff_site and eff_site != 'All Sites':
        # Match location_name with flexibility (e.g. "Caroline Springs" matches "BYD Caroline Springs")
        clean_site = re.sub(r'(?i)^byd\s+', '', eff_site.strip())
        query['location_name'] = {'$regex': clean_site, '$options': 'i'}

    if status and status != 'All':
        query['status'] = status

    if settlement_status and settlement_status != 'All':
        query['settlement_status'] = settlement_status

    if payment_method and payment_method != 'All':
        query['payments.method'] = {'$regex': f'^{re.escape(payment_method)}$', '$options': 'i'}

    if has_payments is True:
        query['payment_count'] = {'$gt': 0}
    elif has_payments is False:
        query['payment_count'] = 0

    if q and q.strip():
        term = q.strip()
        escaped = re.escape(term)
        regex = {'$regex': escaped, '$options': 'i'}
        query['$or'] = [
            {'customer_name': regex},
            {'vehicle': regex},
            {'customer_phone': regex},
            {'customer_email': regex},
            {'proposal_number': regex},
            {'invoice_id': regex},
            {'stock_no': regex},
            {'rego': regex},
            {'order_no': regex},
        ]

    # Sorting
    direction = -1 if sort_order == 'desc' else 1
    sort_field_map = {
        'date': 'sold_date',
        'total': 'deal_total',
        'paid': 'payment_total',
        'due': 'balance_due',
        'proposal': 'proposal_number',
        'customer': 'customer_name',
    }
    mongo_sort = [(sort_field_map.get(sort_by, 'sold_date'), direction), ('invoice_id', direction)]

    # Fetch total count
    total_matching = await db.virtual_yard_deals.count_documents(query)

    # Aggregated metrics for active query
    pipeline_stats = [
        {'$match': query},
        {
            '$group': {
                '_id': None,
                'total_deals': {'$sum': 1},
                'total_deal_value': {'$sum': '$deal_total'},
                'total_collected': {'$sum': '$payment_total'},
                'total_balance_due': {'$sum': '$balance_due'},
                'total_payments_count': {'$sum': '$payment_count'},
            }
        }
    ]
    stats_cursor = db.virtual_yard_deals.aggregate(pipeline_stats)
    stats_list = await stats_cursor.to_list(length=1)
    stats = stats_list[0] if stats_list else {
        'total_deals': 0,
        'total_deal_value': 0.0,
        'total_collected': 0.0,
        'total_balance_due': 0.0,
        'total_payments_count': 0,
    }
    stats.pop('_id', None)

    # Pagination
    skip = (page - 1) * limit
    cursor = db.virtual_yard_deals.find(query).sort(mongo_sort).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        items.append(_strip(doc))

    total_pages = (total_matching + limit - 1) // limit if limit > 0 else 1

    return {
        'items': items,
        'page': page,
        'limit': limit,
        'total': total_matching,
        'pages': total_pages,
        'stats': stats,
    }


@router.get('/deals/{invoice_id}')
async def get_deal(
    invoice_id: str,
    user: User = Depends(get_current_user),
):
    db = get_db()
    deal = await db.virtual_yard_deals.find_one({'invoice_id': invoice_id})
    if not deal:
        raise HTTPException(status_code=404, detail=f"Virtual Yard deal with invoice ID {invoice_id} not found")

    _strip(deal)

    # Check for matched client in Delivery Centre clients collection
    matched_client = None
    query_conditions = []
    if deal.get('proposal_number'):
        query_conditions.append({'vy_order_id': deal['proposal_number']})
    if deal.get('order_no'):
        query_conditions.append({'vy_order_id': deal['order_no']})
    if deal.get('rego'):
        query_conditions.append({'rego': {'$regex': f"^{re.escape(deal['rego'])}$", '$options': 'i'}})
    if deal.get('stock_no'):
        query_conditions.append({'vy_stock_id': deal['stock_no']})
    if deal.get('customer_phone'):
        clean_phone = re.sub(r'[^\d+]', '', deal['customer_phone'])
        if len(clean_phone) >= 8:
            query_conditions.append({'phone': {'$regex': re.escape(clean_phone[-8:])}})

    if query_conditions:
        client_doc = await db.clients.find_one({'$or': query_conditions})
        if client_doc:
            matched_client = {
                'id': client_doc.get('id'),
                'name': client_doc.get('name'),
                'stage': client_doc.get('stage'),
                'vehicle': client_doc.get('vehicle'),
                'salesperson': client_doc.get('salesperson'),
            }

    deal['matched_client'] = matched_client
    return deal


@router.get('/stats')
async def get_overview_stats(
    user: User = Depends(get_current_user),
):
    db = get_db()

    # Location breakdown
    location_pipeline = [
        {
            '$group': {
                '_id': '$location_name',
                'deals_count': {'$sum': 1},
                'total_value': {'$sum': '$deal_total'},
                'total_collected': {'$sum': '$payment_total'},
                'total_due': {'$sum': '$balance_due'},
                'payments_count': {'$sum': '$payment_count'},
            }
        },
        {'$sort': {'deals_count': -1}}
    ]
    location_cursor = db.virtual_yard_deals.aggregate(location_pipeline)
    locations = []
    async for loc in location_cursor:
        locations.append({
            'location_name': loc['_id'] or 'Unassigned',
            'deals_count': loc['deals_count'],
            'total_value': round(loc['total_value'], 2),
            'total_collected': round(loc['total_collected'], 2),
            'total_due': round(loc['total_due'], 2),
            'payments_count': loc['payments_count'],
        })

    # Payment methods breakdown
    methods_pipeline = [
        {'$unwind': '$payments'},
        {
            '$group': {
                '_id': '$payments.method',
                'count': {'$sum': 1},
                'total_amount': {'$sum': '$payments.amount'},
            }
        },
        {'$sort': {'total_amount': -1}}
    ]
    methods_cursor = db.virtual_yard_deals.aggregate(methods_pipeline)
    methods = []
    async for m in methods_cursor:
        methods.append({
            'method': m['_id'] or 'Unspecified',
            'count': m['count'],
            'total_amount': round(m['total_amount'], 2),
        })

    # Settlement status breakdown
    status_pipeline = [
        {
            '$group': {
                '_id': '$settlement_status',
                'count': {'$sum': 1},
                'total_amount': {'$sum': '$deal_total'},
                'total_collected': {'$sum': '$payment_total'},
            }
        },
        {'$sort': {'count': -1}}
    ]
    status_cursor = db.virtual_yard_deals.aggregate(status_pipeline)
    settlement_statuses = []
    async for s in status_cursor:
        settlement_statuses.append({
            'status': s['_id'] or 'Unknown',
            'count': s['count'],
            'total_amount': round(s['total_amount'], 2),
            'total_collected': round(s['total_collected'], 2),
        })

    # Overall totals
    totals_pipeline = [
        {
            '$group': {
                '_id': None,
                'total_deals': {'$sum': 1},
                'total_deal_value': {'$sum': '$deal_total'},
                'total_collected': {'$sum': '$payment_total'},
                'total_balance_due': {'$sum': '$balance_due'},
                'total_payments_count': {'$sum': '$payment_count'},
            }
        }
    ]
    totals_cursor = db.virtual_yard_deals.aggregate(totals_pipeline)
    totals_list = await totals_cursor.to_list(length=1)
    totals = totals_list[0] if totals_list else {}
    totals.pop('_id', None)

    return {
        'totals': totals,
        'locations': locations,
        'payment_methods': methods,
        'settlement_statuses': settlement_statuses,
    }


@router.post('/sync')
async def trigger_sync(
    user: User = Depends(require_admin),
):
    try:
        run_import(DEFAULT_JSON_PATH)
        db = get_db()
        count = await db.virtual_yard_deals.count_documents({})
        return {
            'status': 'success',
            'message': f'Virtual Yard deal payment records synchronized successfully.',
            'total_deals': count,
            'synced_at': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync error: {str(e)}")
