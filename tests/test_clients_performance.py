import asyncio
import json
import re
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

import db as db_module
from auth import get_current_user
from models import Client, User
from routers import clients as module


def record(identifier, site='Denza Melbourne', **fields):
    return {
        'id': identifier, 'name': 'Fixture Client', 'phone': '0400000000',
        'vehicle': 'BYD Seal', 'site_location': site,
        'created_at': datetime(2026, 10, 1, tzinfo=timezone.utc),
        'updated_at': datetime(2026, 10, 1, tzinfo=timezone.utc), **fields,
    }


class Cursor:
    def __init__(self, docs):
        self.docs = docs
        self.offset = 0

    def sort(self, order):
        self.order = order
        return self

    def allow_disk_use(self, enabled):
        return self

    def skip(self, offset):
        self.offset = offset
        return self

    def limit(self, limit):
        self.maximum = limit
        return self

    def batch_size(self, size):
        self.batch = size
        return self

    async def to_list(self, length):
        assert length == self.maximum
        return self.docs[self.offset:self.offset + length]


class Collection:
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def find(self, query, projection, **kwargs):
        self.calls.append((query, projection))
        docs = self.docs
        if 'site_location' in query:
            pattern = query['site_location']['$regex']
            docs = [d for d in docs if re.search(pattern, d['site_location'], re.I)]
        self.cursor = Cursor(docs)
        return self.cursor

    async def find_one(self, query, projection):
        self.calls.append((query, projection))
        return next((dict(d) for d in self.docs if d['id'] == query['id']), None)


@pytest.fixture
def api(monkeypatch):
    collection = Collection([])
    monkeypatch.setattr(module, 'get_db', lambda: SimpleNamespace(clients=collection))
    user = User(id='staff', name='Fixture', email='fixture@example.com', role='admin')
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, collection, user


def test_list_preserves_full_response_and_computed_business_rules(api):
    client, collection, _ = api
    collection.docs = [record('one', payment_complete=True, pdi_complete=True,
                              registration_docs_complete=True, activation_status='Ready',
                              documents=[{'id': 'document', 'status': 'complete'}],
                              comments=[{'id': 'comment', 'author_name': 'Fixture Staff', 'body': 'Fixture comment',
                                         'created_at': datetime(2026, 10, 1, tzinfo=timezone.utc)}])]
    response = client.get('/api/clients')
    assert response.status_code == 200
    assert response.json() == jsonable_encoder([Client(**collection.docs[0])])
    assert response.json()[0]['ready_for_delivery'] is True
    assert response.json()[0]['activation_ready'] is True
    assert response.headers['content-encoding'] == 'gzip'
    assert response.headers['content-type'] == 'application/json'
    assert 'Accept-Encoding' in response.headers['vary']
    assert collection.cursor.batch == 1000
    assert collection.cursor.order == [('delivery_date', 1), ('created_at', -1)]
    assert collection.calls[0][1] == {**dict.fromkeys(Client.model_fields, 1), '_id': 0}


def test_bounded_pagination_keeps_array_contract(api):
    client, collection, _ = api
    collection.docs = [record(str(i)) for i in range(4)]
    response = client.get('/api/clients?limit=2&offset=1')
    assert [row['id'] for row in response.json()] == ['1', '2']
    assert collection.cursor.batch == 2
    assert client.get('/api/clients?offset=20').json() == []


@pytest.mark.parametrize('encoding,compressed', [('gzip', True), ('gzip;q=0', False),
                                                  ('identity', False), ('*;q=0.5', True),
                                                  ('gzip;q=0, *;q=1', False)])
def test_compression_respects_client_encoding_preferences(api, encoding, compressed):
    client, collection, _ = api
    collection.docs = [record('one')]
    response = client.get('/api/clients', headers={'Accept-Encoding': encoding})
    assert response.status_code == 200
    assert (response.headers.get('content-encoding') == 'gzip') is compressed
    assert response.json()[0]['id'] == 'one'


def test_empty_list_does_not_pay_compression_overhead(api):
    client, _, _ = api
    response = client.get('/api/clients')
    assert response.content == b'[]'
    assert 'content-encoding' not in response.headers


@pytest.mark.parametrize('params', ['limit=0', 'limit=-1', 'limit=5001', 'offset=-1'])
def test_invalid_pagination_rejected_before_query(api, params):
    client, collection, _ = api
    assert client.get('/api/clients?' + params).status_code == 422
    assert collection.calls == []


def test_locked_site_overrides_requested_site_on_list_and_detail(api):
    client, collection, user = api
    user.locked_site = 'denza melbourne'
    collection.docs = [record('allowed'), record('blocked', site='BYD Melbourne')]
    response = client.get('/api/clients?site_location=BYD%20Melbourne')
    assert [row['id'] for row in response.json()] == ['allowed']
    assert client.get('/api/clients/blocked').status_code == 404
    assert client.get('/api/clients/allowed').json()['id'] == 'allowed'


def test_search_combines_with_assignment_filters(api):
    client, collection, _ = api
    assert client.get('/api/clients?search=Fixture&unassigned=true').status_code == 200
    query = collection.calls[0][0]
    assert query['$or'] == [{'assigned_agent_id': None}, {'assigned_agent_id': ''}]
    assert len(query['$and'][0]['$or']) == 6
    client.get('/api/clients?mine=true&stage=Scheduled&arrived=true')
    query = collection.calls[-1][0]
    assert query['assigned_agent_id'] == 'staff'
    assert query['stage'] == 'Scheduled'
    assert query['arrived'] is True


def test_large_list_encoding_runs_outside_event_loop(monkeypatch):
    caller_thread = threading.get_ident()
    threads = []
    original = module._client_list_response

    def encode(rows, accept_encoding):
        threads.append(threading.get_ident())
        return original(rows, accept_encoding)

    monkeypatch.setattr(module, '_client_list_response', encode)
    collection = Collection([record('one')])
    monkeypatch.setattr(module, 'get_db', lambda: SimpleNamespace(clients=collection))
    user = User(id='staff', name='Fixture', email='fixture@example.com', role='admin')
    response = asyncio.run(module.list_clients(user=user))
    assert json.loads(response.body)[0]['id'] == 'one'
    assert threads and threads[0] != caller_thread


def test_startup_creates_client_and_authentication_lookup_indexes(monkeypatch):
    class Database:
        def __init__(self):
            self.collections = {}

        def __getattr__(self, name):
            return self.collections.setdefault(name, SimpleNamespace(create_index=AsyncMock()))

    database = Database()
    monkeypatch.setattr(db_module, 'get_db', lambda: database)
    asyncio.run(db_module.ensure_indexes())
    database.clients.create_index.assert_any_await('id')
    database.users.create_index.assert_any_await('id')
