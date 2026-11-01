"""Guarded Denza Melbourne Dealer Studio snapshots and delivery-client upserts."""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bson import ObjectId, json_util
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from models import Client

SITE = 'Denza Melbourne'
SLUG = 'denza-melbourne'
DEALERSHIP = '2606'
ELIGIBLE = {'contract signed', 'vehicle order', 'delivered'}
RAW_COLLECTION = 'dealerstudio_records'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def site_key(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def denza(document):
    # A populated primary site always wins over a stale secondary site field.
    return site_key(document.get('site_location') or document.get('site')) == 'denzamelbourne'


def source_id(value):
    value = str(value or '').strip()
    return value if re.fullmatch(r'[1-9]\d*', value) else None


def phone(value):
    value = re.sub(r'[^\d+]', '', str(value or ''))
    if value.startswith('04') and len(value) == 10:
        return '+61' + value[1:]
    if value.startswith('61') and len(value) == 11:
        return '+' + value
    return value


def date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return None


def read_snapshot(directory, require_fresh=False):
    directory = Path(directory)
    read = lambda name: json.loads((directory / name).read_text(encoding='utf8'))
    manifest, all_leads, active, cars, details = (read(name) for name in [
        'COMPLETE_DATA_MANIFEST.json', 'all_lead_clusters.json', 'active_lead_clusters.json',
        'all_cars_inventory.json', 'all_cars_detailed_specs.json'])
    for envelope in (manifest, all_leads, active):
        if envelope.get('dealership_slug') != SLUG or str(envelope.get('dealership_id')) != DEALERSHIP:
            raise ValueError('The snapshot must belong only to Denza Melbourne (2606).')
    leads, active_rows = all_leads.get('lead_clusters'), active.get('lead_clusters')
    stats = manifest.get('statistics', {})
    for rows, count in [(leads, stats.get('total_historic_leads')), (active_rows, stats.get('total_active_leads')),
                        (cars, stats.get('total_cars_in_stock')), (details, stats.get('total_cars_in_stock'))]:
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError('The snapshot is incomplete or its counts do not match the manifest.')
        ids = [source_id(row.get('id')) for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or None in ids or len(set(ids)) != len(ids):
            raise ValueError('Snapshot rows need unique, stable source IDs.')
        if any(str(row.get('dealership_id')) != DEALERSHIP or row.get('dealership_slug', SLUG) != SLUG for row in rows):
            raise ValueError('A snapshot row belongs to a different dealership.')
    if not leads:
        raise ValueError('An empty lead snapshot cannot be imported automatically.')
    if not {r['id'] for r in active_rows}.issubset({r['id'] for r in leads}) or {r['id'] for r in cars} != {r['id'] for r in details}:
        raise ValueError('Active leads/inventory details are inconsistent with the complete snapshot.')
    if require_fresh:
        if manifest.get('complete') is not True:
            raise ValueError('The harvester did not verify this snapshot as complete.')
        timestamp = datetime.fromisoformat(str(manifest.get('fetchedAt', '')).replace('Z', '+00:00'))
        if timestamp.tzinfo is None or not -60 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= 7200:
            raise ValueError('The Dealer Studio snapshot is stale or has an invalid timestamp.')
    return {'manifest': manifest, 'leads': leads, 'cars': details}


def fields(row, cars):
    item = row.get('item') or {}
    matches = [car for car in cars if any(item.get(key) and str(item[key]).casefold() == str(car.get(key) or '').casefold()
               for key in ('stocknum', 'regplate', 'slug'))]
    car = matches[0] if len(matches) == 1 else {}
    description = ' '.join(str(value).strip() for value in [item.get('year'), item.get('make'), item.get('model'), item.get('model_variant')] if value)
    result = {'name': str(row.get('name') or '').strip(), 'phone': phone(row.get('phone')),
              'email': str(row.get('email') or '').strip().lower() or None, 'vehicle': description,
              'rego': item.get('regplate') or car.get('regplate'), 'vin': car.get('vin'),
              'vy_stock_id': item.get('stocknum') or car.get('stocknum'), 'salesperson': row.get('user_name'),
              'order_date': date(row.get('order_at')), 'dealerstudio_source_id': str(row['id']),
              'dealerstudio_contact_id': row.get('contact_id'), 'dealerstudio_dealership_id': DEALERSHIP,
              'dealerstudio_source_status': row.get('lead_status'), 'dealerstudio_source_type': row.get('lead_status_type'),
              'dealerstudio_source_hash': digest({'lead': row, 'car': car}), 'dealerstudio_source_data': row}
    if str(row.get('lead_status') or '').casefold() == 'delivered' or row.get('delivered_at'):
        result.update(stage='Delivered', arrived=True)
        if date(row.get('delivered_at')):
            result['delivery_date'] = date(row['delivered_at'])
    return result


def plan_clients(rows, cars, documents):
    # Existing Dealer Studio order IDs were saved in vy_order_id by the original import.
    index = {}
    for doc in documents:
        if denza(doc) and doc.get('imported_from') == 'dealer_studio':
            for key in {source_id(doc.get('dealerstudio_source_id')), source_id(doc.get('vy_order_id'))} - {None}:
                index.setdefault(key, []).append(doc)
    patches, inserts, conflicts, counts = {}, {}, [], Counter()
    now = datetime.now(timezone.utc)
    for row in rows:
        key = source_id(row.get('id'))
        if not key or str(row.get('dealership_id')) != DEALERSHIP:
            raise ValueError('A source row is missing its stable ID or Denza dealership identity.')
        matches = list({str(d['_id']): d for d in index.get(key, [])}.values())
        if len(matches) > 1:
            conflicts.append({'source_id': key, 'reason': 'duplicate existing source identity'})
            continue
        values = fields(row, cars)
        if matches:
            doc = matches[0]
            patch = {k: v for k, v in values.items() if v not in (None, '') and doc.get(k) != v}
            if patch:
                patch['updated_at'] = now
                patches[str(doc['_id'])] = {'original': doc, 'set': patch}
                counts['updated'] += 1
            else:
                counts['unchanged'] += 1
        elif str(row.get('lead_status') or '').casefold() in ELIGIBLE:
            if not values['name']:
                raise ValueError('An ordered/delivered source record is missing its customer name.')
            identifier = hashlib.sha256(('dealerstudio:' + DEALERSHIP + ':' + key).encode()).hexdigest()[:24]
            model_fields = {k: v for k, v in values.items() if not k.startswith('dealerstudio_')}
            new = Client(**model_fields, id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'dealerstudio:' + DEALERSHIP + ':' + key)),
                         site_location=SITE, vy_order_id=key, imported_from='dealer_studio', imported_at=now,
                         created_at=datetime.fromisoformat(str(row['created_at']).replace('Z', '+00:00')) if row.get('created_at') else now,
                         updated_at=now).model_dump(mode='json')
            new.update({k: v for k, v in values.items() if k.startswith('dealerstudio_')})
            new.update(_id=ObjectId(identifier), platform='dealer_studio')
            inserts[identifier] = new
            counts['inserted'] += 1
        else:
            counts['not_delivery_eligible'] += 1
    return {'patches': patches, 'inserts': inserts, 'conflicts': conflicts, 'counts': dict(counts)}


