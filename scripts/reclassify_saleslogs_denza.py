"""Repair DENZA site fields on SalesLogs records and explicitly linked records.

No phone/name matching is used: a customer can have deals at several dealerships.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from bson import json_util
from dotenv import dotenv_values
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.import_response_all_departments import (
    DENZA_SITE, DENZA_SOURCE_SITE, MONGO_URL, DB_NAME,
)

DELIVERY_COLLECTIONS = {
    'customers': 'site', 'opportunities': 'site', 'saleslogentries': 'site',
    'allocations': 'site', 'stockholds': 'site',
}
LEAD_COLLECTIONS = {'leads': 'dealer', 'appointments': 'dealership'}


def site_patch(document, primary, lead=False):
    destination = 'DENZA Melbourne' if lead else DENZA_SITE
    fields = {primary, *(field for field in ('site_location', 'site', 'dealer', 'dealership') if field in document)}
    return {field: destination for field in fields if document.get(field) != destination}


def repair(dry_run=True, backup=None, lead_env=None):
    delivery = MongoClient(MONGO_URL, serverSelectionTimeoutMS=15000)
    lead = None
    try:
        db = delivery[DB_NAME]
        source_query = {'$or': [
            {'saleslogs_source_data.SITE': {'$regex': '^\\s*' + DENZA_SOURCE_SITE + '\\s*$', '$options': 'i'}},
            {'SITE': {'$regex': '^\\s*' + DENZA_SOURCE_SITE + '\\s*$', '$options': 'i'}},
        ]}
        clients = list(db.clients.find(source_query, {'_id': 1, 'id': 1, 'crm_customer_id': 1, 'crm_opportunity_id': 1}))
        client_ids = list({str(value) for client in clients for value in (client['_id'], client.get('id')) if value})
        customer_ids = list({client['crm_customer_id'] for client in clients if client.get('crm_customer_id')})
        opportunity_ids = list({client['crm_opportunity_id'] for client in clients if client.get('crm_opportunity_id')})
        query = {'$or': [*source_query['$or'], {'delivery_client_id': {'$in': client_ids}}]}
        configs = [(db, DELIVERY_COLLECTIONS, False)]
        lead_uri = (dotenv_values(lead_env).get('LEAD_CENTER_MONGO_URI') if lead_env else os.environ.get('LEAD_CENTER_MONGO_URI'))
        if lead_uri:
            lead = MongoClient(lead_uri, serverSelectionTimeoutMS=15000)
            configs.append((lead.get_default_database(), LEAD_COLLECTIONS, True))
        changes = []
        checked = {}
        for database, collections, lead_platform in configs:
            for collection, primary in collections.items():
                selector = {'$or': list(query['$or'])}
                if collection == 'customers' and customer_ids:
                    selector['$or'].append({'customer_id': {'$in': customer_ids}})
                if collection == 'opportunities' and opportunity_ids:
                    selector['$or'].append({'opportunity_id': {'$in': opportunity_ids}})
                documents = list(database[collection].find(selector))
                label = database.name + '.' + collection
                checked[label] = len(documents)
                for document in documents:
                    patch = site_patch(document, primary, lead_platform)
                    if patch:
                        changes.append((database, collection, document, patch))
        summary = {'dry_run': dry_run, 'denza_saleslogs_clients': len(clients), 'checked_related': checked,
                   'changes': len(changes), 'lead_database_checked': bool(lead_uri)}
        print(json.dumps(summary, indent=2))
        if dry_run or not changes:
            return summary
        if not backup:
            raise ValueError('A backup path is required before repairing related records.')
        snapshot = [{'database': database.name, 'collection': collection, 'document': document, 'patch': patch}
                    for database, collection, document, patch in changes]
        with Path(backup).open('x', encoding='utf-8') as handle:
            handle.write(json_util.dumps(snapshot, json_options=json_util.CANONICAL_JSON_OPTIONS))
        for database, collection, document, patch in changes:
            guard = {'_id': document['_id'], **{field: document.get(field) for field in patch}}
            result = database[collection].update_one(guard, {'$set': patch})
            if result.modified_count != 1:
                raise RuntimeError('Related record changed concurrently; inspect the backup before retrying.')
            updated = database[collection].find_one({'_id': document['_id']})
            if updated != {**document, **patch}:
                raise RuntimeError('Related-record verification failed.')
        print(json.dumps({'verified': True, 'updated': len(changes), 'backup': str(backup)}))
        return summary
    finally:
        delivery.close()
        if lead is not None:
            lead.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--backup')
    parser.add_argument('--lead-env', help='Optional .env containing LEAD_CENTER_MONGO_URI')
    args = parser.parse_args()
    repair(dry_run=args.dry_run, backup=args.backup, lead_env=args.lead_env)
