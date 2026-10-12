import os
import unittest
from copy import deepcopy
from unittest.mock import patch
from bson import ObjectId

with patch.dict(os.environ, {'MONGO_URL': 'mongodb://fixture', 'DB_NAME': 'fixture'}):
    from scripts.import_response_all_departments import plan_import, protected_hash

CAROLINE = 'BYD Caroline Springs'
NUNAWADING = 'BYD Nunawading'


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


if __name__ == '__main__':
    unittest.main()
