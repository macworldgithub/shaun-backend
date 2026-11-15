import asyncio
import copy
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth import get_current_user
from models import User
from routers import clients, post_delivery as routes
from services import post_delivery as service

NOW = datetime(2026, 9, 25, 1, tzinfo=timezone.utc)
MISSING = object()


def matches(row, query):
    for key, expected in query.items():
        if key == '$or':
            if not any(matches(row, part) for part in expected): return False
            continue
        actual = row.get(key, MISSING)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == '$exists' and (actual is not MISSING) != value: return False
                if op == '$in' and actual not in value: return False
                if op == '$ne' and actual == value: return False
                if op == '$lte' and (actual is MISSING or actual > value): return False
                if op == '$lt' and (actual is MISSING or actual >= value): return False
                if op == '$regex' and not re.search(value, str(actual), re.I if expected.get('$options') == 'i' else 0): return False
        elif actual != expected and not (actual is MISSING and expected is None): return False
    return True


class Cursor:
    def __init__(self, rows): self.rows = rows
    def __aiter__(self):
        async def generate():
            for row in self.rows: yield copy.deepcopy(row)
        return generate()
    def sort(self, key, direction=1): self.rows.sort(key=lambda row: row.get(key, 0), reverse=direction < 0); return self
    def limit(self, size): self.rows = self.rows[:size]; return self
    async def to_list(self, size): return copy.deepcopy(self.rows[:size] if size else self.rows)


class Collection:
    def __init__(self, rows=()): self.rows = copy.deepcopy(list(rows))
    async def find_one(self, query, *args): return copy.deepcopy(next((row for row in self.rows if matches(row, query)), None))
    def find(self, query=None, *args): return Cursor([row for row in self.rows if matches(row, query or {})])
    async def insert_one(self, row): self.rows.append(copy.deepcopy(row))
    def apply(self, row, update, inserted=False):
        if inserted: row.update(copy.deepcopy(update.get('$setOnInsert', {})))
        row.update(copy.deepcopy(update.get('$set', {})))
        for key in update.get('$unset', {}): row.pop(key, None)
        for key, value in update.get('$inc', {}).items(): row[key] = row.get(key, 0) + value
    async def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows if matches(row, query)), None)
        inserted = row is None
        if inserted and upsert:
            row = copy.deepcopy(query); self.rows.append(row)
        if row is not None: self.apply(row, update, inserted)
        return SimpleNamespace(matched_count=int(row is not None), modified_count=int(row is not None))
    async def find_one_and_update(self, query, update, **kwargs):
        row = next((row for row in self.rows if matches(row, query)), None)
        if row is None: return None
        self.apply(row, update)
        return copy.deepcopy(row)


def database(client=None):
    record = {'id': 'client-1', 'name': 'Jamie Customer', 'phone': '+61400000000', 'vehicle': 'BYD Seal',
              'site_location': 'BYD Fairfield', 'stage': 'Delivered', 'sale_type': 'Retail', 'customer_type': 'Private',
              'salesperson': 'Alex Sales', 'crm_customer_id': 'customer-1', 'delivered_at': NOW, 'post_delivery_enrollment_id': 'enrollment-1', **(client or {})}
    db = SimpleNamespace(**{key: Collection() for key in ('clients', 'customers', 'users', 'post_delivery_jobs', 'post_delivery_settings', 'post_delivery_followups', 'timelineevents', 'messages')})
    db.clients.rows = [record]
    db.customers.rows = [{'customer_id': 'customer-1', 'owner_user_id': 'sales-user'}]
    return db


def test_delivery_transition_is_once_only_and_does_not_backfill_old_deliveries():
    first = service.delivery_transition({'stage': 'Ready for Pickup'}, {'stage': 'Delivered'}, NOW)
    assert first['delivered_at'] == NOW and first['post_delivery_enrollment_id']
    assert not service.delivery_transition({'stage': 'Delivered'}, {'notes': 'Edited'}, NOW)
    assert not service.delivery_transition({'stage': 'Scheduled', **first}, {'stage': 'Delivered'}, NOW)


@pytest.mark.parametrize('changes', [{'sale_type': 'Fleet'}, {'sale_type': 'Demo'}, {'deal_type': 'Fleet order'}, {'customer_type': 'Business'}, {'registered_operator_type': 'Company'}, {'customer_type': None, 'fleet_company': 'Company'}])
def test_private_customer_exclusions(changes):
    assert not service.private_customer({'sale_type': 'Retail', 'customer_type': 'Private', **changes})


