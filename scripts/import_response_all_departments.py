"""Upsert Caroline Springs and Nunawading SalesLogs, routing DENZA by row SITE.
Existing IDs and staff workflow fields are retained. Live runs require a backup.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from bson import ObjectId, json_util
from pymongo import MongoClient, UpdateOne
from models import Client
from scripts.import_saleslogs import (
    derive_stage_and_flags, derive_contact_status, clean_identifier,
    normalise_phone, title_case_name, build_vehicle,
    extract_addons_and_accessories, build_comments, to_iso_date,
)
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ.get('DB_NAME', 'test_database')
DEPARTMENT_SITE_MAP = {
    'BYD Caroline Springs - New': 'BYD Caroline Springs',
    'BYD Nunawading - New': 'BYD Nunawading',
}
DENZA_SITE = 'Denza Melbourne'
DENZA_SOURCE_SITE = 'Harmony DENZA - VIC'
PLACEHOLDERS = {'TBA', 'TBC', 'TBD', 'PENDING', 'UNKNOWN', 'NOT AVAILABLE',
                'NO ALLOCATION', 'NO OPPORTUNITY', 'HOUSE', '-', '?'}


def identity(value):
    clean = clean_identifier(value)
    return clean.upper() if clean and clean.upper() not in PLACEHOLDERS else None


def site_key(value):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'^byd\s+', '', str(value or '').lower())).removesuffix('vic')


def effective_site(document):
    return document.get('site_location') or document.get('site') or ''


def is_denza_source(row):
    return str((row or {}).get('SITE') or '').strip().casefold() == DENZA_SOURCE_SITE.casefold()


def row_site(row, department_site):
    return DENZA_SITE if is_denza_source(row) else department_site


def editable(document):
    if is_denza_source(document.get('saleslogs_source_data')) or is_denza_source(document):
        return True
    if any('melbourne' in site_key(document.get(key)) for key in ('site_location', 'site')):
        return False
    return site_key(effective_site(document)) in {site_key(site) for site in DEPARTMENT_SITE_MAP.values()}


def source_fields(row, site):
    stage, arrived, vehicle_at, skip = derive_stage_and_flags(row)
    if skip:
        return None, skip
    pay = str(row.get('Pay') or row.get('DeptType') or 'Retail').strip()
    sale_type = next((kind for term, kind in [('FLEET', 'Fleet'), ('LEASE', 'Lease'), ('NOVATED', 'Lease'), ('CASH', 'Cash'), ('DEMO', 'Demo')] if term in pay.upper()), 'Cash' if pay == 'C' else 'Retail')
    suburb = str(row.get('Suburb') or '').strip().title()
    fields = {
        'name': title_case_name(row.get('DriverName') or row.get("Driver's Name") or row.get('Client') or row.get('CompanyName') or row.get('Company Name') or ''),
        'phone': normalise_phone(row.get('PhoneNo') or row.get('Phone Number')),
        'email': str(row.get('Email') or '').strip().lower() or None,
        'vehicle': build_vehicle(row), 'rego': identity(row.get('Rego') or row.get('Rego No.')),
        'vin': identity(row.get('Vin') or row.get('Vin No.')),
        'vy_order_id': identity(row.get('OrderNo') or row.get('Order No.') or row.get('SaleslogsOrder')),
        'vy_stock_id': clean_identifier(row.get('StockNo') or row.get('Stock No.')),
        'delivery_date': to_iso_date(row.get('ETA')) or to_iso_date(row.get('Estimated')) or to_iso_date(row.get('Actual')),
        'order_date': to_iso_date(row.get('OrderDate') or row.get('Order Date')),
        'salesperson': str(row.get('SP_FULL') or row.get('SP') or row.get('Sales Person') or '').strip() or None,
        'secondary_salesperson': str(row.get('SP2_FULL') or '').strip() or None,
        'delivery_consultant': str(row.get('AM_FULL') or row.get('AM') or row.get('Delivery Consultant') or '').strip() or None,
        'stage': stage, 'arrived': arrived, 'site_location': row_site(row, site),
        'location': (suburb if 'VIC' in suburb.upper() else f'{suburb}, VIC') if suburb else None,
        'sale_type': sale_type, 'deal_type': pay,
        'trade_in_flag': bool(str(row.get('TradeInType') or row.get('TradeInStockNo') or row.get('RegoTrade') or '').strip()),
    }
    if fields['email'] and fields['email'].upper() in PLACEHOLDERS | {'NIL', 'N/A', 'NONE'}:
        fields['email'] = None
    return fields, vehicle_at


def record_keys(document):
    keys = [(field, identity(document.get(field))) for field in ('saleslogs_source_id', 'vy_order_id', 'vin', 'rego') if identity(document.get(field))]
    signature = (str(document.get('name') or '').strip().lower(), normalise_phone(document.get('phone')), str(document.get('vehicle') or '').strip().lower())
    if all(signature):
        keys.append(('signature', signature))
    return keys


def build_patch(existing, fields, row, now):
    patch = {}
    for key, value in fields.items():
        if value in (None, ''):
            continue  # Empty exports must not erase staff-enriched values.
        if key == 'stage' and existing.get('stage') == 'Delivered' and value != 'Delivered':
            continue
        if key == 'arrived' and existing.get('arrived') and not value:
            continue
        if existing.get(key) != value:
            patch[key] = value
    if fields['arrived'] and not existing.get('arrived_at'):
        patch['arrived_at'] = now.isoformat()
    addons, accessories = extract_addons_and_accessories(row)
    combined_addons = list(existing.get('addons') or [])
    combined_accessories = list(existing.get('accessories') or [])
    known_addons = {str(item).lower() for item in combined_addons}
    known_accessories = {str(item.get('name') or '').lower() for item in combined_accessories}
    for addon in addons:
        if addon.lower() not in known_addons:
            combined_addons.append(addon)
            known_addons.add(addon.lower())
    for accessory in accessories:
        if accessory['name'].lower() not in known_accessories:
            combined_accessories.append({**accessory, 'updated_at': accessory['updated_at'].isoformat()})
            known_accessories.add(accessory['name'].lower())
    if combined_addons != (existing.get('addons') or []):
        patch['addons'] = combined_addons
    if combined_accessories != (existing.get('accessories') or []):
        patch['accessories'] = combined_accessories
    return patch


def plan_import(payload, documents, users):
    departments = payload.get('departments', {})
    if any(not isinstance(departments.get(name, {}).get('data', {}).get('list'), list) for name in DEPARTMENT_SITE_MAP):
        raise ValueError('Fresh deal lists for both requested departments are required.')
    now = datetime.now(timezone.utc)
    index = defaultdict(list)
    original = {str(document['_id']): document for document in documents}
    working = {key: dict(document) for key, document in original.items()}
    patches, inserts, seen, conflicts = {}, {}, {}, []
    migrated = []
    # Older exports may no longer contain the deal. Stored source SITE still
    # identifies its dealership, including cancelled/historical records.
    for key, document in working.items():
        if is_denza_source(document.get('saleslogs_source_data')) or is_denza_source(document):
            patch = {'site_location': DENZA_SITE}
            if 'site' in document:
                patch['site'] = DENZA_SITE
            patch = {field: value for field, value in patch.items() if document.get(field) != value}
            if patch:
                patch['updated_at'] = now.isoformat()
                patches[key] = patch
                document.update(patch)
                migrated.append(key)
    for key, document in working.items():
        if editable(document):
            for match_key in record_keys(document):
                index[(site_key(effective_site(document)), *match_key)].append(key)
    counts = {site: Counter() for site in [*DEPARTMENT_SITE_MAP.values(), DENZA_SITE]}
    for department, department_site in DEPARTMENT_SITE_MAP.items():
        department_id = departments[department].get('id')
        for row in departments[department]['data']['list']:
            site = row_site(row, department_site)
            counts[site]['rows'] += 1
            fields, skip_or_location = source_fields(row, site)
            if fields is None:
                counts[site]['skipped_' + skip_or_location] += 1
                continue
            source_id = identity(row.get('id'))
            if not source_id:
                raise ValueError('An eligible SalesLogs row has no stable source id.')
            digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            source_key = (department_id or department, source_id)
            if source_key in seen:
                if seen[source_key] != digest:
                    raise ValueError(f'Conflicting duplicate source ID {source_id} at {site}')
                counts[site]['duplicate_source_rows'] += 1
                continue
            seen[source_key] = digest
            metadata = {'saleslogs_source_id': source_id, 'saleslogs_department_id': department_id, 'saleslogs_source_hash': digest, 'saleslogs_source_data': row}
            probe = {**fields, 'saleslogs_source_id': source_id}
            matched_keys = []
            for kind, value in record_keys(probe):
                match_sites = [site, department_site] if site == DENZA_SITE else [site]
                candidates = list(dict.fromkeys(key for match_site in match_sites for key in index.get((site_key(match_site), kind, value), [])))
                candidates = [key for key in candidates if not working[key].get('saleslogs_source_id') or identity(working[key]['saleslogs_source_id']) == source_id]
                candidates = [key for key in candidates if not working[key].get('saleslogs_department_id') or working[key]['saleslogs_department_id'] == department_id]
                if candidates:
                    if not matched_keys:
                        matched_keys = candidates
                    elif len(matched_keys) > 1:
                        narrowed = [key for key in matched_keys if key in candidates]
                        if narrowed:
                            matched_keys = narrowed
                    if kind == 'saleslogs_source_id':
                        break
            if len(matched_keys) > 1:
                conflicts.append({'site': site, 'source_id': source_id, 'matching_ids': matched_keys})
                counts[site]['ambiguous_matches'] += 1
                continue
            if matched_keys:
                key = matched_keys[0]
                existing = working[key]
                patch = build_patch(existing, fields, row, now)
                if 'site' in existing and existing.get('site') != site:
                    patch['site'] = site
                patch.update({field: value for field, value in metadata.items() if existing.get(field) != value})
                if patch:
                    patch['updated_at'] = now.isoformat()
                    existing.update(patch)
                    if key in inserts:
                        inserts[key].update(patch)
                    else:
                        patches.setdefault(key, {}).update(patch)
                counts[site]['updated' if patch or key in migrated else 'unchanged'] += 1
                for match_key in record_keys(existing):
                    index[(site_key(site), *match_key)].append(key)
                continue
            # Identity belongs to the source department, not its routed display site.
            stable = hashlib.sha256((department_site + ':' + source_id).encode()).hexdigest()[:24]
            addons, accessories = extract_addons_and_accessories(row)
            assigned = next((user.get('id') or str(user['_id']) for user in users if user.get('name', '').strip().lower() in {str(fields.get('salesperson') or '').lower(), str(fields.get('delivery_consultant') or '').lower()} and site_key(user.get('locked_site') or user.get('site') or user.get('active_site')) == site_key(site)), None)
            new = Client(**fields, id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'saleslogs:' + department_site + ':' + source_id)),
                addons=addons, accessories=accessories, comments=build_comments(row, skip_or_location if not fields['arrived'] else None),
                arrived_at=now if fields['arrived'] else None, contact_status=derive_contact_status(row, fields['stage']),
                assigned_agent_id=assigned, imported_from='saleslogs-json', imported_at=now,
                created_at=datetime.fromisoformat(fields['order_date'] + 'T00:00:00+00:00') if fields['order_date'] else now,
                updated_at=now).model_dump(mode='json')
            new.update(metadata)
            new['_id'] = ObjectId(stable)
            inserts[stable] = new
            counts[site]['inserted'] += 1
            working[stable] = new
            for match_key in record_keys(new):
                index[(site_key(site), *match_key)].append(stable)
    return {'counts': {site: dict(count) for site, count in counts.items()}, 'patches': patches, 'inserts': inserts, 'conflicts': conflicts, 'original': original, 'migrated_to_denza': migrated}


def protected_hash(documents):
    protected = sorted((doc for doc in documents if not editable(doc)), key=lambda doc: str(doc['_id']))
    return hashlib.sha256(json_util.dumps(protected, json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True).encode()).hexdigest()


def run_import(dry_run=False, backfill_existing_sites=True, backup_path=None):
    json_path = Path(__file__).resolve().parents[1] / 'data-saleslog/response_all_departments.json'
    payload = json.loads(json_path.read_text(encoding='utf-8'))
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000, socketTimeoutMS=60000)
    try:
        db = client[DB_NAME]
        documents = list(db.clients.find({}).batch_size(100))
        plan = plan_import(payload, documents, list(db.users.find({'active': True})))
        summary = {'database': DB_NAME, 'collection': 'clients', 'fetchedAt': payload.get('fetchedAt'), 'dry_run': dry_run, 'by_site': plan['counts'], 'older_records_moved_to_denza': len(plan['migrated_to_denza']), 'conflicts': plan['conflicts']}
        print(json.dumps(summary, indent=2))
        if dry_run:
            return summary
        if plan['conflicts']:
            raise ValueError('Ambiguous existing matches require resolution before applying the import.')
        if not backup_path:
            raise ValueError('A backup path is required for a live import.')
        before_protected = protected_hash(documents)
        backup_path = Path(backup_path).resolve()
        with backup_path.open('x', encoding='utf-8') as handle:
            handle.write(json_util.dumps({'database': DB_NAME, 'collection': 'clients', 'backedUpAt': datetime.now(timezone.utc), 'documents': documents}, json_options=json_util.CANONICAL_JSON_OPTIONS))
        operations = []
        for key, patch in plan['patches'].items():
            original = plan['original'][key]
            guard = {'_id': original['_id']}
            for field in ('site_location', 'site', 'updated_at', 'updatedAt'):
                guard[field] = original.get(field)
            operations.append(UpdateOne(guard, {'$set': patch}))
        for document in plan['inserts'].values():
            operations.append(UpdateOne({'_id': document['_id']}, {'$setOnInsert': document}, upsert=True))
        result = Counter()
        for start in range(0, len(operations), 300):
            batch = db.clients.bulk_write(operations[start:start + 300], ordered=True)
            result.update({'matched': batch.matched_count, 'modified': batch.modified_count, 'inserted': batch.upserted_count})
        if result['modified'] != len(plan['patches']) or result['inserted'] != len(plan['inserts']):
            raise RuntimeError('A record changed concurrently; check the result before retrying.')
        after = list(db.clients.find({}).batch_size(100))
        if protected_hash(after) != before_protected:
            raise RuntimeError('Protected-site records changed during this run; investigate concurrent changes.')
        check = plan_import(payload, after, list(db.users.find({'active': True})))
        if check['patches'] or check['inserts'] or check['conflicts']:
            raise RuntimeError('Post-import verification found remaining changes.')
        print(json.dumps({'applied': dict(result), 'protected_sites_unchanged': True, 'second_run_changes': 0, 'backup': str(backup_path), 'verified': True}))
        return {**summary, 'applied': dict(result), 'verified': True}
    finally:
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--backup')
    args = parser.parse_args()
    run_import(dry_run=args.dry_run, backup_path=args.backup)
