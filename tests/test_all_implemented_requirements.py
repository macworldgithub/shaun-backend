import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from fastapi.testclient import TestClient

from server import app
from contractor_models import (
    Job, JobTask, JobEvidence, OfflineActionItem, OfflineActionType, IntegrationSyncLog
)
from services.contractor_service import (
    clock_in_contractor,
    clock_out_contractor,
    compute_contractor_analytics,
    poll_delivery_centre_integration,
    create_job_from_delivery_client
)
from models import ClientDocument, User

client = TestClient(app)


# ===========================================================================
# 1. Models & Offline Evidence Support
# ===========================================================================

def test_job_task_duration_field_and_computed_property():
    """Requirement: Task duration tracked on checklist items."""
    task = JobTask(title="Window Tint 2 Front", duration_seconds=180)
    assert task.duration_seconds == 180

    job = Job(
        model_name="BYD Seal",
        full_vin="LC0SEAL1234567890",
        checklist=[task]
    )
    assert len(job.tasks) == 1
    assert job.tasks[0]["duration_seconds"] == 180
    assert job.tasks[0]["title"] == "Window Tint 2 Front"


def test_offline_action_type_includes_evidence():
    """Requirement: Offline action schema supports queued evidence uploads."""
    item = OfflineActionItem(
        client_action_id="offline-action-1",
        action_type="upload_evidence",
        job_id="job-999",
        payload={"stage": "after", "fileName": "completed.jpg"}
    )
    assert item.action_type == "upload_evidence"
    assert item.job_id == "job-999"


# ===========================================================================
# 2. Per-Task Time Tracking & Persistence
# ===========================================================================

@pytest.mark.anyio
async def test_clock_out_accumulates_task_duration():
    """Requirement: Stopping a task timer accumulates duration onto the specific task."""
    mock_db = MagicMock()
    mock_db.contractor_time_logs = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_activities = MagicMock()
    mock_db.clients = MagicMock()

    t_in = datetime.now(timezone.utc) - timedelta(seconds=300)
    mock_db.contractor_time_logs.find_one = AsyncMock(return_value={
        'id': 'log-task-1',
        'job_id': 'job-1',
        'task_id': 'task-tint-101',
        'contractor_id': 'c-1',
        'contractor_name': 'Shaun Contractor',
        'clock_in': t_in,
        'clock_out': None,
        'duration_seconds': None
    })
    mock_db.contractor_time_logs.update_one = AsyncMock()
    mock_db.contractor_jobs.update_one = AsyncMock()
    mock_db.contractor_activities.insert_one = AsyncMock()
    mock_db.contractor_jobs.find_one = AsyncMock(return_value={
        'id': 'job-1',
        'client_id': None,
        'total_time_seconds': 0
    })

    t_out = t_in + timedelta(seconds=300)
    res = await clock_out_contractor(
        db=mock_db,
        job_id='job-1',
        contractor_id='c-1',
        contractor_name='Shaun Contractor',
        clock_out_time=t_out
    )

    assert res.duration_seconds == 300
    assert mock_db.contractor_jobs.update_one.called
    first_call_args = mock_db.contractor_jobs.update_one.call_args_list[0][0]
    filter_arg = first_call_args[0]
    update_arg = first_call_args[1]
    assert filter_arg.get('checklist.id') == 'task-tint-101'
    assert update_arg['$inc']['checklist.$.duration_seconds'] == 300
    assert update_arg['$inc']['total_time_seconds'] == 300


# ===========================================================================
# 3. Average Time per Task Analytics
# ===========================================================================

@pytest.mark.anyio
async def test_analytics_computes_average_time_per_task():
    """Requirement: Analytics aggregates average duration and count per service task."""
    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_jobs.count_documents = AsyncMock(return_value=2)

    sample_jobs = [
        {
            'id': 'j-1', 'status': 'completed', 'model_name': 'BYD Atto 3',
            'assigned_contractor_name': 'Contractor A', 'total_time_seconds': 1800,
            'checklist': [
                {'title': 'Window Tint 2 Front', 'duration_seconds': 1800}
            ]
        },
        {
            'id': 'j-2', 'status': 'completed', 'model_name': 'BYD Atto 3',
            'assigned_contractor_name': 'Contractor A', 'total_time_seconds': 1200,
            'checklist': [
                {'title': 'Window Tint 2 Front', 'duration_seconds': 1200}
            ]
        }
    ]

    async def mock_cursor(*args, **kwargs):
        for j in sample_jobs:
            yield j

    mock_db.contractor_jobs.find = MagicMock(side_effect=mock_cursor)

    res = await compute_contractor_analytics(db=mock_db)
    assert 'average_time_per_task' in res
    assert len(res['average_time_per_task']) == 1

    task_stat = res['average_time_per_task'][0]
    assert task_stat['task_title'] == 'Window Tint 2 Front'
    assert task_stat['avg_minutes'] == 25.0
    assert task_stat['total_completed'] == 2


