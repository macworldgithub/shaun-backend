import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from auth import get_current_user
from models import User
from contractor_models import OfflineActionItem
from routers.contractor import router
from routers.contractor_users import router as users_router
from services.contractor_integration import authenticate_webhook, receive_event, drain_outbox, build_snapshot, save_config, integration_config
from services.contractor_service import process_offline_batch_sync, clock_out_contractor
from services.contractor_reporting import analytics, report_query
from tests.contractor_fixtures import Cursor, mock_database, contractor_environment


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture
def api():
    db = mock_database()
    app = FastAPI()
    app.include_router(router)
    app.include_router(users_router)
    actor = User(id='worker-a', email='worker@example.com', name='Worker', role='contractor')
    app.dependency_overrides[get_current_user] = lambda: actor
    db.contractor_jobs.find_one.return_value = {'id': 'job-b', 'model_name': 'Seal', 'full_vin': 'VIN123456', 'assigned_contractor_id': 'worker-b', 'site_location': 'Fairfield'}
    with patch('routers.contractor.get_db', return_value=db), patch('routers.contractor_users.get_db', return_value=db):
        yield TestClient(app), db, actor, app


@pytest.mark.parametrize('method,path,body', [
    ('get', '', None), ('get', '/evidence', None), ('get', '/evidence/photo/download', None),
    ('get', '/location-history', None), ('get', '/activity', None), ('get', '/time-logs', None),
    ('post', '/clock-in', {}), ('post', '/clock-out', {}),
    ('post', '/location', {'latitude': -37.8, 'longitude': 145}),
    ('post', '/comments', {'message': 'Unauthorized'}),
    ('post', '/flag-issue', {'description': 'Unauthorized'}),
    ('patch', '/status', {'status': 'completed'}),
])
def test_every_job_endpoint_denies_other_worker(api, method, path, body):
    client, db, _, _ = api
    response = client.request(method, '/api/contractor/jobs/job-b' + path, json=body)
    assert response.status_code == 403
    db.contractor_jobs.update_one.assert_not_awaited()
    db.contractor_time_logs.insert_one.assert_not_awaited()


def test_worker_cannot_create_or_assign_jobs(api):
    client, _, _, _ = api
    assert client.post('/api/contractor/jobs', json={'model_name': 'Seal', 'full_vin': 'VIN123456'}).status_code == 403
    assert client.post('/api/contractor/jobs/from-template', json={'template_id': 't', 'model_name': 'Seal', 'full_vin': 'VIN123456'}).status_code == 403


def test_cross_site_admin_cannot_read_job(api):
    client, _, actor, _ = api
    actor.role = 'admin'
    actor.locked_site = 'Another Centre'
    assert client.get('/api/contractor/jobs/job-b').status_code == 404


def test_admin_manages_contractors_without_granting_admin(api):
    client, db, actor, _ = api
    actor.role = 'admin'
    db.users.find_one.return_value = None
    response = client.post('/api/contractor/users', json={'email': 'new@example.com', 'name': 'New Worker', 'role': 'contractor'})
    assert response.status_code == 200
    assert response.json()['user']['role'] == 'contractor'
    assert db.audit.insert_one.await_count == 1
    assert client.post('/api/contractor/users', json={'email': 'boss@example.com', 'name': 'Boss', 'role': 'admin'}).status_code == 403


def test_super_admin_can_grant_admin_and_shared_owner_controls_remain_separate(api):
    client, db, actor, _ = api
    actor.role = 'super_admin'
    db.users.find_one.return_value = None
    assert client.post('/api/contractor/users', json={'email': 'boss@example.com', 'name': 'Boss', 'role': 'admin'}).status_code == 200
    db.users.find_one.return_value = {'id': 'shared', 'email': 'shared@example.com', 'role': 'contractor', 'platform_access': ['delivery_centre', 'sales_crm']}
    assert client.patch('/api/contractor/users/shared', json={'active': False}).status_code == 409