def raw_documents(rows, kind):
    return [{'_id': f'dealerstudio:{DEALERSHIP}:{kind}:{row["id"]}', 'dealership_id': DEALERSHIP,
             'record_type': kind, 'source_id': str(row['id']), 'site_location': SITE, 'source_hash': digest(row), 'source_data': row} for row in rows]


def hash_documents(rows):
    return hashlib.sha256(json_util.dumps(sorted(rows, key=lambda d: str(d['_id'])),
        json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True).encode()).hexdigest()


def run_import(directory, dry_run=False, backup=None, require_fresh=False):
    snapshot = read_snapshot(directory, require_fresh)
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    with MongoClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=10000, socketTimeoutMS=60000) as mongo:
        db = mongo[os.environ.get('DB_NAME', 'test_database')]
        clients = list(db.clients.find({}).batch_size(100))
        plan = plan_clients(snapshot['leads'], snapshot['cars'], clients)
        # Two logical models share one physical collection to fit Atlas collection quotas.
        sources = {RAW_COLLECTION: [*raw_documents(snapshot['leads'], 'lead'), *raw_documents(snapshot['cars'], 'car')]}
        old = {name: list(db[name].find({}).batch_size(100)) for name in sources}
        raw_changes = {}
        for name, incoming in sources.items():
            previous = {d['_id']: d for d in old[name]}
            raw_changes[name] = []
            for row in incoming:
                existing = previous.get(row['_id'])
                if existing and (str(existing.get('dealership_id')) != DEALERSHIP or not denza(existing)):
                    raise ValueError('A raw source identity collides with another dealership.')
                if not existing or existing.get('record_type') != row['record_type'] or existing.get('source_hash') != row['source_hash'] or existing.get('source_data') != row['source_data']:
                    raw_changes[name].append(row)
        summary = {'database': db.name, 'site': SITE, 'dry_run': dry_run, 'counts': plan['counts'], 'conflicts': plan['conflicts'],
                   'source_counts': {'leads': len(snapshot['leads']), 'inventory': len(snapshot['cars'])},
                   'storage_collection': RAW_COLLECTION, 'raw_upserts': {name: len(rows) for name, rows in raw_changes.items()}}
        if plan['conflicts']:
            raise ValueError('Duplicate Denza source identities must be resolved before importing.')
        if dry_run:
            return summary
        if not backup:
            raise ValueError('A private pre-import backup is required.')
        backup = Path(backup).resolve()
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle, gzip.GzipFile(fileobj=handle, mode='wb') as compressed:
            compressed.write(json_util.dumps({'database': db.name, 'createdAt': datetime.now(timezone.utc),
                'collections': {'clients': clients, **old}}, json_options=json_util.CANONICAL_JSON_OPTIONS).encode())
        untouched_ids = {d['_id'] for d in clients} - {p['original']['_id'] for p in plan['patches'].values()}
        protected_before = hash_documents([d for d in clients if d['_id'] in untouched_ids])
        operations = []
        for change in plan['patches'].values():
            original = change['original']
            guard = {'_id': original['_id'], 'imported_from': 'dealer_studio'}
            for field in ('site_location', 'site', 'updated_at', 'updatedAt', 'vy_order_id', 'dealerstudio_source_id'):
                guard[field] = original.get(field)
            operations.append(UpdateOne(guard, {'$set': change['set']}))
        for doc in plan['inserts'].values():
            operations.append(UpdateOne({'_id': doc['_id']}, {'$setOnInsert': doc}, upsert=True))
        modified = inserted = 0
        for start in range(0, len(operations), 300):
            result = db.clients.bulk_write(operations[start:start + 300], ordered=True)
            modified += result.modified_count
            inserted += result.upserted_count
        if modified != len(plan['patches']) or inserted != len(plan['inserts']):
            raise RuntimeError('A client changed concurrently. The partial result requires a fresh verification.')
        for name, rows in raw_changes.items():
            ops = [UpdateOne({'_id': row['_id'], 'dealership_id': DEALERSHIP}, {'$set': {**{k: v for k, v in row.items() if k != '_id'}, 'updated_at': datetime.now(timezone.utc)},
                '$setOnInsert': {'created_at': datetime.now(timezone.utc)}}, upsert=True) for row in rows]
            for start in range(0, len(ops), 300):
                db[name].bulk_write(ops[start:start + 300], ordered=True)
        after = list(db.clients.find({}).batch_size(100))
        if hash_documents([d for d in after if d['_id'] in untouched_ids]) != protected_before:
            raise RuntimeError('Unrelated clients changed during the sync; investigate concurrent changes.')
        check = plan_clients(snapshot['leads'], snapshot['cars'], after)
        if check['patches'] or check['inserts'] or check['conflicts']:
            raise RuntimeError('The client upsert did not pass second-run verification.')
        for name, rows in sources.items():
            actual = {d['_id']: d for d in db[name].find({'_id': {'$in': [r['_id'] for r in rows]}})}
            if any(actual.get(r['_id'], {}).get('source_data') != r['source_data'] for r in rows):
                raise RuntimeError('Raw Dealer Studio snapshot verification failed.')
            changed_ids = {r['_id'] for r in raw_changes[name]}
            protected = [r for r in old[name] if r['_id'] not in changed_ids]
            if protected:
                current = list(db[name].find({'_id': {'$in': [r['_id'] for r in protected]}}))
                if hash_documents(current) != hash_documents(protected):
                    raise RuntimeError('Unrelated raw snapshot records changed during the sync.')
        return {**summary, 'verified': True, 'modified_clients': modified, 'inserted_clients': inserted,
                'protected_records_unchanged': True, 'second_run_changes': 0, 'backup': str(backup)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--backup')
    parser.add_argument('--require-fresh', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run_import(args.input, args.dry_run, args.backup, args.require_fresh)))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': type(error).__name__, 'database_code': getattr(error, 'code', None),
                          'message': str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Dealer Studio database import failed.'}))
        raise SystemExit(1)
