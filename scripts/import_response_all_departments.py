"""
Import records from data-saleslog/response_all_departments.json into Shaun Delivery Centre MongoDB.
- Inserts only those records which are NOT already in the database.
- Links every record to its site ('BYD Caroline Springs', 'BYD Nunawading', 'Denza Melbourne').
- Backfills site_location for existing matched records that currently have site_location as None.
"""
import sys
import os
import re
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from models import Client
from scripts.import_saleslogs import (
    derive_stage_and_flags,
    derive_contact_status,
    clean_identifier,
    normalise_phone,
    title_case_name,
    build_vehicle,
    extract_addons_and_accessories,
    build_comments,
    to_iso_date,
)

load_dotenv(Path(__file__).resolve().parents[1] / '.env')

MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ.get('DB_NAME', 'test_database')

DEPARTMENT_SITE_MAP = {
    'BYD Caroline Springs - New': 'BYD Caroline Springs',
    'BYD Nunawading - New': 'BYD Nunawading',
    'Denza Melbourne - New': 'Denza Melbourne',
}


def run_import(dry_run=False, backfill_existing_sites=True):
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]
    json_path = Path(__file__).resolve().parents[1] / 'data-saleslog' / 'response_all_departments.json'

    if not json_path.exists():
        print(f"Error: JSON file not found at {json_path}")
        return

    print(f"Loading data from {json_path}...")
    with open(json_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    departments_data = payload.get('departments', {})
    print(f"Found {len(departments_data)} departments in JSON: {list(departments_data.keys())}")

    user_map = {u.get('name', '').lower(): u for u in db.users.find({'active': True})}

    # 1. Index existing database records for fast duplicate detection
    print("\nIndexing existing clients in database...")
    existing_orders = {}
    existing_vins = {}
    existing_regos = {}
    existing_email_names = {}
    existing_signatures = {}

    existing_docs = list(db.clients.find({}, {
        'id': 1, 'vy_order_id': 1, 'vin': 1, 'rego': 1,
        'email': 1, 'name': 1, 'phone': 1, 'vehicle': 1, 'site_location': 1
    }))
    print(f"Loaded {len(existing_docs)} existing client documents from database.")

    for doc in existing_docs:
        doc_id = doc.get('id')
        order = doc.get('vy_order_id')
        vin = doc.get('vin')
        rego = doc.get('rego')
        email = doc.get('email')
        name = doc.get('name')
        phone = doc.get('phone') or ''
        vehicle = doc.get('vehicle') or ''

        if order:
            existing_orders[order] = doc
        if vin:
            existing_vins[vin] = doc
        if rego:
            existing_regos[rego] = doc
        if email and name:
            existing_email_names[(email.lower().strip(), name.lower().strip())] = doc
        if name:
            existing_signatures[(name.strip(), phone.strip(), vehicle.strip())] = doc

    # 2. Iterate departments and collect new records to insert & existing records to update
    counts = {
        'total_in_json': 0,
        'skipped_blank': 0,
        'skipped_cancelled': 0,
        'matched_existing': 0,
        'new_to_insert': 0,
        'existing_sites_updated': 0,
        'by_site_new': {},
        'by_site_existing': {},
        'by_stage_new': {},
    }

    pending_inserts = []
    pending_updates = []
    now = datetime.now(timezone.utc)

    # Local tracking of newly processed records in this run to avoid duplicates within file
    seen_orders = set(existing_orders.keys())
    seen_vins = set(existing_vins.keys())
    seen_regos = set(existing_regos.keys())
    seen_email_names = set(existing_email_names.keys())
    seen_signatures = set(existing_signatures.keys())

    for dept_key, mapped_site in DEPARTMENT_SITE_MAP.items():
        dept_obj = departments_data.get(dept_key, {})
        data_obj = dept_obj.get('data', {})
        records = data_obj.get('list', [])
        print(f"\nProcessing department '{dept_key}' -> Site: '{mapped_site}' ({len(records)} records)...")
        counts['by_site_new'][mapped_site] = 0
        counts['by_site_existing'][mapped_site] = 0

        for row in records:
            counts['total_in_json'] += 1
            name_raw = (row.get('DriverName') or row.get("Driver's Name") or 
                        row.get('Client') or row.get('CompanyName') or row.get('Company Name') or '').strip()
            if not name_raw:
                counts['skipped_blank'] += 1
                continue

            stage, arrived, vehicle_at, skip = derive_stage_and_flags(row)
            if skip:
                if skip == 'cancelled':
                    counts['skipped_cancelled'] += 1
                else:
                    counts['skipped_blank'] += 1
                continue

            order_no = clean_identifier(row.get('OrderNo') or row.get('Order No.') or row.get('SaleslogsOrder'))
            vin = clean_identifier(row.get('Vin') or row.get('Vin No.'))
            rego = clean_identifier(row.get('Rego') or row.get('Rego No.'))
            phone = normalise_phone(row.get('PhoneNo') or row.get('Phone Number'))
            email = (str(row.get('Email') or '').strip().lower()) or None
            if email in ('', 'nil'):
                email = None
            client_name = title_case_name(name_raw)
            vehicle = build_vehicle(row)

            # Check if this record already exists in database
            existing = None
            if order_no and order_no in existing_orders:
                existing = existing_orders[order_no]
            elif vin and vin in existing_vins:
                existing = existing_vins[vin]
            elif rego and rego in existing_regos:
                existing = existing_regos[rego]
            elif email and (email.lower(), client_name.lower()) in existing_email_names:
                existing = existing_email_names[(email.lower(), client_name.lower())]
            elif (client_name, phone, vehicle) in existing_signatures:
                existing = existing_signatures[(client_name, phone, vehicle)]

            if existing:
                counts['matched_existing'] += 1
                counts['by_site_existing'][mapped_site] += 1
                # If existing client does not have site_location, update it
                if backfill_existing_sites and not existing.get('site_location'):
                    pending_updates.append(
                        UpdateOne({'id': existing['id']}, {'$set': {'site_location': mapped_site}})
                    )
                    existing['site_location'] = mapped_site
                    counts['existing_sites_updated'] += 1
                continue

            # Also check if seen earlier in this run (e.g. multi-department duplicates)
            if (
                (order_no and order_no in seen_orders)
                or (vin and vin in seen_vins)
                or (rego and rego in seen_regos)
                or (email and (email.lower(), client_name.lower()) in seen_email_names)
                or ((client_name, phone, vehicle) in seen_signatures)
            ):
                counts['matched_existing'] += 1
                continue

            # Record is truly NEW: Build Client document
            delivery_date = (to_iso_date(row.get('ETA')) or 
                             to_iso_date(row.get('Estimated')) or 
                             to_iso_date(row.get('Actual')))
            actual_delivery = to_iso_date(row.get('Actual'))
            order_date = to_iso_date(row.get('OrderDate') or row.get('Order Date'))

            sales_person = (str(row.get('SP_FULL') or row.get('SP') or row.get('Sales Person') or '').strip()) or None
            delivery_consultant = (str(row.get('AM_FULL') or row.get('AM') or row.get('Delivery Consultant') or '').strip()) or None

            assigned_agent_id = None
            if delivery_consultant:
                u = user_map.get(delivery_consultant.lower())
                if u:
                    assigned_agent_id = u['id']
            if not assigned_agent_id and sales_person:
                u = user_map.get(sales_person.lower())
                if u:
                    assigned_agent_id = u['id']

            addons, accessories = extract_addons_and_accessories(row)
            comments = build_comments(row, vehicle_at if not arrived else None)
            contact_status = derive_contact_status(row, stage)

            suburb = (str(row.get('Suburb') or '').strip().title()) or None
            location = None
            if suburb:
                location = suburb if 'VIC' in suburb.upper() else f"{suburb}, VIC"

            trade_in_flag = bool((row.get('TradeInType') or row.get('TradeInStockNo') or row.get('RegoTrade') or '').strip())
            trade_in_status = 'Pending' if trade_in_flag else 'Pending'

            stock_no = (str(row.get('StockNo') or row.get('Stock No.') or '').strip()) or None
            if stock_no and '[REMOVED]' in stock_no:
                stock_no = stock_no.replace('[REMOVED]', '')

            pay_type = (row.get('Pay') or row.get('DeptType') or 'Retail').strip()
            sale_type = 'Retail'
            if 'FLEET' in pay_type.upper():
                sale_type = 'Fleet'
            elif 'LEASE' in pay_type.upper() or 'NOVATED' in pay_type.upper():
                sale_type = 'Lease'
            elif 'CASH' in pay_type.upper() or pay_type == 'C':
                sale_type = 'Cash'
            elif 'DEMO' in pay_type.upper():
                sale_type = 'Demo'

            last_contacted = now if contact_status in ('Contacted', 'Awaiting Reply') else None
            if stage == 'Delivered' and actual_delivery:
                try:
                    last_contacted = datetime.fromisoformat(f'{actual_delivery}T12:00:00+00:00')
                except Exception:
                    last_contacted = now

            client_dict = {
                'id': str(uuid.uuid4()),
                'name': client_name,
                'phone': phone or '',
                'email': email,
                'vehicle': vehicle,
                'rego': rego,
                'vin': vin,
                'delivery_date': delivery_date,
                'stage': stage,
                'salesperson': sales_person,
                'delivery_consultant': delivery_consultant,
                'notes': None,
                'address': None,
                'location': location,
                'deal_type': pay_type or 'Retail',
                'sale_type': sale_type,
                'trade_in_flag': trade_in_flag,
                'trade_in_status': trade_in_status,
                'vy_order_id': order_no,
                'vy_stock_id': stock_no,
                'stripe_customer_id': None,
                'arrived': arrived,
                'arrived_at': now if arrived else None,
                'contact_status': contact_status,
                'last_contacted_at': last_contacted,
                'assigned_agent_id': assigned_agent_id,
                'accessories': accessories,
                'aftermarket_notes': None,
                'addons': addons,
                'site_location': mapped_site,
                'imported_from': 'saleslogs-json',
                'imported_at': now,
                'comments': comments,
                'created_at': datetime.fromisoformat(f'{order_date}T00:00:00+00:00') if order_date else now,
                'updated_at': now,
            }

            # Use Client pydantic model to guarantee valid structure and defaults
            client_obj = Client(**client_dict)
            client_doc = client_obj.model_dump(mode='json')

            pending_inserts.append(client_doc)
            counts['new_to_insert'] += 1
            counts['by_site_new'][mapped_site] += 1
            counts['by_stage_new'][stage] = counts['by_stage_new'].get(stage, 0) + 1

            # Mark as seen
            if order_no:
                seen_orders.add(order_no)
            if vin:
                seen_vins.add(vin)
            if rego:
                seen_regos.add(rego)
            if email:
                seen_email_names.add((email.lower(), client_name.lower()))
            seen_signatures.add((client_name, phone, vehicle))

    print("\n" + "=" * 60)
    print("IMPORT SUMMARY PLAN:")
    print(f"Total rows in JSON:              {counts['total_in_json']}")
    print(f"Skipped blank:                   {counts['skipped_blank']}")
    print(f"Skipped cancelled:               {counts['skipped_cancelled']}")
    print(f"Already in database (skipped):   {counts['matched_existing']}")
    print(f"Existing clients to update site: {len(pending_updates)}")
    print(f"NEW records to insert:           {counts['new_to_insert']}")
    print("\nNew records by site:")
    for s, n in counts['by_site_new'].items():
        print(f"  {s:25s}: {n} new (and {counts['by_site_existing'].get(s, 0)} already in DB)")
    print("\nNew records by stage:")
    for st, n in sorted(counts['by_stage_new'].items(), key=lambda x: -x[1]):
        print(f"  {st:25s}: {n}")
    print("=" * 60)

    if dry_run:
        print("\nDRY RUN: No modifications were made to the database.")
        return counts

    # Execute updates for existing records
    if pending_updates:
        print(f"\nUpdating site_location for {len(pending_updates)} existing clients...")
        for i in range(0, len(pending_updates), 500):
            batch = pending_updates[i:i + 500]
            db.clients.bulk_write(batch, ordered=False)
        print(f"  Successfully updated {len(pending_updates)} existing records.")

    # Execute inserts for new records
    if pending_inserts:
        print(f"\nInserting {len(pending_inserts)} new clients into database...")
        for i in range(0, len(pending_inserts), 500):
            batch = pending_inserts[i:i + 500]
            db.clients.insert_many(batch, ordered=True)
        print(f"  Successfully inserted {len(pending_inserts)} new records.")

    total_after = db.clients.count_documents({})
    print(f"\nDone! Total clients in database now: {total_after}")
    return counts


if __name__ == '__main__':
    dry_run_flag = '--dry-run' in sys.argv
    run_import(dry_run=dry_run_flag)
