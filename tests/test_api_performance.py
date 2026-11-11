import asyncio
import threading
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from models import User
from routers import clients, auth as auth_routes
from services import thread_history
from tests.test_clients_performance import Cursor, record


def async_cursor(rows):
    cursor = MagicMock()
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    return cursor


def test_alerts_batch_shared_offers_without_per_customer_queries(monkeypatch):
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    customers = [record(str(i), linked_offer_ids=['offer'], document_completeness='Partial') for i in range(100)]
    db = SimpleNamespace(clients=MagicMock(), offers=MagicMock())
    db.clients.find.return_value = async_cursor(customers)
    db.offers.find.return_value = async_cursor([{'id': 'offer', 'name': 'Fixture offer', 'deliver_by': yesterday}])
    db.offers.find_one = AsyncMock()
    monkeypatch.setattr(clients, 'get_db', lambda: db)
    user = User(name='Fixture', email='fixture@example.com', locked_site='Denza Melbourne')
    alerts = asyncio.run(clients.client_alerts(user))
    assert len(alerts) == 200
    assert len([alert for alert in alerts if alert['type'] == 'offer']) == 100
    db.offers.find.assert_called_once()
    db.offers.find_one.assert_not_awaited()
    assert db.offers.find.call_args.args[0] == {'id': {'$in': ['offer']}}
    assert 'documents' not in db.clients.find.call_args.args[1]
    assert db.clients.find.call_args.args[0]['site_location']['$regex'] == '^Denza\\ Melbourne$'


def test_history_fetches_contact_owners_only_for_inbox_phones_and_joins_off_loop(monkeypatch):
    db = SimpleNamespace(clients=MagicMock(), messages=MagicMock())
    db.clients.find.return_value = async_cursor([{'id': 'client', 'phone': '0499918523', 'site_location': 'Fairfield'}])
    db.messages.find.return_value = async_cursor([{'id': 'message', 'phone': '+61499918523', 'body': 'Fixture reply',
        'direction': 'inbound', 'site': 'Fairfield', 'sent_at': '2026-11-12T00:00:00Z'}])
    monkeypatch.setattr(thread_history, 'get_db', lambda: db)
    monkeypatch.setattr(thread_history, 'get_lead_db', MagicMock(side_effect=RuntimeError('not configured')))
    original = thread_history._merge_history
    caller = threading.get_ident()
    worker_threads = []
    def join(*args):
        worker_threads.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(thread_history, '_merge_history', join)
    user = User(name='Fixture', email='fixture@example.com', active_site='Fairfield')
    rows = asyncio.run(thread_history.history(user))
    assert rows[0]['body'] == 'Fixture reply'
    assert db.clients.find.call_args.args[0].get('$or')
    assert len(db.clients.find.call_args.args[0]['$or']) == 1
    assert worker_threads and worker_threads[0] != caller


def test_large_inbox_identity_work_scales_linearly(monkeypatch):
    count = 600
    contacts = [{'id': str(i), 'phone': f'+614{i:08d}', 'site_location': 'Fairfield'} for i in range(count)]
    messages = [{'id': f'm-{i}', 'client_id': str(i), 'phone': f'+614{i:08d}', 'body': f'message {i}',
                 'direction': 'inbound', 'sent_at': '2026-11-12T00:00:00Z'} for i in range(count)]
    original = thread_history.phone_key
    calls = 0
    def normalize(value):
        nonlocal calls
        calls += 1
        return original(value)
    monkeypatch.setattr(thread_history, 'phone_key', normalize)
    user = User(name='Fixture', email='fixture@example.com', role='super_admin')
    rows = thread_history._merge_history(user, None, None, None, 200, contacts, messages, [], [])
    assert len(rows) == 200 and rows[0]['client_id'] == '0' and rows[-1]['client_id'] == '199'
    assert calls < count * 20


def test_inbox_keeps_client_link_when_the_customers_phone_changed(monkeypatch):
    db = SimpleNamespace(clients=MagicMock(), messages=MagicMock())
    db.messages.find.return_value = async_cursor([{'id': 'old-phone-message', 'client_id': 'client',
        'phone': '+61499918523', 'direction': 'inbound', 'body': 'Fixture', 'sent_at': '2026-11-12T00:00:00Z'}])
    db.clients.find.return_value = async_cursor([{'id': 'client', 'phone': '+61400000000', 'site_location': 'Fairfield'}])
    monkeypatch.setattr(thread_history, 'get_db', lambda: db)
    monkeypatch.setattr(thread_history, 'get_lead_db', MagicMock(side_effect=RuntimeError('not configured')))
    rows = asyncio.run(thread_history.history(User(name='Fixture', email='fixture@example.com', active_site='Fairfield')))
    assert rows[0]['client_id'] == 'client' and rows[0]['site'] == 'Fairfield'
    assert {'id': {'$in': ['client']}} in db.clients.find.call_args.args[0]['$or']