@pytest.mark.anyio
async def test_offline_actions_cannot_bypass_assignment():
    db = mock_database()
    db.contractor_jobs.find_one.return_value = {'id': 'job-b', 'assigned_contractor_id': 'worker-b'}
    user = User(id='worker-a', email='a@example.com', name='A', role='contractor')
    result = await process_offline_batch_sync(db, user, [OfflineActionItem(client_action_id='a1', action_type='clock_in', job_id='job-b')])
    assert result.failed == 1
    db.contractor_time_logs.insert_one.assert_not_awaited()


@pytest.mark.anyio
async def test_completed_offline_action_is_not_applied_twice():
    db = mock_database()
    db.contractor_jobs.find_one.return_value = {'id': 'job-a', 'assigned_contractor_id': 'worker-a'}
    db.contractor_offline_actions.find_one.return_value = {'status': 'success'}
    user = User(id='worker-a', email='a@example.com', name='A', role='contractor')
    result = await process_offline_batch_sync(db, user, [OfflineActionItem(client_action_id='a1', action_type='add_comment', job_id='job-a', payload={'message': 'Once'})])
    assert result.succeeded == 1
    db.contractor_activities.insert_one.assert_not_awaited()
    db.contractor_activities.update_one.assert_not_awaited()


@pytest.mark.anyio
async def test_clock_out_retry_uses_atomic_job_accounting_marker():
    db = mock_database()
    now = datetime.now(timezone.utc)
    closed = {'id': 'time-1', 'job_id': 'job-a', 'contractor_id': 'worker-a', 'contractor_name': 'A',
        'task_id': 'task-a', 'clock_in': now - timedelta(minutes=5), 'clock_out': now, 'duration_seconds': 300}
    db.contractor_time_logs.find_one.return_value = closed
    db.contractor_jobs.find_one.return_value = None
    result = await clock_out_contractor(db, 'job-a', 'worker-a', 'A', client_action_id='out-1')
    assert result.duration_seconds == 300
    query, update = db.contractor_jobs.update_one.call_args.args
    assert query['applied_time_entries'] == {'$ne': 'time-1'}
    assert update['$addToSet']['applied_time_entries'] == 'time-1'


def request_for(payload, headers=None):
    request = MagicMock()
    body = json.dumps(payload).encode()
    request.headers = headers or {}
    request.body = AsyncMock(return_value=body)
    request.json = AsyncMock(return_value=payload)
    return request


@pytest.mark.anyio
async def test_webhook_requires_auth_and_rejects_stale_or_tampered_signatures():
    db = mock_database()
    request = request_for({'event': 'job.updated'})
    with pytest.raises(HTTPException) as error:
        await authenticate_webhook(request, db)
    assert error.value.status_code == 401
    stamp = str(int(time.time()))
    signature = hmac.new(b'test-webhook-secret', stamp.encode() + b'.' + await request.body(), hashlib.sha256).hexdigest()
    request.headers = {'x-contractor-timestamp': stamp, 'x-contractor-signature': signature}
    await authenticate_webhook(request, db)
    request.body.return_value = b'tampered'
    with pytest.raises(HTTPException): await authenticate_webhook(request, db)
    request.headers['x-contractor-timestamp'] = '1'
    with pytest.raises(HTTPException): await authenticate_webhook(request, db)


@pytest.mark.anyio
async def test_webhook_replay_returns_success_without_repeating_mutations():
    db = mock_database()
    db.contractor_webhook_receipts.insert_one.side_effect = DuplicateKeyError('duplicate')
    db.contractor_webhook_receipts.find_one.return_value = {'status': 'success'}
    result = await receive_event(db, request_for({'event_id': 'evt-1', 'event': 'job.updated'}, {'authorization': 'Bearer test-webhook-secret'}))
    assert result['duplicate'] is True
    db.contractor_jobs.update_one.assert_not_awaited()