# ===========================================================================
# 4. Scheduled Polling & Fallback Worker
# ===========================================================================

@pytest.mark.anyio
async def test_poll_delivery_centre_integration_auto_creates_jobs():
    """Requirement: 5-15 min background polling creates jobs from Delivery Centre clients."""
    mock_db = MagicMock()
    mock_db.clients = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_sync_logs = MagicMock()
    mock_db.contractor_activities = MagicMock()
    mock_db.users = MagicMock()

    client_data = {
        'id': 'dc-client-99',
        'vehicle': 'BYD Sealion 7',
        'vin': 'LC0SEALION999999',
        'delivery_date': '2026-11-20',
        'stage': 'Pre-Delivery',
        'accessories': [{'name': 'Ceramic Coating'}]
    }

    async def mock_client_cursor():
        yield client_data

    mock_db.clients.find = MagicMock(return_value=mock_client_cursor())
    mock_db.contractor_jobs.find_one = AsyncMock(return_value=None)
    mock_db.contractor_jobs.insert_one = AsyncMock()
    mock_db.contractor_sync_logs.insert_one = AsyncMock()
    mock_db.contractor_activities.insert_one = AsyncMock()
    mock_db.clients.find_one = AsyncMock(return_value=client_data)
    mock_db.users.find_one = AsyncMock(return_value=None)

    async def mock_empty_cursor():
        if False:
            yield None

    mock_db.contractor_jobs.find = MagicMock(return_value=mock_empty_cursor())

    poll_res = await poll_delivery_centre_integration(mock_db)
    assert poll_res['status'] == 'success'
    assert poll_res['created_jobs'] == 1
    assert mock_db.contractor_jobs.insert_one.called


# ===========================================================================
# 5. Inbound Webhook Event Automation
# ===========================================================================

@pytest.mark.anyio
async def test_inbound_webhook_event_automation():
    """Requirement: Webhooks automatically create jobs, update due dates & flag urgent blockers."""
    from routers.contractor import receive_external_webhook

    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_sync_logs = MagicMock()
    mock_db.contractor_activities = MagicMock()
    mock_db.contractor_activities.insert_one = AsyncMock()
    mock_db.clients = MagicMock()

    # A. Test due date update event
    mock_db.contractor_jobs.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
    mock_db.contractor_sync_logs.insert_one = AsyncMock()

    async def mock_json():
        return {
            'event': 'job.due_date_updated',
            'client_id': 'c-123',
            'due_date': '2026-12-01'
        }

    mock_request = MagicMock()
    mock_request.json = AsyncMock(side_effect=mock_json)

    with patch('routers.contractor.get_db', return_value=mock_db):
        res = await receive_external_webhook(mock_request)
        assert res['received'] is True
        assert 'due_date_updated:1' in res['action']
        mock_db.contractor_jobs.update_many.assert_called_once()

    # B. Test urgent flag event
    target_job = {'id': 'job-xyz', 'client_id': 'c-123', 'is_urgent': False}
    mock_db.contractor_jobs.find_one = AsyncMock(return_value=target_job)
    mock_db.contractor_jobs.update_one = AsyncMock()

    async def mock_urgent_json():
        return {
            'event': 'job.urgent_flag',
            'client_id': 'c-123',
            'description': 'Customer flight moved up!'
        }

    mock_urgent_req = MagicMock()
    mock_urgent_req.json = AsyncMock(side_effect=mock_urgent_json)

    with patch('routers.contractor.get_db', return_value=mock_db), \
         patch('routers.contractor.notify_urgent_issue_flagged', new=AsyncMock()):
        urgent_res = await receive_external_webhook(mock_urgent_req)
        assert urgent_res['received'] is True
        assert 'urgent_flagged:job-xyz' in urgent_res['action']
        mock_db.contractor_jobs.update_one.assert_called()


