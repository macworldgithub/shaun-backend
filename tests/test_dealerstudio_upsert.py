import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from bson import ObjectId
from pymongo.errors import NetworkTimeout
from scripts.import_dealerstudio import plan_clients, read_snapshot, raw_documents, read_documents, CLIENT_SCOPE


def row(key=101, **changes):
    return {'id': key, 'dealership_id': 2606, 'dealership_slug': 'denza-melbourne', 'name': 'Saved Customer',
            'phone': '0400123456', 'email': 'saved@example.com', 'lead_status': 'Contract Signed',
            'lead_status_type': 'converted', 'created_at': '2026-10-01T00:00:00Z', 'order_at': '2026-10-02',
            'item': {'make': 'DENZA', 'model': 'B5', 'stocknum': 'STOCK1'}, **changes}


def apply(documents, plan):
    result = deepcopy(documents)
    for doc in result:
        if str(doc['_id']) in plan['patches']:
            doc.update(plan['patches'][str(doc['_id'])]['set'])
    return result + list(plan['inserts'].values())


class DealerStudioUpsertTests(unittest.TestCase):
    def test_read_retry_resumes_after_last_complete_page_without_duplicate_rows(self):
        class Cursor:
            def __init__(self, rows, fail=False): self.rows, self.fail = rows, fail
            def sort(self, *args): return self
            def limit(self, *args): return self
            def batch_size(self, *args): return self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __iter__(self):
                for index, doc in enumerate(self.rows):
                    yield doc
                    if self.fail and index == 0: raise NetworkTimeout('fixture interrupted cursor')
        class Collection:
            def __init__(self): self.calls = []
            def find(self, query):
                self.calls.append(query)
                if len(self.calls) == 1: return Cursor([{'_id': 1}, {'_id': 2}])
                return Cursor([{'_id': 3}], fail=len(self.calls) == 2)
        collection = Collection()
        query = {'_id': {'$in': [1, 2, 3]}, 'dealership_id': '2606'}
        with patch('scripts.import_dealerstudio.time.sleep') as sleep:
            result = read_documents(collection, query, batch_size=2)
        self.assertEqual([doc['_id'] for doc in result], [1, 2, 3])
        self.assertEqual(collection.calls[1], collection.calls[2])
        self.assertEqual(collection.calls[2]['$and'], [query, {'_id': {'$gt': 2}}])
        sleep.assert_called_once_with(1)

    def test_repeated_network_failure_has_a_bounded_retry_limit(self):
        class Collection:
            def __init__(self): self.calls = 0
            def find(self, query):
                self.calls += 1
                raise NetworkTimeout('fixture database unreachable')
        collection = Collection()
        with patch('scripts.import_dealerstudio.time.sleep'):
            with self.assertRaises(NetworkTimeout): read_documents(collection, {})
        self.assertEqual(collection.calls, 3)

    def test_database_scope_excludes_other_sites_and_importers(self):
        import re
        def matches(doc, query):
            if '$or' in query and not any(matches(doc, part) for part in query['$or']): return False
            for key, condition in query.items():
                if key == '$or': continue
                value = doc.get(key)
                if isinstance(condition, dict):
                    if '$in' in condition and value not in condition['$in']: return False
                    if '$regex' in condition and not re.search(condition['$regex'], value or '', re.I): return False
                elif value != condition: return False
            return True
        self.assertTrue(matches({'imported_from': 'dealer_studio', 'site_location': 'Denza Melbourne'}, CLIENT_SCOPE))
        self.assertTrue(matches({'imported_from': 'dealer_studio', 'site': 'Denza Melbourne'}, CLIENT_SCOPE))
        self.assertTrue(matches({'imported_from': 'dealer_studio', 'site_location': ' DENZA-MELBOURNE '}, CLIENT_SCOPE))
        self.assertFalse(matches({'imported_from': 'saleslogs-csv', 'site_location': 'Denza Melbourne'}, CLIENT_SCOPE))
        self.assertFalse(matches({'imported_from': 'dealer_studio', 'site_location': 'BYD Melbourne', 'site': 'Denza Melbourne'}, CLIENT_SCOPE))

    def test_existing_identity_staff_workflow_and_communication_fields_survive(self):
        doc = {'_id': ObjectId(), 'id': 'saved-id', 'imported_from': 'dealer_studio', 'site_location': 'Denza Melbourne',
               'vy_order_id': '101', 'name': 'Old name', 'stage': 'Ready for Pickup', 'documents': [{'id': 'saved-doc'}],
               'notes': 'Staff note', 'comments': [{'body': 'Staff comment'}], 'assigned_agent_id': 'saved-agent',
               'contact_status': 'Contacted', 'consent_sms': False, 'opted_out': True, 'pdi_complete': True}
        plan = plan_clients([row()], [], [doc])
        updated = apply([doc], plan)[0]
        self.assertFalse(plan['inserts'])
        self.assertEqual(updated['name'], 'Saved Customer')
        for field in ['_id', 'id', 'stage', 'documents', 'notes', 'comments', 'assigned_agent_id', 'contact_status', 'consent_sms', 'opted_out', 'pdi_complete']:
            self.assertEqual(updated[field], doc[field])

    def test_only_denza_dealerstudio_records_can_be_updated(self):
        docs = [{'_id': ObjectId(), 'site_location': site, 'site': 'Denza Melbourne', 'imported_from': 'dealer_studio', 'vy_order_id': '101'}
                for site in ['BYD Caroline Springs', 'BYD Nunawading', 'BYD Melbourne']]
        plan = plan_clients([row()], [], docs)
        self.assertFalse(plan['patches'])
        self.assertEqual(len(plan['inserts']), 1)
        self.assertEqual(next(iter(plan['inserts'].values()))['site_location'], 'Denza Melbourne')

    def test_initial_insert_and_second_run_are_idempotent(self):
        source = [row(), row(102, lead_status='Vehicle Order')]
        first = plan_clients(source, [], [])
        self.assertEqual(len(first['inserts']), 2)
        second = plan_clients(source, [], apply([], first))
        self.assertFalse(second['inserts'])
        self.assertFalse(second['patches'])

    def test_lost_spam_and_cancelled_leads_are_mirrored_but_not_new_delivery_clients(self):
        source = [row(101, lead_status='lost'), row(102, lead_status='spam'), row(103, lead_status='Order Cancelled')]
        plan = plan_clients(source, [], [])
        self.assertFalse(plan['inserts'])
        self.assertEqual(len(raw_documents(source, 'lead')), 3)

    def test_existing_cancelled_or_lost_records_are_retained_and_source_status_updated(self):
        first = plan_clients([row()], [], [])
        existing = apply([], first)
        plan = plan_clients([row(lead_status='Order Cancelled')], [], existing)
        updated = apply(existing, plan)
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]['id'], existing[0]['id'])
        self.assertEqual(updated[0]['dealerstudio_source_status'], 'Order Cancelled')

    def test_delivered_promotes_source_stage_but_never_regresses_on_an_earlier_source_stage(self):
        first = apply([], plan_clients([row()], [], []))
        delivered = apply(first, plan_clients([row(lead_status='Delivered', delivered_at='2026-10-31')], [], first))
        self.assertEqual(delivered[0]['stage'], 'Delivered')
        earlier = apply(delivered, plan_clients([row()], [], delivered))
        self.assertEqual(earlier[0]['stage'], 'Delivered')
        self.assertEqual(earlier[0]['delivery_date'], '2026-10-31')

    def test_stable_source_id_updates_even_when_customer_details_change(self):
        first = apply([], plan_clients([row()], [], []))
        changed = plan_clients([row(name='Updated name', phone='0400999999')], [], first)
        self.assertEqual(len(changed['patches']), 1)
        self.assertFalse(changed['inserts'])

    def test_duplicate_existing_source_ids_block_the_plan(self):
        docs = [{'_id': ObjectId(), 'imported_from': 'dealer_studio', 'site_location': 'Denza Melbourne', 'vy_order_id': '101'} for _ in range(2)]
        plan = plan_clients([row()], [], docs)
        self.assertEqual(len(plan['conflicts']), 1)
        self.assertFalse(plan['inserts'])
        self.assertFalse(plan['patches'])

    def test_recorded_vin_is_joined_only_through_a_unique_stock_identity(self):
        source = [row()]
        cars = [{'stocknum': 'STOCK1', 'vin': 'RECORDEDVIN'}]
        plan = plan_clients(source, cars, [])
        self.assertEqual(next(iter(plan['inserts'].values()))['vin'], 'RECORDEDVIN')
        ambiguous = plan_clients(source, cars + [{'stocknum': 'STOCK1', 'vin': 'DIFFERENTVIN'}], [])
        self.assertIsNone(next(iter(ambiguous['inserts'].values()))['vin'])

    def test_other_dealership_rows_are_rejected(self):
        with self.assertRaises(ValueError):
            plan_clients([row(dealership_id=999)], [], [])

    def test_lead_and_inventory_models_share_storage_without_identity_collisions(self):
        lead = raw_documents([row()], 'lead')[0]
        car = raw_documents([row()], 'car')[0]
        self.assertNotEqual(lead['_id'], car['_id'])
        self.assertEqual(lead['record_type'], 'lead')
        self.assertEqual(car['record_type'], 'car')
        self.assertEqual(lead['source_id'], car['source_id'])

    def test_fresh_snapshot_checks_reject_stale_or_incomplete_exports(self):
        base = Path(__file__).resolve().parents[1] / '.runtime'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='dealerstudio-test-', dir=base) as directory:
            path = Path(directory)
            manifest = {'dealership_slug': 'denza-melbourne', 'dealership_id': '2606', 'complete': True,
                        'fetchedAt': datetime.now(timezone.utc).isoformat(),
                        'statistics': {'total_historic_leads': 1, 'total_active_leads': 1, 'total_cars_in_stock': 0}}
            envelope = {'dealership_slug': 'denza-melbourne', 'dealership_id': '2606', 'lead_clusters': [row()]}
            files = {'COMPLETE_DATA_MANIFEST.json': manifest, 'all_lead_clusters.json': envelope, 'active_lead_clusters.json': envelope,
                     'all_cars_inventory.json': [], 'all_cars_detailed_specs.json': []}
            for name, value in files.items():
                (path / name).write_text(json.dumps(value), encoding='utf8')
            self.assertEqual(len(read_snapshot(path, True)['leads']), 1)
            manifest['fetchedAt'] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            (path / 'COMPLETE_DATA_MANIFEST.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): read_snapshot(path, True)
            manifest['fetchedAt'] = datetime.now(timezone.utc).isoformat()
            manifest['statistics']['total_cars_in_stock'] = 1
            (path / 'COMPLETE_DATA_MANIFEST.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): read_snapshot(path, True)


if __name__ == '__main__':
    unittest.main()