@pytest.mark.anyio
async def test_new_webhook_job_preserves_complete_card_details():
    db = mock_database()
    result = await receive_event(db, request_for({'event_id': 'new-job', 'event': 'job.request', 'payload': {
        'model_name': 'Seal', 'full_vin': 'FULLVIN123456', 'delivery_date_time': '2026-12-01T10:30:00+11:00',
        'due_date': '2026-11-30', 'description': 'Window tint', 'checklist': [{'title': 'Tint front windows'}]}},
        {'authorization': 'Bearer test-webhook-secret'}))
    assert result['received'] is True
    card = db.contractor_jobs.update_one.call_args.args[1]['$setOnInsert']
    assert card['last_6_vin'] == '123456'
    assert card['checklist'][0]['title'] == 'Tint front windows'
    assert card['delivery_date_time'].endswith('+11:00')


@pytest.mark.anyio
async def test_snapshot_contains_timestamps_time_tasks_media_and_gps():
    db = mock_database()
    db.contractor_time_logs.find.return_value = Cursor([{'id': 'clock', 'task_id': 'task', 'clock_in': '2026-10-01T10:00:00Z', 'clock_out': '2026-10-01T11:00:00Z'}])
    db.contractor_locations.find.return_value = Cursor([{'id': 'gps', 'latitude': -37, 'longitude': 145}])
    db.contractor_evidence.find.return_value = Cursor([{'id': 'photo', 'storage_path': 'private', 'stage': 'after'}])
    snapshot = await build_snapshot(db, {'id': 'job-a', 'status': 'invoiced', 'completed_at': '2026-10-01T11:00:00Z', 'total_time_seconds': 3600, 'checklist': [{'completed': True}]})
    assert snapshot['total_hours'] == 1
    assert snapshot['location_history'][0]['latitude'] == -37
    assert snapshot['time_logs'][0]['task_id'] == 'task'
    assert 'storage_path' not in snapshot['evidence'][0]
    assert snapshot['evidence'][0]['download_path'].endswith('/photo/download')


@pytest.mark.anyio
async def test_outbound_failures_remain_durable_and_retry_with_same_event_id():
    db = mock_database()
    item = {'id': 'stable-event', 'job_id': 'job-a', 'event': 'job.snapshot', 'attempts': 1, 'payload': {}}
    db.contractor_outbox.find_one_and_update.side_effect = [item, None]
    db.contractor_jobs.find_one.return_value = {'id': 'job-a', 'client_id': 'client-a'}
    config = {'outbound_webhook_url': 'https://delivery.example/webhook', 'api_key': 'api-key', 'webhook_secret': 'secret'}
    sent = []
    def fail(request):
        sent.append(request)
        return httpx.Response(503)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    with patch('services.contractor_integration.httpx.AsyncClient', return_value=client):
        await drain_outbox(db, config)
    assert db.contractor_outbox.update_one.call_args.args[1]['$set']['status'] == 'retry'
    assert sent[0].headers['idempotency-key'] == 'stable-event'
    db.contractor_outbox.find_one_and_update.side_effect = [item, None]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    with patch('services.contractor_integration.httpx.AsyncClient', return_value=client):
        await drain_outbox(db, config)
    assert db.contractor_outbox.update_one.call_args.args[1]['$set']['status'] == 'delivered'


@pytest.mark.anyio
async def test_media_is_encrypted_at_rest_and_legacy_reads_still_work(tmp_path, monkeypatch):
    from services import storage
    monkeypatch.setenv('DOCUMENT_STORAGE_BACKEND', 'local')
    monkeypatch.setattr(storage, 'LOCAL_ROOT', tmp_path)
    key = 'contractor-jobs/job-a/photo.jpg'
    await storage.put_bytes(key, b'private-image', 'image/jpeg')
    on_disk = (tmp_path / key).read_bytes()
    assert on_disk.startswith(storage.ENCRYPTED_HEADER)
    assert b'private-image' not in on_disk
    assert await storage.get_bytes(key) == b'private-image'
    legacy = tmp_path / 'legacy.jpg'
    legacy.write_bytes(b'old-image')
    assert await storage.get_bytes('legacy.jpg') == b'old-image'