# ===========================================================================
# 6. Evidence Mirroring to Delivery Centre Client Documents
# ===========================================================================

@pytest.mark.anyio
async def test_evidence_mirrors_to_delivery_centre():
    """Requirement: Evidence photos/videos mirror as ClientDocument into Delivery Centre."""
    from routers.contractor import upload_job_evidence
    from fastapi import UploadFile

    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_evidence = MagicMock()
    mock_db.contractor_activities = MagicMock()
    mock_db.clients = MagicMock()

    mock_db.contractor_jobs.find_one = AsyncMock(return_value={
        'id': 'job-ev-1',
        'client_id': 'client-dc-777',
        'model_name': 'BYD Atto 3'
    })
    mock_db.contractor_evidence.insert_one = AsyncMock()
    mock_db.contractor_activities.insert_one = AsyncMock()
    mock_db.clients.update_one = AsyncMock()

    mock_user = User(
        id='admin-user-1',
        email='admin@omnisuiteai.com',
        name='Admin User',
        role='super_admin'
    )

    mock_file = MagicMock(spec=UploadFile)
    mock_file.filename = 'after_coating.jpg'
    mock_file.content_type = 'image/jpeg'
    mock_file.read = AsyncMock(return_value=b'image-binary-stream')

    with patch('routers.contractor.get_db', return_value=mock_db), \
         patch('routers.contractor.put_bytes', new=AsyncMock()):
        ev_result = await upload_job_evidence(
            job_id='job-ev-1',
            stage='after',
            caption='Ceramic coat applied',
            file=mock_file,
            user=mock_user
        )
        assert ev_result.stage == 'after'
        assert ev_result.job_id == 'job-ev-1'

        mock_db.clients.update_one.assert_called_once()
        client_call = mock_db.clients.update_one.call_args[0]
        assert client_call[0] == {'id': 'client-dc-777'}
        push_doc = client_call[1]['$push']['documents']
        assert '[AFTER]' in push_doc['file_name']
        assert push_doc['status'] == 'complete'


# ===========================================================================
# 7. Integration Credentials & Polling Trigger Endpoints
# ===========================================================================

@pytest.mark.anyio
async def test_integration_config_and_poll_api():
    """Requirement: Integration config storage & manual polling endpoint."""
    from routers.contractor import get_integration_config, save_integration_config, trigger_manual_integration_poll

    mock_db = MagicMock()
    mock_db.settings = MagicMock()
    mock_db.settings.find_one = AsyncMock(return_value=None)
    mock_db.settings.update_one = AsyncMock()

    mock_user = User(
        id='super-1',
        email='shaun@omnisuiteai.com',
        name='Shaun Sumaru',
        role='super_admin'
    )

    # 1. Get default config
    with patch('routers.contractor.get_db', return_value=mock_db):
        cfg = await get_integration_config(user=mock_user)
        assert cfg['polling_interval_minutes'] == 10
        assert 'api_url' in cfg

    # 2. Save config
    mock_req = MagicMock()
    mock_req.json = AsyncMock(return_value={
        'api_url': 'https://deliverycentre.goodshowroom.com',
        'api_key': 'prod-key-xyz',
        'webhook_url': 'https://byd-panel.omnisuiteai.com/api/contractor/integrations/webhook',
        'polling_interval_minutes': 15,
        'auto_poll_enabled': True
    })

    with patch('routers.contractor.get_db', return_value=mock_db):
        save_res = await save_integration_config(mock_req, user=mock_user)
        assert save_res['success'] is True
        assert save_res['config']['api_key'] == 'prod-key-xyz'
        assert save_res['config']['polling_interval_minutes'] == 15

    # 3. Manual polling trigger
    with patch('routers.contractor.get_db', return_value=mock_db), \
         patch('routers.contractor.poll_delivery_centre_integration', new=AsyncMock(return_value={'status': 'success', 'created_jobs': 2})):
        poll_res = await trigger_manual_integration_poll(user=mock_user)
        assert poll_res['status'] == 'success'
        assert poll_res['created_jobs'] == 2