def test_private_novated_lease_is_not_mistaken_for_a_fleet_sale():
    assert service.private_customer({'sale_type': 'Novated lease', 'customer_type': 'Private', 'registered_operator_type': 'Individual', 'fleet_company': 'Salary packaging provider'})


def test_templates_use_vehicle_fields_and_fill_every_placeholder():
    text = service.render(service.DEFAULT_TEMPLATES['delivery_day'], {'name': 'Jamie Customer', 'vehicle_make': 'Denza', 'vehicle_model': 'B5', 'vehicle': 'Fallback'})
    assert 'Hi Jamie' in text and 'Denza B5' in text and '[' not in text
    assert 'BYD Seal' in service.render('[Make] [Model]', {'vehicle': 'BYD Seal'})


def test_full_sequence_sends_three_texts_and_one_assigned_crm_reminder(monkeypatch):
    async def run():
        db = database(); send = AsyncMock(return_value={'success': True, 'status': 'queued', 'message_id': 'provider-id'})
        monkeypatch.setattr(service, 'send_sms', send)
        await service.ensure_jobs(db, db.clients.rows[0])
        await service.ensure_jobs(db, db.clients.rows[0])
        assert len(db.post_delivery_jobs.rows) == 4
        for day, texts in [(0, 1), (3, 1), (23, 2), (31, 3)]:
            # Calendar-day due times also account for the intervening DST change.
            now = next(row['due_at'] for row in db.post_delivery_jobs.rows if row['day'] == day)
            await service.run_due(db, now)
            await service.run_due(db, now)
            assert send.await_count == texts
        reminder = db.post_delivery_followups.rows[0]
        assert reminder['salesperson_id'] == 'sales-user' and reminder['customer_id'] == 'customer-1'
        assert len(db.post_delivery_followups.rows) == 1 and len(db.messages.rows) == 3
        assert db.post_delivery_jobs.rows[2]['due_at'] - NOW == timedelta(days=23, hours=-1)
    asyncio.run(run())


def test_concurrent_workers_claim_once_and_timeout_retry_keeps_key_and_payload(monkeypatch):
    async def run():
        db = database(); await service.ensure_jobs(db, db.clients.rows[0]); identity = db.post_delivery_jobs.rows[0]['_id']
        started, release = asyncio.Event(), asyncio.Event()
        async def timeout(*args, **kwargs): started.set(); await release.wait(); return {'success': False, 'status': 'error', 'retryable': True}
        send = AsyncMock(side_effect=timeout)
        first = asyncio.create_task(service.process_job(db, identity, NOW, send))
        await started.wait(); await service.process_job(db, identity, NOW, send); release.set(); await first
        assert send.await_count == 1
        original = send.call_args
        db.post_delivery_settings.rows = [{'_id': 'fairfield', 'templates': {'delivery_day': 'Changed text'}}]
        send.side_effect = None; send.return_value = {'success': True, 'status': 'queued', 'message_id': 'same-provider-id'}
        await service.process_job(db, identity, NOW + timedelta(minutes=3), send)
        assert send.call_args == original
        assert db.post_delivery_jobs.rows[0]['status'] == 'sent'
    asyncio.run(run())


@pytest.mark.parametrize('has_owner', [True, False])
def test_reminder_can_be_assigned_to_a_sales_floor_only_user(has_owner):
    async def run():
        db = database()
        db.users.rows = [{'_id': 'floor-user', 'name': 'Alex Sales', 'active': True,
                          'role': 'sales_consultant', 'platform_access': ['sales_floor'], 'site': 'BYD Fairfield'}]
        if has_owner:
            db.customers.rows[0]['owner_user_id'] = 'floor-user'
            # The canonical owner wins even if CRM has a separate account with the same name.
            db.users.rows.append({**db.users.rows[0], '_id': 'crm-user', 'platform_access': ['crm']})
        else:
            db.customers.rows[0].pop('owner_user_id')
        await service.ensure_jobs(db, db.clients.rows[0])
        job = next(row for row in db.post_delivery_jobs.rows if row['step'] == 'sales_followup')
        await service.process_job(db, job['_id'], job['due_at'], AsyncMock())
        assert db.post_delivery_followups.rows[0]['salesperson_id'] == 'floor-user'
    asyncio.run(run())