@pytest.mark.anyio
async def test_credentials_are_encrypted_masked_and_blank_preserves_existing():
    db = mock_database()
    actor = User(email='super@example.com', name='Super', role='super_admin')
    await save_config(db, {'api_key': 'private-key', 'polling_interval_minutes': 15, 'auto_poll_enabled': False}, actor)
    stored = db.settings.update_one.call_args.args[1]['$set']
    assert stored['api_key_encrypted'] != 'private-key'
    db.settings.find_one.return_value = stored
    public = await integration_config(db, public=True)
    assert public['api_key'] == '' and public['api_key_configured']
    await save_config(db, {'api_key': '', 'polling_interval_minutes': 5}, actor)
    db.settings.find_one.return_value = db.settings.update_one.call_args.args[1]['$set']
    assert (await integration_config(db))['api_key'] == 'private-key'
    with pytest.raises(HTTPException): await save_config(db, {'api_url': 'http://insecure.example'}, actor)
    with pytest.raises(HTTPException): await save_config(db, {'polling_interval_minutes': 1}, actor)


@pytest.mark.anyio
async def test_reports_include_invoiced_completions_and_period_breakdowns():
    db = mock_database()
    db.contractor_jobs.find.return_value = Cursor([{'id': 'j', 'full_vin': 'VIN123456', 'model_name': 'Seal', 'status': 'invoiced',
        'assigned_contractor_name': 'A', 'created_at': '2026-10-01T00:00:00Z', 'completed_at': '2026-10-02T00:00:00Z',
        'total_time_seconds': 600, 'checklist': [{'title': 'Tint', 'duration_seconds': 600}]}])
    result = await analytics(db, contractor_id='a', model='Seal', status='invoiced', site='Fairfield', date_from='2026-10-01', date_to='2026-10-31')
    assert result['completion_rate'] == 100
    assert result['avg_time_per_job_seconds'] == 600
    assert result['jobs_per_week'][0]['jobs'] == 1
    assert result['completion_trends'][0]['completed'] == 1
    query = db.contractor_jobs.find.call_args.args[0]
    assert 'Fairfield' in query['site_location']['$regex'] and query['assigned_contractor_id'] == 'a'
    assert '$convert' in str(query['$expr'])  # Both legacy string and BSON dates are supported.
    with pytest.raises(HTTPException): report_query(date_from='invalid')


@pytest.mark.anyio
async def test_remote_sync_queues_when_the_delivery_client_is_in_another_database():
    from services.contractor_service import sync_job_to_delivery_client
    db = mock_database()
    db.settings.find_one.return_value = {'outbound_webhook_url': 'https://delivery.example/webhook'}
    db.contractor_jobs.find_one.return_value = {'id': 'job-a', 'client_id': 'remote-client', 'full_vin': 'VIN123456', 'status': 'invoiced'}
    result = await sync_job_to_delivery_client(db, 'job-a')
    assert result.status == 'pending'
    queued = db.contractor_outbox.insert_one.call_args.args[0]
    assert queued['status'] == 'pending'
    assert queued['payload']['status'] == 'invoiced'


def test_site_alias_filter_preserves_the_byd_denza_boundary():
    import re
    from services.contractor_access import site_filter
    pattern = site_filter('BYD Fairfield')['$regex']
    assert re.fullmatch(pattern, 'Fairfield', re.I)
    assert re.fullmatch(pattern, 'BYD Fairfield', re.I)
    melbourne = site_filter('BYD Melbourne City')['$regex']
    assert re.fullmatch(melbourne, 'BYD Melbourne', re.I)
    assert not re.fullmatch(melbourne, 'Denza Melbourne', re.I)
