import os
import unittest
from copy import deepcopy
from unittest.mock import patch
from bson import ObjectId

with patch.dict(os.environ, {'MONGO_URL': 'mongodb://fixture', 'DB_NAME': 'fixture'}):
    from scripts.import_response_all_departments import plan_import, protected_hash

CAROLINE = 'BYD Caroline Springs'
NUNAWADING = 'BYD Nunawading'
DENZA = 'Denza Melbourne'


def row(source_id='1001', **fields):
    return {'id': source_id, 'DriverName': 'Existing Customer', 'PhoneNo': '0400123456',
            'OrderNo': 'ORDER1', 'VDType': 'ATTO 3', 'VehLocation': 'ON WATER',
            'OrderDate': '2026-10-01', **fields}


def payload(caroline=None, nunawading=None, melbourne=None):
    return {'departments': {
        'BYD Caroline Springs - New': {'id': 'caroline', 'data': {'list': caroline or []}},
        'BYD Nunawading - New': {'id': 'nunawading', 'data': {'list': nunawading or []}},
        'Denza Melbourne - New': {'id': 'melbourne', 'data': {'list': melbourne or []}},
    }}


def applied(documents, plan):
    result = deepcopy(documents)
    for document in result:
        document.update(plan['patches'].get(str(document['_id']), {}))
    return result + list(plan['inserts'].values())