def test_inbox_preserves_explicit_site_messages_without_a_known_phone(monkeypatch):
    db = SimpleNamespace(clients=MagicMock(), messages=MagicMock())
    db.messages.find.return_value = async_cursor([{'id': 'legacy', 'phone': 'unknown', 'site': 'Fairfield',
        'direction': 'inbound', 'body': 'Fixture', 'sent_at': '2026-11-12T00:00:00Z'}])
    monkeypatch.setattr(thread_history, 'get_db', lambda: db)
    monkeypatch.setattr(thread_history, 'get_lead_db', MagicMock(side_effect=RuntimeError('not configured')))
    rows = asyncio.run(thread_history.history(User(name='Fixture', email='fixture@example.com', active_site='Fairfield')))
    assert rows[0]['id'] == 'legacy'
    db.clients.find.assert_not_called()


@pytest.fixture
def page_api(monkeypatch):
    collection = MagicMock()
    collection.find.side_effect = lambda *args, **kwargs: Cursor([record(str(i)) for i in range(53)])
    collection.count_documents = AsyncMock(side_effect=lambda query: 4 if 'payment_complete' in str(query) else 53)
    monkeypatch.setattr(clients, 'get_db', lambda: SimpleNamespace(clients=collection))
    user = User(name='Fixture', email='fixture@example.com', locked_site='Denza Melbourne')
    app = FastAPI(); app.include_router(clients.router)
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as api:
        yield api, collection


def test_page_fetches_only_requested_rows_with_exact_counts_and_readiness(page_api):
    api, collection = page_api
    response = api.get('/api/clients/page?page=3&limit=20')
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body['total'], body['pages'], body['ready_count']) == (53, 3, 4)
    assert [row['id'] for row in body['items']] == [str(i) for i in range(40, 53)]
    assert all('documents' not in row and 'booking_type' in row for row in body['items'])
    assert collection.count_documents.await_count == 2
    assert response.headers['content-encoding'] == 'gzip'


def test_page_combines_assignment_search_and_enforced_site(page_api):
    api, collection = page_api
    assert api.get('/api/clients/page?search=A.*&unassigned=true&site_location=Fairfield').status_code == 200
    query = collection.find.call_args.args[0]
    assert query['$and'][0]['site_location']['$regex'] == '^Denza\\ Melbourne$'
    assert query['$and'][1]['$or'][0]['name']['$regex'] == r'A\.\*'
    assert query['$and'][2] == {'assigned_agent_id': {'$in': [None, '']}}


def test_calendar_view_preserves_readiness_without_unneeded_contact_fields(page_api):
    api, collection = page_api
    response = api.get('/api/clients?calendar=true&limit=5')
    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert {'id', 'name', 'vehicle', 'booking_type', 'delivery_time', 'ready_for_delivery'} <= row.keys()
    assert 'email' not in row and 'phone' not in row and 'vin' not in row
    assert 'email' not in collection.find.call_args.args[1]
    assert 'phone' in collection.find.call_args.args[1]  # validated, then omitted from the response


@pytest.mark.parametrize('params', ['page=0', 'limit=0', 'limit=201', 'exception=unknown'])
def test_invalid_page_rejected_without_database_work(page_api, params):
    api, collection = page_api
    assert api.get('/api/clients/page?' + params).status_code == 422
    collection.find.assert_not_called()
    collection.count_documents.assert_not_awaited()


def test_login_password_work_does_not_block_the_event_loop(monkeypatch):
    db = SimpleNamespace(users=SimpleNamespace(find_one=AsyncMock(return_value={
        'id': 'fixture', 'name': 'Fixture', 'email': 'fixture@example.com', 'password_hash': 'fixture-hash',
    }), update_one=AsyncMock()))
    monkeypatch.setattr(auth_routes, 'get_db', lambda: db)
    threads = {}
    def verify(*args):
        threads['bcrypt'] = threading.get_ident()
        return True
    async def audit(*args, **kwargs):
        threads['loop'] = threading.get_ident()
    monkeypatch.setattr(auth_routes, 'verify_password', verify)
    monkeypatch.setattr(auth_routes, '_audit', audit)
    app = FastAPI(); app.include_router(auth_routes.router)
    with TestClient(app) as api:
        response = api.post('/api/auth/login', json={'email': 'fixture@example.com', 'password': 'fixture'})
    assert response.status_code == 200
    assert threads['bcrypt'] != threads['loop']
