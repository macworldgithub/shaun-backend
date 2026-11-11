from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from models import User
from routers import virtualyard
from tests.contractor_fixtures import Cursor


class DealsCursor(Cursor):
    def __init__(self, data):
        super().__init__(data)
        self.offset, self.maximum = 0, len(self.data)

    def skip(self, value):
        self.offset = value
        return self

    def limit(self, value):
        self.maximum = value
        return self

    def allow_disk_use(self, value):
        return self

    async def generate(self):
        for item in self.data[self.offset:self.offset + self.maximum]:
            yield dict(item)


@pytest.mark.parametrize('page,expected', [(1, ['0', '1', '2']), (2, ['3', '4', '5']), (3, ['6']), (4, [])])
def test_deals_pages_keep_filters_totals_and_strip_mongo_ids(monkeypatch, page, expected):
    collection = MagicMock()
    collection.count_documents = AsyncMock(return_value=7)
    stats = SimpleNamespace(to_list=AsyncMock(return_value=[{'_id': None, 'total_deals': 7}]))
    collection.aggregate.return_value = stats
    collection.find.return_value = DealsCursor([{'_id': ObjectId(), 'invoice_id': str(i)} for i in range(7)])
    monkeypatch.setattr(virtualyard, 'get_db', lambda: SimpleNamespace(virtual_yard_deals=collection))
    app = FastAPI()
    app.include_router(virtualyard.router)
    app.dependency_overrides[get_current_user] = lambda: User(name='Fixture', email='fixture@example.com', role='super_admin')
    with TestClient(app) as client:
        response = client.get('/api/virtualyard/deals', params={
            'page': page, 'limit': 3, 'site': 'BYD Nunawading', 'status': 'Approved',
            'settlement_status': 'Paid', 'payment_method': 'EFT', 'has_payments': 'true', 'q': 'Fixture'})
    assert response.status_code == 200
    body = response.json()
    assert [item['invoice_id'] for item in body['items']] == expected
    assert all('_id' not in item for item in body['items'])
    assert (body['page'], body['pages'], body['total']) == (page, 3, 7)
    assert body['stats'] == {'total_deals': 7}
    collection.count_documents.assert_not_awaited()
    query = collection.find.call_args.args[0]
    assert query == collection.aggregate.call_args.args[0][0]['$match']
    assert query['location_name']['$regex'] == 'Nunawading'
    assert query['status'] == 'Approved'
    assert query['settlement_status'] == 'Paid'
    assert query['payments.method']['$regex'] == '^EFT$'
    assert query['payment_count'] == {'$gt': 0}
    assert query['$or'][0]['customer_name']['$regex'] == 'Fixture'
