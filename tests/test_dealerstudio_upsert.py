import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bson import ObjectId
from scripts.import_dealerstudio import plan_clients, read_snapshot, raw_documents


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
