import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch.dict(os.environ, {'MONGO_URL': 'mongodb://fixture', 'DB_NAME': 'fixture', 'JWT_SECRET': 'fixture'}):
    from routers.staff import list_staff, site_regex


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_):
        return self

    async def to_list(self, length=None):
        return self.rows


class Collection:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.projection = None

    def find(self, query, projection=None):
        self.query = query
        self.projection = projection
        return Cursor(self.rows)


class StaffDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def test_site_aliases_keep_brands_and_other_dealerships_separate(self):
        self.assertTrue(site_regex('Caroline Springs').match('BYD Caroline Springs'))
        self.assertTrue(site_regex('BYD Melbourne City VIC').match('Melbourne'))
        self.assertFalse(site_regex('BYD Melbourne').match('Denza Melbourne'))
        self.assertFalse(site_regex('BYD Doncaster').match('BYD Nunawading'))

    async def test_non_admin_can_read_staff_and_requested_site_cannot_override_the_lock(self):
        users = Collection([{'_id': 'mongo-id', 'id': 'staff-id', 'name': 'Staff', 'active': True}])
        members = Collection([{'email': 'shared@example.com'}])
        db = SimpleNamespace(users=users, staff_site_memberships=members)
        actor = SimpleNamespace(role='agent', locked_site='BYD Nunawading')
        with patch('routers.staff.get_db', return_value=db):
            result = await list_staff(site='BYD Doncaster', user=actor)
        self.assertEqual(result[0]['id'], 'staff-id')
        self.assertNotIn('_id', result[0])
        self.assertNotIn('password_hash', users.projection)
        self.assertEqual(users.query['active'], {'$ne': False})
        site_filter = users.query['$or'][0]['site']
        self.assertTrue(site_filter.match('BYD Nunawading'))
        self.assertFalse(site_filter.match('BYD Doncaster'))
        self.assertTrue(users.query['$or'][-1]['email']['$in'][0].match(' SHARED@EXAMPLE.COM '))

    async def test_unlocked_staff_can_select_a_site_without_needing_admin_rights(self):
        users = Collection([])
        db = SimpleNamespace(users=users, staff_site_memberships=Collection([]))
        actor = SimpleNamespace(role='agent', locked_site=None)
        with patch('routers.staff.get_db', return_value=db):
            await list_staff(site='Denza Melbourne', user=actor)
        regex = users.query['$or'][0]['site']
        self.assertTrue(regex.match('DENZA Melbourne'))
        self.assertFalse(regex.match('BYD Melbourne'))


if __name__ == '__main__':
    unittest.main()