def test_changed_recipient_is_held_for_review_instead_of_sending_to_the_old_number():
    async def run():
        db = database(); await service.ensure_jobs(db, db.clients.rows[0])
        job = db.post_delivery_jobs.rows[0]; job['phone'] = '+61411111111'
        send = AsyncMock(); await service.process_job(db, job['_id'], NOW, send)
        send.assert_not_awaited(); assert db.post_delivery_jobs.rows[0]['status'] == 'review'
    asyncio.run(run())


@pytest.mark.parametrize('change,reason', [({'sms_opt_out': True}, 'opted out'), ({'stage': 'Scheduled'}, 'reversed'), ({'sale_type': 'Fleet'}, 'Private')])
def test_pending_jobs_recheck_current_eligibility_and_never_send(monkeypatch, change, reason):
    async def run():
        db = database(); await service.ensure_jobs(db, db.clients.rows[0]); db.clients.rows[0].update(change)
        send = AsyncMock(); await service.process_job(db, db.post_delivery_jobs.rows[0]['_id'], NOW, send)
        send.assert_not_awaited(); assert reason in db.post_delivery_jobs.rows[0]['reason']
    asyncio.run(run())


def test_site_pause_and_expired_delivery_window_do_not_send():
    async def run():
        db = database(); await service.ensure_jobs(db, db.clients.rows[0]); send = AsyncMock()
        db.post_delivery_settings.rows = [{'_id': 'fairfield', 'enabled': False}]
        await service.process_job(db, db.post_delivery_jobs.rows[0]['_id'], NOW, send)
        assert db.post_delivery_jobs.rows[0]['status'] == 'retry' and db.post_delivery_jobs.rows[0]['attempts'] == 0
        db.post_delivery_settings.rows[0]['enabled'] = True
        await service.process_job(db, db.post_delivery_jobs.rows[0]['_id'], NOW + timedelta(days=2), send)
        send.assert_not_awaited(); assert db.post_delivery_jobs.rows[0]['status'] == 'skipped'
    asyncio.run(run())


def test_delivery_api_stamps_enrollment_once_and_settings_are_site_scoped(monkeypatch):
    db = database({'stage': 'Ready for Pickup'})
    for key in ('post_delivery_enrollment_id', 'delivered_at'): db.clients.rows[0].pop(key)
    monkeypatch.setattr(clients, 'get_db', lambda: db); monkeypatch.setattr(routes, 'get_db', lambda: db)
    monkeypatch.setattr(clients, 'dispatch_crm_webhook', lambda **kwargs: None)
    monkeypatch.setattr(clients, 'sync_client_updates_to_contractor_jobs', AsyncMock())
    send = AsyncMock(return_value={'success': True, 'status': 'queued', 'message_id': 'fixture-provider'})
    monkeypatch.setattr(service, 'send_sms', send)
    user = User(id='delivery', name='Delivery Staff', email='delivery@example.com', role='delivery_consultant', locked_site='BYD Fairfield')
    app = FastAPI(); app.include_router(clients.router); app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    api = TestClient(app)
    first = api.patch('/api/clients/client-1', json={'stage': 'Delivered'})
    assert first.status_code == 200, first.text
    assert first.json()['delivered_at']
    assert api.patch('/api/clients/client-1', json={'stage': 'Delivered'}).json()['delivered_at'] == first.json()['delivered_at']
    assert len(db.post_delivery_jobs.rows) == 4 and send.await_count == 1
    assert api.get('/api/post-delivery/clients/client-1').json()['enrolled']
    db.clients.rows[0]['site_location'] = 'BYD Nunawading'
    assert api.get('/api/post-delivery/clients/client-1').status_code == 404
    assert api.put('/api/post-delivery/settings', json={'enabled': True, 'templates': service.DEFAULT_TEMPLATES}).status_code == 403
    user.role = 'admin'
    assert api.put('/api/post-delivery/settings', json={'enabled': True, 'templates': service.DEFAULT_TEMPLATES}).status_code == 200
    assert db.post_delivery_settings.rows[0]['_id'] == 'fairfield'
    user.locked_site = None; user.all_sites = True
    assert api.put('/api/post-delivery/settings', headers={'X-Site': 'BYD Nunawading'}, json={'enabled': False, 'templates': service.DEFAULT_TEMPLATES}).status_code == 200
    assert db.post_delivery_settings.rows[1]['_id'] == 'nunawading'
