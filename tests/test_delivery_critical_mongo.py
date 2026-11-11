"""Opt-in integration tests in a newly created, disposable database only.

RUN_DELIVERY_MONGO_TESTS=1 python -m pytest tests/test_delivery_critical_mongo.py -q -s
Uses the configured Mongo server, never the application's collections.
"""
import asyncio
import json
import os
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure
from models import ClientBase, ClientUpdate, User
from routers import clients
from services.delivery_booking import ensure_booking_index, booking_key
from services.delivery_dashboard import dashboard

pytestmark = pytest.mark.skipif(os.environ.get('RUN_DELIVERY_MONGO_TESTS') != '1', reason='Explicitly enable disposable MongoDB integration tests')


def test_atomic_reservations_dashboard_and_summary_on_real_mongodb(monkeypatch):
    async def verify():
        connection = AsyncIOMotorClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=10000)
        name = 'dct_' + uuid.uuid4().hex
        database = connection[name]
        monkeypatch.setattr(clients, 'get_db', lambda: database)
        monkeypatch.setattr(clients, 'sync_client_updates_to_contractor_jobs', AsyncMock())
        user = User(id='integration-worker', email='fixture@example.com', name='Integration Worker',
                    role='delivery_consultant', locked_site='BYD Caroline Springs', active_site='BYD Caroline Springs')
        payload = dict(name='Disposable Booking Fixture', phone='+61499918523', vehicle='BYD Seal',
                       booking_type='confirmed', delivery_date='2026-11-20', delivery_time='09:30',
                       site_location='BYD Caroline Springs')
        try:
            await ensure_booking_index(database)
            # Both requests race against the actual unique partial index.
            results = await asyncio.gather(
                clients.create_client(ClientBase(**payload), user),
                clients.create_client(ClientBase(**payload), user), return_exceptions=True)
            successes = [result for result in results if not isinstance(result, Exception)]
            errors = [result for result in results if isinstance(result, HTTPException)]
            assert len(successes) == len(errors) == 1 and errors[0].status_code == 409
            winner = successes[0]
            assert await database.clients.count_documents({}) == 1
            assert not next(slot for slot in (await clients.available_slots('2026-11-20', user=user))['slots'] if slot['time'] == '09:30')['available']
            await clients.update_client(winner.id, ClientUpdate(delivery_time='10:00'), user)
            edits = await asyncio.gather(
                clients.update_client(winner.id, ClientUpdate(delivery_date='2026-11-21'), user),
                clients.update_client(winner.id, ClientUpdate(delivery_time='11:00'), user), return_exceptions=True)
            assert sum(not isinstance(result, Exception) for result in edits) >= 1
            assert all(not isinstance(result, Exception) or isinstance(result, HTTPException) and result.status_code == 409 for result in edits)
            saved = await database.clients.find_one({'id': winner.id})
            assert saved['delivery_slot_key'] == booking_key(saved)
            await clients.update_client(winner.id, ClientUpdate(delivery_date='2026-11-20', delivery_time='10:00'), user)
            # Moving a booking releases the old time atomically.
            second = await clients.create_client(ClientBase(**payload), user)
            await clients.update_client(second.id, ClientUpdate(booking_type='estimated', delivery_time=None), user)
            third = await clients.create_client(ClientBase(**payload), user)
            assert third.delivery_time == '09:30'
            await database.clients.insert_one({**payload, 'id': 'other-site', 'site_location': 'BYD Nunawading'})
            await database.messages.insert_many([
                {'id': 'visible-message', 'phone': '+61499918523', 'body': 'HARMONY OK', 'direction': 'inbound', 'site': 'BYD Caroline Springs', 'sent_at': '2026-11-11T10:00:00Z'},
                {'id': 'hidden-message', 'body': 'Other site', 'direction': 'inbound', 'site': 'BYD Nunawading'},
            ])
            stats = await dashboard(database, user, 'BYD Nunawading')
            assert stats['total_clients'] == 3
            assert stats['by_stage'] == {'Scheduled': 3}
            assert stats['sms_total'] == 1
            assert stats['recent_messages'][0]['id'] == 'visible-message'
            assert all(row['site_location'] == user.locked_site for row in stats['upcoming'])
            # Representative documents demonstrate the payload reduction without
            # loading customer data from the application's real database.
            await database.clients.update_many({}, {'$set': {'notes': 'fixture ' * 2000, 'documents': [{'body': 'fixture ' * 2000}]}})
            start = time.perf_counter()
            full = await clients.list_clients(user=user)
            full_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            summary = await clients.list_clients(user=user, summary=True)
            summary_ms = (time.perf_counter() - start) * 1000
            assert len(summary.body) < len(full.body) * 0.2
            assert 'documents' not in json.loads(summary.body)[0]
            request = Request({'type': 'http', 'headers': []})
            page = json.loads((await clients.paged_clients(request, user=user, page=1, limit=2, exception=None)).body)
            assert page['total'] == 3 and page['pages'] == 2 and len(page['items']) == 2
            await database.clients.update_many({}, {'$set': {
                'payment_complete': True, 'pdi_complete': True, 'registration_docs_complete': True,
            }})
            await database.clients.update_one({'id': winner.id}, {'$set': {
                'trade_in_flag': True, 'trade_in_status': 'Valid', 'trade_in_valid_until': '2000-01-01',
            }})
            ready = json.loads((await clients.paged_clients(request, user=user, page=1, limit=20,
                                                            exception='ready_for_delivery')).body)
            assert ready['total'] == ready['ready_count'] == len(ready['items']) == 2
            assert all(row['ready_for_delivery'] for row in ready['items'])
            empty = json.loads((await clients.paged_clients(request, user=user, page=1, limit=20,
                                                            exception=None, search='not-present')).body)
            assert empty['total'] == 0 and empty['items'] == [] and empty['ready_count'] == 2
            print(json.dumps({'race': 'one success / one HTTP 409', 'site_scoped_clients': 3,
                              'full_bytes': len(full.body), 'summary_bytes': len(summary.body),
                              'full_ms': round(full_ms, 1), 'summary_ms': round(summary_ms, 1)}))
        finally:
            # The only removed resource is this test's freshly generated database.
            assert name.startswith('dct_') and len(name.removeprefix('dct_')) == 32
            for collection in ('clients', 'messages'):
                # The application's database account does not grant dropDatabase.
                # Drop only collections created by this test; fall back to removing
                # their fixture records when schema deletion is restricted too.
                try:
                    await database.drop_collection(collection)
                except OperationFailure:
                    await database[collection].delete_many({})
                    assert await database[collection].count_documents({}) == 0
            connection.close()
    asyncio.run(verify())
