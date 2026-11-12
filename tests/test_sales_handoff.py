import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth import get_current_user
from models import User, Client
from routers import sales_handoff


def fixture(monkeypatch, site='BYD Fairfield'):
    user = User(id='delivery-user', name='Delivery Consultant', email='delivery@example.com', locked_site=site)
    client = {'id': 'client-1', 'name': 'Fixture', 'phone': '+61499918523', 'vehicle': 'BYD Seal',
              'site_location': 'BYD Fairfield', 'handoff_status': 'new', 'crm_customer_id': 'customer-1',
              'delivery_handoff': {'promises': 'Include mats', 'lead_time': '2 weeks', 'payment_method': 'Finance', 'finance_provider': 'Test bank'},
              'comments': [{'id': 'sales-message', 'channel': 'sales_delivery', 'team': 'sales', 'author_name': 'Sales Consultant', 'body': 'Question', 'created_at': '2026-11-12T08:00:00Z'}]}
    db = SimpleNamespace(clients=MagicMock())
    db.clients.find_one = AsyncMock(return_value=client)
    db.clients.update_one = AsyncMock()
    monkeypatch.setattr(sales_handoff, 'get_db', lambda: db)
    app = FastAPI(); app.include_router(sales_handoff.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), db, client


def test_shared_sales_message_and_structured_handoff_are_visible(monkeypatch):
    api, db, row = fixture(monkeypatch)
    response = api.get('/api/clients/client-1/sales-chat')
    assert response.status_code == 200
    assert response.json()['handoff']['promises'] == 'Include mats'
    assert response.json()['messages'][0]['team'] == 'sales'
    # Main Client serialization must preserve the shared fields and chat metadata.
    assert Client(**row).model_dump()['comments'][0]['channel'] == 'sales_delivery'


def test_delivery_reply_is_attributed_and_notifies_sales(monkeypatch):
    api, db, _ = fixture(monkeypatch)
    response = api.post('/api/clients/client-1/sales-chat', json={'body': '  Arrival confirmed  ', 'author_name': 'Spoofed', 'team': 'sales'})
    assert response.status_code == 201
    message = response.json()
    assert message['author_name'] == 'Delivery Consultant' and message['team'] == 'delivery'
    assert message['body'] == 'Arrival confirmed'
    update = db.clients.update_one.call_args.args[1]
    assert update['$inc']['sales_unread'] == 1
    assert update['$set']['last_delivery_message_id'] == message['id']


@pytest.mark.parametrize('body', ['', '  ', 'x' * 2001])
def test_invalid_messages_do_not_write(monkeypatch, body):
    api, db, _ = fixture(monkeypatch)
    assert api.post('/api/clients/client-1/sales-chat', json={'body': body}).status_code == 422
    db.clients.update_one.assert_not_awaited()


def test_other_site_cannot_read_send_or_acknowledge(monkeypatch):
    api, db, _ = fixture(monkeypatch, 'BYD Nunawading')
    assert api.get('/api/clients/client-1/sales-chat').status_code == 404
    assert api.post('/api/clients/client-1/sales-chat', json={'body': 'Question'}).status_code == 404
    assert api.post('/api/clients/client-1/sales-chat/read', json={}).status_code == 404
    db.clients.update_one.assert_not_awaited()


def test_read_marker_is_guarded_against_a_new_message_race(monkeypatch):
    api, db, _ = fixture(monkeypatch)
    assert api.post('/api/clients/client-1/sales-chat/read', json={'last_message_id': 'last-seen'}).status_code == 200
    assert db.clients.update_one.call_args.args[0]['last_sales_message_id'] == 'last-seen'