class SaleslogsDepartmentUpsertTests(unittest.TestCase):
    def test_updates_source_values_without_overwriting_ids_or_staff_workflow(self):
        document = {'_id': ObjectId(), 'id': 'existing-uuid', 'site_location': CAROLINE,
                    'vy_order_id': 'ORDER1', 'name': 'Old Name', 'phone': '0400000000',
                    'notes': 'Staff note', 'documents': [{'title': 'Licence'}],
                    'consent_sms': False, 'opted_out': True,
                    'comments': [{'body': 'Staff conversation'}], 'assigned_agent_id': 'staff-id'}
        original = deepcopy(document)
        plan = plan_import(payload([row()]), [document], [])
        result = applied([document], plan)[0]
        self.assertEqual(len(plan['patches']), 1)
        self.assertFalse(plan['inserts'])
        self.assertEqual(result['name'], 'Existing Customer')
        for field in ['_id', 'id', 'notes', 'documents', 'consent_sms', 'opted_out', 'comments', 'assigned_agent_id']:
            self.assertEqual(result[field], original[field])
        self.assertEqual(document, original)

    def test_melbourne_and_other_sites_are_not_updated_even_with_matching_identifiers(self):
        documents = [{'_id': ObjectId(), 'id': 'melbourne', 'site_location': 'Denza Melbourne', 'vy_order_id': 'ORDER1'},
                     {'_id': ObjectId(), 'id': 'byd-melbourne', 'site_location': 'BYD Melbourne', 'vy_order_id': 'ORDER1'}]
        plan = plan_import(payload([row()], melbourne=[row('999')]), documents, [])
        self.assertFalse(plan['patches'])
        self.assertEqual(len(plan['inserts']), 1)
        self.assertEqual(protected_hash(documents), protected_hash(applied(documents, plan)))
        self.assertTrue(all(doc['site_location'] == CAROLINE for doc in plan['inserts'].values()))

    def test_both_sites_upsert_and_second_run_has_no_changes(self):
        source = payload([row()], [row('2001')])
        first = plan_import(source, [], [])
        self.assertEqual(len(first['inserts']), 2)
        documents = applied([], first)
        second = plan_import(source, documents, [])
        self.assertFalse(second['patches'])
        self.assertFalse(second['inserts'])
        self.assertEqual(second['counts'][CAROLINE]['unchanged'], 1)
        self.assertEqual(second['counts'][NUNAWADING]['unchanged'], 1)

    def test_stable_deal_identity_updates_even_when_contact_and_order_change(self):
        first = plan_import(payload([row()]), [], [])
        documents = applied([], first)
        changed = plan_import(payload([row(OrderNo='ORDER2', PhoneNo='0400999999')]), documents, [])
        self.assertEqual(len(changed['patches']), 1)
        self.assertFalse(changed['inserts'])
        result = applied(documents, changed)[0]
        self.assertEqual(result['id'], documents[0]['id'])
        self.assertEqual(result['vy_order_id'], 'ORDER2')

    def test_placeholder_identifiers_do_not_merge_unrelated_existing_deals(self):
        document = {'_id': ObjectId(), 'id': 'other', 'site_location': CAROLINE, 'vin': 'TBA', 'rego': 'TBC', 'vy_order_id': '0', 'name': 'Another Customer'}
        plan = plan_import(payload([row(OrderNo='0', Vin='TBA', Rego='TBC')]), [document], [])
        self.assertFalse(plan['patches'])
        self.assertEqual(len(plan['inserts']), 1)

    def test_distinct_source_deals_for_same_customer_are_not_collapsed(self):
        plan = plan_import(payload([row('1001'), row('1002')]), [], [])
        self.assertEqual(len(plan['inserts']), 2)
        self.assertFalse(plan['conflicts'])

    def test_missing_requested_department_stops_the_import(self):
        source = payload([row()])
        del source['departments']['BYD Nunawading - New']
        with self.assertRaises(ValueError):
            plan_import(source, [], [])

    def test_routes_denza_rows_from_either_department_and_ignores_denza_export(self):
        source = payload([row(SITE='Harmony DENZA - VIC')],
                         [row('2001', SITE='Harmony DENZA - VIC')],
                         [row('ignored', SITE='Harmony DENZA - VIC')])
        plan = plan_import(source, [], [], include_denza=True)
        self.assertEqual(len(plan['inserts']), 2)
        self.assertTrue(all(doc['site_location'] == DENZA for doc in plan['inserts'].values()))
        self.assertEqual(plan['counts'][DENZA]['inserted'], 2)
        second = plan_import(source, applied([], plan), [], include_denza=True)
        self.assertFalse(second['patches'])
        self.assertFalse(second['inserts'])

    def test_moves_existing_deal_in_place_using_source_identity(self):
        first = plan_import(payload(nunawading=[row()]), [], [])
        documents = applied([], first)
        documents[0]['notes'] = 'Keep staff notes'
        documents[0]['site'] = NUNAWADING
        source = payload(nunawading=[row(SITE='Harmony DENZA - VIC')])
        plan = plan_import(source, documents, [], include_denza=True)
        self.assertFalse(plan['inserts'])
        result = applied(documents, plan)[0]
        self.assertEqual(result['site_location'], DENZA)
        self.assertEqual(result['site'], DENZA)
        for field in ['_id', 'id', 'notes']:
            self.assertEqual(result[field], documents[0][field])
        self.assertFalse(plan_import(source, applied(documents, plan), [], include_denza=True)['patches'])

    def test_backfills_historical_and_cancelled_records_missing_from_current_export(self):
        documents = [{'_id': ObjectId(), 'id': 'historical', 'site_location': NUNAWADING,
                      'stage': 'Delivered', 'saleslogs_source_data': {'SITE': 'Harmony DENZA - VIC'}}]
        plan = plan_import(payload(), documents, [], include_denza=True)
        self.assertEqual(len(plan['migrated_to_denza']), 1)
        result = applied(documents, plan)
        self.assertEqual(result[0]['site_location'], DENZA)
        self.assertEqual(result[0]['stage'], 'Delivered')
        self.assertEqual(protected_hash(documents), protected_hash(result))
        self.assertFalse(plan_import(payload(), result, [], include_denza=True)['patches'])

    def test_denza_source_ids_are_scoped_by_original_department(self):
        source = payload([row(SITE='Harmony DENZA - VIC')], [row(SITE='Harmony DENZA - VIC')])
        first = plan_import(source, [], [], include_denza=True)
        self.assertEqual(len(first['inserts']), 2)
        self.assertFalse(plan_import(source, applied([], first), [], include_denza=True)['patches'])

    def test_default_excludes_denza_rows_from_both_byd_departments(self):
        source = payload([row(), row('denza-cs', SITE='Harmony DENZA - VIC')],
                         [row('2001'), row('denza-nw', SITE='Harmony DENZA - VIC')],
                         [row('denza-export', SITE='Harmony DENZA - VIC')])
        plan = plan_import(source, [], [])
        self.assertEqual(len(plan['inserts']), 2)
        self.assertEqual({doc['site_location'] for doc in plan['inserts'].values()}, {CAROLINE, NUNAWADING})
        self.assertEqual(plan['counts'][DENZA]['skipped_excluded'], 2)
        self.assertFalse(plan['migrated_to_denza'])
        self.assertFalse(plan_import(source, applied([], plan), [])['inserts'])

    def test_default_preserves_historical_denza_and_dealer_studio_records(self):
        documents = [{'_id': ObjectId(), 'id': 'historical', 'site_location': NUNAWADING,
                      'vy_order_id': 'ORDER1', 'saleslogs_source_data': {'SITE': 'Harmony DENZA - VIC'}},
                     {'_id': ObjectId(), 'id': 'dealer-studio', 'site_location': DENZA,
                      'vy_order_id': 'ORDER1', 'imported_from': 'dealer_studio'}]
        before = deepcopy(documents)
        plan = plan_import(payload(nunawading=[row()]), documents, [])
        self.assertFalse(plan['patches'])
        self.assertFalse(plan['migrated_to_denza'])
        self.assertEqual(len(plan['inserts']), 1)
        self.assertEqual(documents, before)


if __name__ == '__main__':
    unittest.main()
