import json
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from bson import ObjectId

from services.contractor_integration import build_snapshot, drain_outbox, queue_job_event
from services.contractor_service import sync_client_updates_to_contractor_jobs
from tests.contractor_fixtures import Cursor, mock_database


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('external', [False, True])
async def test_queue_encodes_mongo_job_without_mutating_it(external):
    db = mock_database()
    db.settings.find_one.return_value = {'outbound_webhook_url': 'https://delivery.example/webhook' if external else ''}
    mongo_id, reference = ObjectId(), ObjectId()
    now = datetime.now(timezone.utc)
    job = {'_id': mongo_id, 'id': 'job-a', 'updated_at': now, 'metadata': {'reference': reference}}

    event_id = await queue_job_event(db, 'job.updated', 'job-a', job)

    queued = db.contractor_outbox.insert_one.call_args.args[0]
    assert queued['id'] == event_id
    assert queued['status'] == ('pending' if external else 'local')
    assert json.loads(json.dumps(queued['payload'])) == {
        'id': 'job-a', 'updated_at': now.isoformat(), 'metadata': {'reference': str(reference)}}
    assert job['_id'] == mongo_id
    assert job['metadata']['reference'] == reference


@pytest.mark.anyio
async def test_delivery_update_queues_real_mongo_job_document():
    db = mock_database()
    db.contractor_jobs.find.return_value = Cursor([{'_id': ObjectId(), 'id': 'job-a', 'client_id': 'client-a'}])

    await sync_client_updates_to_contractor_jobs(db, 'client-a', {'delivery_date': '2026-11-05'}, 'Scheduled poll')

    queued = db.contractor_outbox.insert_one.call_args.args[0]
    assert queued['event'] == 'job.updated'
    assert queued['payload']['id'] == 'job-a'
    assert queued['payload']['due_date'] == '2026-11-05'
    assert '_id' not in queued['payload']
    json.dumps(queued['payload'])


@pytest.mark.anyio
async def test_snapshot_encodes_nested_mongo_references_and_hides_storage():
    db = mock_database()
    reference = ObjectId()
    db.contractor_evidence.find.return_value = Cursor([{
        '_id': ObjectId(), 'id': 'photo', 'storage_path': 'private/path', 'metadata': {'reference': reference}}])

    snapshot = await build_snapshot(db, {'_id': ObjectId(), 'id': 'job-a', 'checklist': [{'reference': reference}]})

    assert '_id' not in snapshot
    assert snapshot['checklist'][0]['reference'] == str(reference)
    assert snapshot['evidence'][0]['metadata']['reference'] == str(reference)
    assert 'storage_path' not in snapshot['evidence'][0]
    json.dumps(snapshot)


@pytest.mark.anyio
async def test_outbox_retry_encodes_stored_payload_when_job_was_removed():
    db = mock_database()
    reference = ObjectId()
    item = {'id': 'event-a', 'event': 'job.updated', 'job_id': 'job-a', 'attempts': 1,
            'payload': {'_id': ObjectId(), 'id': 'job-a', 'metadata': {'reference': reference}}}
    db.contractor_outbox.find_one_and_update.side_effect = [item, None]
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    with patch('services.contractor_integration.httpx.AsyncClient', return_value=client):
        await drain_outbox(db, {'outbound_webhook_url': 'https://delivery.example/webhook', 'api_key': '', 'webhook_secret': 'fixture'})

    assert len(sent) == 1
    assert sent[0]['payload']['metadata']['reference'] == str(reference)
    assert '_id' not in sent[0]['payload']
    assert db.contractor_outbox.update_one.call_args.args[1]['$set']['status'] == 'delivered'
