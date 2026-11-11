"""Isolated API regressions for Harmony's delivery-centre critical updates."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from auth import get_current_user, require_admin
from models import User
from routers import clients, sms, admin
from services.delivery_booking import booking_key, SLOT_TIMES
from services import thread_history


PHONE = '+61499918523'
DAY = '2026-11-20'
SITE = 'BYD Caroline Springs'


def customer(**fields):
    return {'id': 'fixture-client', 'name': 'Critical Updates Fixture', 'phone': PHONE,
            'vehicle': 'BYD Seal', 'site_location': SITE, 'delivery_date': DAY,
            'booking_type': 'estimated', **fields}


def cursor(rows):
    value = MagicMock()
    for name in ('sort', 'limit', 'skip', 'allow_disk_use', 'batch_size'):
        getattr(value, name).return_value = value
    value.to_list = AsyncMock(return_value=rows)
    value.__aiter__.return_value = rows
    return value


@pytest.fixture
def api(monkeypatch):
    db = SimpleNamespace(clients=MagicMock(), messages=MagicMock(), users=MagicMock())
    for collection in (db.clients, db.messages, db.users):
        collection.find.return_value = cursor([])
        collection.find_one = AsyncMock(return_value=None)
        collection.insert_one = AsyncMock()
        collection.update_one = AsyncMock()
        collection.update_many = AsyncMock()
        collection.find_one_and_update = AsyncMock()
    for module in (clients, sms, thread_history, admin):
        monkeypatch.setattr(module, 'get_db', lambda: db)
    monkeypatch.setattr(sms, 'get_lead_db', MagicMock(side_effect=RuntimeError('not configured')))
    monkeypatch.setattr(thread_history, 'get_lead_db', MagicMock(side_effect=RuntimeError('not configured')))
    monkeypatch.setattr(clients, 'sync_client_updates_to_contractor_jobs', AsyncMock())
    user = User(id='fixture-staff', name='Fixture Staff', email='fixture@example.com',
                role='delivery_consultant', locked_site=SITE, active_site=SITE)
    app = FastAPI()
    for router in (clients.router, sms.router, admin.router):
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, db, user


def test_estimated_booking_does_not_reserve_time(api):
    client, db, _ = api
    response = client.post('/api/clients', json=customer())
    assert response.status_code == 201, response.text
    assert response.json()['booking_type'] == 'estimated'
    assert db.clients.insert_one.call_args.args[0]['delivery_slot_key'] is None


def test_confirmed_booking_persists_site_local_slot(api):
    client, db, _ = api
    response = client.post('/api/clients', json=customer(booking_type='confirmed', delivery_time='09:30'))
    assert response.status_code == 201, response.text
    assert response.json()['delivery_time'] == '09:30'
    assert db.clients.insert_one.call_args.args[0]['delivery_slot_key'] == 'carolinesprings|2026-11-20|09:30'


@pytest.mark.parametrize('fields', [
    {'booking_type': 'confirmed'}, {'booking_type': 'confirmed', 'delivery_date': None, 'delivery_time': '09:30'},
    {'delivery_time': '25:00'}, {'delivery_time': '07:30'}, {'delivery_time': '09:15'},
    {'delivery_date': '2026-02-30'}, {'delivery_date': '20/11/2026'}, {'booking_type': 'invalid'},
])
def test_invalid_booking_rejected_before_writing(api, fields):
    client, db, _ = api
    assert client.post('/api/clients', json=customer(**fields)).status_code == 422
    db.clients.insert_one.assert_not_awaited()


def test_duplicate_confirmation_returns_conflict(api):
    client, db, _ = api
    db.clients.insert_one.side_effect = DuplicateKeyError('slot already reserved')
    response = client.post('/api/clients', json=customer(booking_type='confirmed', delivery_time='09:30'))
    assert response.status_code == 409
    assert 'another time' in response.json()['detail']


def test_reschedule_and_demote_release_old_unique_key(api):
    client, db, _ = api
    existing = customer(booking_type='confirmed', delivery_time='09:30')
    db.clients.find_one.return_value = existing
    db.clients.find_one_and_update.side_effect = lambda query, update, **kwargs: {**existing, **update['$set']}
    response = client.patch('/api/clients/fixture-client', json={'delivery_time': '10:00'})
    assert response.status_code == 200, response.text
    assert db.clients.find_one_and_update.call_args.args[1]['$set']['delivery_slot_key'].endswith('|10:00')
    response = client.patch('/api/clients/fixture-client', json={'booking_type': 'estimated', 'delivery_time': None})
    assert response.status_code == 200, response.text
    assert response.json()['delivery_time'] is None
    assert db.clients.find_one_and_update.call_args.args[1]['$set']['delivery_slot_key'] is None


def test_conflicting_reschedule_keeps_existing_booking(api):
    client, db, _ = api
    db.clients.find_one.return_value = customer(booking_type='confirmed', delivery_time='09:30')
    db.clients.find_one_and_update.side_effect = DuplicateKeyError('slot already reserved')
    assert client.patch('/api/clients/fixture-client', json={'delivery_time': '10:00'}).status_code == 409


def test_simultaneous_edits_to_one_booking_use_compare_and_set(api):
    client, db, _ = api
    existing = customer(booking_type='confirmed', delivery_time='09:30')
    db.clients.find_one.return_value = existing
    db.clients.find_one_and_update.return_value = None
    response = client.patch('/api/clients/fixture-client', json={'delivery_time': '10:00'})
    assert response.status_code == 409
    assert 'changed while you were editing' in response.json()['detail']
    match = db.clients.find_one_and_update.call_args.args[0]
    assert all(match[field] == existing.get(field) for field in ('delivery_date', 'delivery_time', 'booking_type', 'site_location'))


def test_cross_site_booking_update_cannot_bypass_access(api):
    client, db, _ = api
    db.clients.find_one.return_value = customer(site_location='BYD Nunawading')
    assert client.patch('/api/clients/fixture-client', json={'delivery_time': '10:00'}).status_code == 404
    db.clients.find_one_and_update.assert_not_awaited()


def test_slots_respect_site_aliases_and_exclude_own_booking(api):
    client, db, _ = api
    db.clients.find.return_value = cursor([
        customer(site_location='Caroline Springs', delivery_time='09:30'),
        customer(id='other', site_location='BYD Nunawading', delivery_time='10:00'),
    ])
    result = client.get(f'/api/clients/available-slots?day={DAY}').json()
    assert len(result['slots']) == len(SLOT_TIMES) == 20
    slots = {row['time']: row['available'] for row in result['slots']}
    assert slots['09:30'] is False
    assert slots['10:00'] is True
    result = client.get(f'/api/clients/available-slots?day={DAY}&exclude_client_id=fixture-client').json()
    assert all(slot['available'] for slot in result['slots'])


def test_slot_validation_and_key_identity():
    assert booking_key(customer(booking_type='confirmed', delivery_time='09:30')) == booking_key(
        customer(booking_type='confirmed', delivery_time='09:30', site_location='Caroline Springs'))
    assert booking_key(customer()) is None
    with pytest.raises(HTTPException):
        booking_key(customer(booking_type='confirmed', delivery_time='09:30', site_location='All Sites'))


def test_summary_is_bounded_and_excludes_heavy_fields(api):
    client, db, _ = api
    db.clients.find.return_value = cursor([customer(notes='private large notes', documents=[{'body': 'x' * 10000}])])
    response = client.get(f'/api/clients?summary=true&date_from={DAY}&date_to={DAY}')
    assert response.status_code == 200, response.text
    record = response.json()[0]
    assert record['booking_type'] == 'estimated'
    assert 'documents' not in record and 'notes' not in record and 'comments' not in record
    query, projection = db.clients.find.call_args.args
    assert query['delivery_date'] == {'$gte': DAY, '$lte': DAY}
    assert 'documents' not in projection
    assert len(response.content) < 2000
    assert client.get('/api/clients?date_from=2026-11-21&date_to=2026-11-20').status_code == 422


def test_delivery_receipt_is_not_saved_as_customer_reply(api):
    client, db, _ = api
    response = client.post('/api/messages/inbound', json={
        'sender': '+61408622037', 'to': PHONE, 'message': 'Original outbound message',
        'message_id': 'fixture-outbound-id', 'status': 'delivered', 'received_at': '2026-11-11 10:00:00',
    })
    assert response.status_code == 200, response.text
    assert response.json()['event'] == 'status'
    db.messages.update_many.assert_awaited_once()
    db.messages.insert_one.assert_not_awaited()
    db.messages.update_one.assert_not_awaited()
    db.clients.update_one.assert_not_awaited()


def test_forwarded_customer_reply_saved_without_lead_centre_and_deduplicated(api):
    client, db, _ = api
    db.clients.find.return_value = cursor([customer()])
    db.messages.find_one.return_value = {'site': SITE, 'client_id': 'fixture-client'}
    event = {'sender': '0499 918 523', 'to': '+61408622037', 'message': 'HARMONY OK',
             'type': 'inbound', 'original_message_id': 'fixture-outbound-id', 'received_at': '2026-11-11 10:01:00'}
    first = client.post('/api/messages/inbound', json=event)
    second = client.post('/api/messages/inbound', json=event)
    assert first.status_code == second.status_code == 200
    assert first.json()['recorded_id'] == second.json()['recorded_id']
    queries = [call.args[0] for call in db.messages.update_one.call_args_list]
    assert queries[0] == queries[1]
    saved = db.messages.update_one.call_args.args[1]['$setOnInsert']
    assert saved['body'] == 'HARMONY OK' and saved['phone'] == PHONE and saved['site'] == SITE
    assert saved['client_id'] == 'fixture-client'
    assert saved['direction'] == 'inbound'


@pytest.mark.parametrize('payload', [[], {}, {'type': 'inbound'}, {'status': 'delivered'}])
def test_malformed_webhook_does_not_acknowledge_a_lost_message(api, payload):
    client, _, _ = api
    assert client.post('/api/messages/inbound', json=payload).status_code == 400


def test_worker_reads_forwarded_reply_without_lead_centre(api):
    client, db, user = api
    db.clients.find.return_value = cursor([customer()])
    db.messages.find.return_value = cursor([{'id': 'inbound-test', 'site': SITE, 'phone': PHONE,
        'body': 'HARMONY OK', 'direction': 'inbound', 'status': 'received', 'sent_at': '2026-11-11T10:01:00Z'}])
    response = client.get('/api/messages')
    assert response.status_code == 200, response.text
    assert response.json()[0]['body'] == 'HARMONY OK'
    user.request_site = 'BYD Nunawading'
    assert client.get('/api/messages').json() == []


def test_specialist_has_manager_permissions_but_consultant_does_not(api):
    client, db, user = api
    assert client.get('/api/admin/users').status_code == 403
    user.role = 'delivery_specialist'
    assert asyncio.run(require_admin(user)) == user
    assert client.get('/api/admin/users').status_code == 200
    # Preserve the existing platform-owner restriction on account mutations.
    assert client.post('/api/admin/users', json={'name': 'Fixture', 'email': 'other@example.com'}).status_code == 403


def test_sms_uses_explicitly_requested_sender(monkeypatch):
    import mobilemessage
    from unittest.mock import patch
    response = SimpleNamespace(status_code=200, json=lambda: {'results': [{'status': 'success', 'message_id': 'fixture-id'}]})
    with patch.dict(mobilemessage.os.environ, {'MOBILEMESSAGE_USERNAME': 'fixture', 'MOBILEMESSAGE_PASSWORD': 'fixture',
                    'MOBILEMESSAGE_SENDER_ID': '+61408622037'}, clear=True), patch.object(mobilemessage.requests, 'post', return_value=response) as post:
        assert mobilemessage._send_sync(PHONE, 'Harmony test')['success']
    assert post.call_args.kwargs['json']['messages'][0]['sender'] == '+61408622037'


def test_specialists_receive_site_scoped_manager_notifications(api):
    from services.push_service import broadcast_role_push
    _, db, _ = api
    asyncio.run(broadcast_role_push(db, 'admin', 'Fixture alert', 'Fixture body', site=SITE))
    query = db.users.find.call_args.args[0]
    assert 'delivery_specialist' in query['role']['$in']
    assert '$or' in query and query['active'] is True
