import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from contractor_models import (
    Job, JobCreate, JobTask, JobTaskCreate, JobTemplate, JobTemplateCreate,
    JobTimeEntry, JobLocationEntry, JobEvidence, JobActivity,
    IntegrationSyncLog,
    OfflineActionItem, OfflineBatchSyncRequest, BulkJobImportRequest,
    PushSubscriptionPayload, PushSubscriptionKeys
)
from services.contractor_service import (
    create_job_from_delivery_client,
    sync_job_to_delivery_client,
    clock_in_contractor,
    clock_out_contractor,
    register_vehicle_location,
    compute_contractor_analytics,
    generate_contractor_jobs_csv,
    generate_contractor_jobs_xlsx,
    generate_contractor_jobs_pdf,
    process_offline_batch_sync,
    bulk_import_contractor_jobs
)
from services.push_service import (
    get_or_init_vapid,
    save_push_subscription,
    remove_push_subscription,
    send_web_push
)
from models import User


# ===========================================================================
# 1. Models & Business Rules Tests
# ===========================================================================

def test_job_model_derives_last_6_vin():
    job = Job(
        model_name="BYD Sealion 7",
        full_vin="LC0E74BD5R0123456",
        delivery_date_time="2026-10-15"
    )
    assert job.full_vin == "LC0E74BD5R0123456"
    assert job.last_6_vin == "123456"
    assert job.status == "requested"
    assert job.is_urgent is False
    assert job.priority == "normal"
    assert job.total_time_seconds == 0


def test_job_urgent_priority_synchronization():
    # Setting priority='urgent' should set is_urgent=True
    job1 = Job(
        model_name="BYD Shark 6",
        full_vin="LC0SHARK678999999",
        priority="urgent"
    )
    assert job1.is_urgent is True
    assert job1.priority == "urgent"

    # Setting is_urgent=True should set priority='urgent'
    job2 = Job(
        model_name="BYD Atto 3",
        full_vin="LC0ATTO3333111222",
        is_urgent=True
    )
    assert job2.priority == "urgent"
    assert job2.is_urgent is True


def test_job_tasks_checklist():
    tasks = [
        JobTask(title="Ceramic Tint 2 Front"),
        JobTask(title="Paint Protection Coating"),
        JobTask(title="Floor Mats Deep Dish")
    ]
    job = Job(
        model_name="BYD Seal",
        full_vin="LC0SEAL222333444",
        checklist=tasks
    )
    assert len(job.checklist) == 3
    assert job.checklist[0].title == "Ceramic Tint 2 Front"
    assert job.checklist[0].completed is False
    assert job.checklist[1].completed is False


def test_job_template_model():
    template = JobTemplate(
        name="BYD Delivery Standard Tint & Detail",
        tasks=[
            "2x Front Window Tinting",
            "Exterior High-Gloss Wash & Wax",
            "Interior Vacuum & Dash Wipe",
            "Fit Deep Dish Mats"
        ],
        estimated_duration_minutes=90,
        category="Detailing"
    )
    assert template.name == "BYD Delivery Standard Tint & Detail"
    assert len(template.tasks) == 4
    assert template.estimated_duration_minutes == 90
    assert template.category == "Detailing"


def test_contractor_user_role_and_profile():
    contractor = User(
        email="tint.specialist@external.com.au",
        name="Sam Tinting",
        role="contractor",
        company_name="Precision Tint & Auto Care",
        contractor_phone="0412 345 678",
        contractor_skills=["Tinting", "PPF", "Ceramic Coating"]
    )
    assert contractor.role == "contractor"
    assert contractor.company_name == "Precision Tint & Auto Care"
    assert "Tinting" in contractor.contractor_skills


# ===========================================================================
# 2. Time Tracking Service Tests
# ===========================================================================

@pytest.mark.anyio
async def test_clock_in_and_out_calculates_duration():
    mock_db = MagicMock()
    mock_db.contractor_time_logs = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_activities = MagicMock()

    # Initially not clocked in
    mock_db.contractor_time_logs.find_one = AsyncMock(return_value=None)
    mock_db.contractor_time_logs.insert_one = AsyncMock()
    mock_db.contractor_time_logs.update_one = AsyncMock()
    mock_db.contractor_jobs.find_one = AsyncMock(return_value={'id': 'job-1', 'status': 'requested'})
    mock_db.contractor_jobs.update_one = AsyncMock()
    mock_db.contractor_activities.insert_one = AsyncMock()

    # 1. Clock in
    clock_in_entry = await clock_in_contractor(
        db=mock_db,
        job_id="job-1",
        contractor_id="user-c1",
        contractor_name="Sam Tint",
        task_id="task-1",
        notes="Starting front windows"
    )
    assert clock_in_entry.job_id == "job-1"
    assert clock_in_entry.contractor_id == "user-c1"
    assert clock_in_entry.clock_out is None
    assert mock_db.contractor_time_logs.insert_one.called
    assert mock_db.contractor_jobs.update_one.called  # Should move job to in_progress

    # 2. Clock out
    start_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    mock_db.contractor_time_logs.find_one = AsyncMock(side_effect=[
        {
            'id': clock_in_entry.id,
            'job_id': 'job-1',
            'contractor_id': 'user-c1',
            'contractor_name': 'Sam Tint',
            'clock_in': start_time,
            'clock_out': None
        },
        {
            'id': clock_in_entry.id,
            'job_id': 'job-1',
            'contractor_id': 'user-c1',
            'contractor_name': 'Sam Tint',
            'clock_in': start_time,
            'clock_out': datetime.now(timezone.utc),
            'duration_seconds': 2700,
            'notes': 'Completed tint'
        }
    ])

    clock_out_res = await clock_out_contractor(
        db=mock_db,
        job_id="job-1",
        contractor_id="user-c1",
        contractor_name="Sam Tint",
        notes="Completed tint"
    )
    assert clock_out_res.duration_seconds >= 2690
    assert mock_db.contractor_jobs.update_one.called


@pytest.mark.anyio
async def test_double_clock_in_prevented():
    mock_db = MagicMock()
    mock_db.contractor_time_logs = MagicMock()
    # Already has active entry
    mock_db.contractor_time_logs.find_one = AsyncMock(return_value={'id': 'active-log', 'clock_out': None})

    with pytest.raises(ValueError, match="already clocked on"):
        await clock_in_contractor(
            db=mock_db,
            job_id="job-1",
            contractor_id="user-c1",
            contractor_name="Sam Tint"
        )


# ===========================================================================
# 3. Location Check-In Tests
# ===========================================================================

@pytest.mark.anyio
async def test_register_vehicle_location():
    mock_db = MagicMock()
    mock_db.contractor_locations = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_activities = MagicMock()

    mock_db.contractor_locations.insert_one = AsyncMock()
    mock_db.contractor_jobs.update_one = AsyncMock()
    mock_db.contractor_activities.insert_one = AsyncMock()
    # Job lookup for sync
    mock_db.contractor_jobs.find_one = AsyncMock(return_value={'id': 'job-1', 'client_id': None})

    entry = await register_vehicle_location(
        db=mock_db,
        job_id="job-1",
        user_id="user-c1",
        user_name="Sam Tint",
        latitude=-37.7788,
        longitude=145.0123,
        accuracy=5.0,
        location_name="BYD Fairfield Bay 6",
        notes="Parked in detailing bay"
    )
    assert entry.latitude == -37.7788
    assert entry.longitude == 145.0123
    assert entry.location_name == "BYD Fairfield Bay 6"
    assert mock_db.contractor_locations.insert_one.called
    assert mock_db.contractor_jobs.update_one.called


# ===========================================================================
# 4. Two-Way Integration Tests
# ===========================================================================

@pytest.mark.anyio
async def test_create_job_from_delivery_client():
    mock_db = MagicMock()
    mock_db.clients = MagicMock()
    mock_db.users = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_activities = MagicMock()
    mock_db.contractor_sync_logs = MagicMock()

    mock_client = {
        'id': 'client-dc-1',
        'name': 'Sandra Delivery',
        'vehicle': 'BYD Atto 2 Premium Ski White',
        'vin': 'LC0ATTO2PREM987654',
        'rego': 'VIC-222',
        'delivery_date': '2026-10-18',
        'site_location': 'Fairfield',
        'accessories': [
            {'name': 'Ceramic Window Tint (2x Front)', 'status': 'Pending Order'},
            {'name': 'Floor Mats Moulded (Deep Dish)', 'status': 'Pending Order'}
        ]
    }
    mock_db.clients.find_one = AsyncMock(return_value=mock_client)
    mock_db.users.find_one = AsyncMock(return_value={'id': 'user-c1', 'name': 'Tint Master'})
    mock_db.contractor_jobs.insert_one = AsyncMock()
    mock_db.contractor_activities.insert_one = AsyncMock()
    mock_db.contractor_sync_logs.insert_one = AsyncMock()

    job = await create_job_from_delivery_client(
        db=mock_db,
        client_id="client-dc-1",
        assigned_contractor_id="user-c1",
        due_date="2026-10-17",
        priority="high"
    )

    assert job.client_id == "client-dc-1"
    assert job.model_name == "BYD Atto 2 Premium Ski White"
    assert job.full_vin == "LC0ATTO2PREM987654"
    assert job.last_6_vin == "987654"
    assert job.assigned_contractor_name == "Tint Master"
    assert len(job.checklist) == 2
    assert "Fit Ceramic Window Tint" in job.checklist[0].title
    assert mock_db.contractor_jobs.insert_one.called
    assert mock_db.contractor_sync_logs.insert_one.called


@pytest.mark.anyio
async def test_sync_completed_job_to_delivery_client():
    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.clients = MagicMock()
    mock_db.contractor_sync_logs = MagicMock()

    mock_job = {
        'id': 'job-1',
        'client_id': 'client-dc-1',
        'model_name': 'BYD Seal',
        'status': 'completed',
        'total_time_seconds': 3600,
        'bay_location': 'Bay 3',
        'checklist': [
            {'title': 'Fit Ceramic Window Tint (2x Front)', 'completed': True},
            {'title': 'Fit Floor Mats Moulded', 'completed': True}
        ]
    }
    mock_client = {
        'id': 'client-dc-1',
        'name': 'Sandra Delivery',
        'accessories': [
            {'name': 'Ceramic Window Tint (2x Front)', 'status': 'Pending Order'},
            {'name': 'Floor Mats Moulded (Deep Dish)', 'status': 'Pending Order'}
        ]
    }

    mock_db.contractor_jobs.find_one = AsyncMock(return_value=mock_job)
    mock_db.clients.find_one = AsyncMock(return_value=mock_client)
    mock_db.clients.update_one = AsyncMock()
    mock_db.contractor_sync_logs.insert_one = AsyncMock()

    sync_result = await sync_job_to_delivery_client(mock_db, "job-1", "Test Contractor")
    assert sync_result is not None
    assert sync_result.direction == "outbound_to_delivery"
    assert sync_result.status == "success"
    assert mock_db.clients.update_one.called
    assert mock_db.contractor_sync_logs.insert_one.called


# ===========================================================================
# 5. Reporting & CSV Export Tests
# ===========================================================================

@pytest.mark.anyio
async def test_compute_contractor_analytics():
    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()

    mock_jobs = [
        {
            'id': 'j1',
            'full_vin': 'LC011111111111111',
            'last_6_vin': '111111',
            'model_name': 'BYD Sealion 7',
            'assigned_contractor_name': 'Contractor A',
            'status': 'completed',
            'total_time_seconds': 3600,
            'due_date': '2026-10-01'
        },
        {
            'id': 'j2',
            'full_vin': 'LC022222222222222',
            'last_6_vin': '222222',
            'model_name': 'BYD Shark 6',
            'assigned_contractor_name': 'Contractor B',
            'status': 'in_progress',
            'total_time_seconds': 1800,
            'due_date': '2026-08-01'  # overdue
        }
    ]

    class AsyncCursor:
        def __init__(self, items):
            self.items = items
        def __aiter__(self):
            return self._gen()
        async def _gen(self):
            for item in self.items:
                yield item

    mock_db.contractor_jobs.count_documents = AsyncMock(side_effect=[2, 1])
    mock_db.contractor_jobs.find = MagicMock(return_value=AsyncCursor(mock_jobs))

    analytics = await compute_contractor_analytics(mock_db)
    assert analytics['total_jobs'] == 2
    assert analytics['completed_jobs'] == 1
    assert analytics['completion_rate_percent'] == 50.0
    assert analytics['overdue_jobs_count'] == 1
    assert analytics['jobs_by_status']['completed'] == 1
    assert analytics['jobs_by_status']['in_progress'] == 1
    assert len(analytics['completed_per_vin']) == 1


@pytest.mark.anyio
async def test_generate_contractor_jobs_csv():
    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()

    mock_jobs = [
        {
            'id': 'job-csv-1',
            'model_name': 'BYD Atto 3',
            'full_vin': 'LC0ATTO3999999999',
            'last_6_vin': '999999',
            'rego': 'VIC-CSV',
            'status': 'completed',
            'priority': 'normal',
            'due_date': '2026-10-15',
            'delivery_date_time': '2026-10-16',
            'assigned_contractor_name': 'Sam Tint',
            'total_time_seconds': 5400,
            'checklist': [{'title': 'Tint', 'completed': True}],
            'issue_flag': False,
            'issue_description': '',
            'created_at': '2026-09-29T00:00:00Z'
        }
    ]

    class AsyncCursor:
        def __init__(self, items):
            self.items = items
        def sort(self, *args, **kwargs):
            return self
        def __aiter__(self):
            return self._gen()
        async def _gen(self):
            for item in self.items:
                yield item

    mock_db.contractor_jobs.find = MagicMock(return_value=AsyncCursor(mock_jobs))

    csv_data = await generate_contractor_jobs_csv(mock_db)
    assert 'Job ID,Model Name,Full VIN,Last 6 VIN,Rego,Status' in csv_data
    assert 'job-csv-1,BYD Atto 3,LC0ATTO3999999999,999999,VIC-CSV,completed' in csv_data
    assert 'Sam Tint' in csv_data


@pytest.mark.anyio
async def test_generate_contractor_jobs_xlsx():
    mock_db = MagicMock()
    mock_jobs = [
        {
            'id': 'job-xlsx-1',
            'model_name': 'BYD Seal',
            'full_vin': 'LC0SEAL1234567890',
            'last_6_vin': '567890',
            'rego': 'SEAL-01',
            'status': 'in_progress',
            'priority': 'urgent',
            'is_urgent': True,
            'due_date': '2026-10-20',
            'delivery_date_time': '2026-10-21',
            'assigned_contractor_name': 'Alex Detailing',
            'total_time_seconds': 3600,
            'checklist': [{'title': 'Full Tint', 'completed': True}],
            'issue_flag': False,
            'issue_description': None,
            'bay_location': 'Bay 3',
            'created_at': '2026-09-29T10:00:00Z'
        }
    ]

    class AsyncCursor:
        def __init__(self, items):
            self.items = items
        def sort(self, *args, **kwargs):
            return self
        def __aiter__(self):
            return self._gen()
        async def _gen(self):
            for item in self.items:
                yield item

    mock_db.contractor_jobs.find = MagicMock(return_value=AsyncCursor(mock_jobs))

    xlsx_bytes = await generate_contractor_jobs_xlsx(mock_db)
    assert isinstance(xlsx_bytes, bytes)
    assert len(xlsx_bytes) > 100
    # ZIP PK magic header
    assert xlsx_bytes[:2] == b'PK'


@pytest.mark.anyio
async def test_generate_contractor_jobs_pdf():
    mock_db = MagicMock()
    mock_jobs = [
        {
            'id': 'job-pdf-1',
            'model_name': 'BYD Dolphin',
            'full_vin': 'LC0DOLPHIN1234567',
            'last_6_vin': '234567',
            'rego': 'DOL-99',
            'status': 'requested',
            'priority': 'normal',
            'due_date': '2026-10-18',
            'assigned_contractor_name': 'Sarah Ceramic',
            'total_time_seconds': 1800,
            'checklist': [{'title': 'Ceramic Coat', 'completed': False}],
            'issue_flag': True,
            'issue_description': 'Rear scratch',
            'created_at': '2026-09-29T10:00:00Z'
        }
    ]

    class AsyncCursor:
        def __init__(self, items):
            self.items = items
        def sort(self, *args, **kwargs):
            return self
        def __aiter__(self):
            return self._gen()
        async def _gen(self):
            for item in self.items:
                yield item

    mock_db.contractor_jobs.find = MagicMock(return_value=AsyncCursor(mock_jobs))
    mock_db.users.find_one = AsyncMock(return_value=None)

    pdf_bytes = await generate_contractor_jobs_pdf(mock_db)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 200
    # PDF magic header
    assert pdf_bytes.startswith(b'%PDF-')


@pytest.mark.anyio
async def test_bulk_import_contractor_jobs():
    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_jobs.insert_one = AsyncMock(return_value=MagicMock())
    mock_db.contractor_activities = MagicMock()
    mock_db.contractor_activities.insert_one = AsyncMock(return_value=MagicMock())
    mock_db.contractor_templates = MagicMock()
    mock_db.contractor_templates.find_one = AsyncMock(return_value={
        'id': 'tmpl-1',
        'tasks': ['Clean exterior', 'Apply tint']
    })
    mock_db.users = MagicMock()
    mock_db.users.find_one = AsyncMock(return_value={'id': 'c-1', 'name': 'Contractor Bob'})

    user = User(id='admin-1', email='admin@byd.com', name='Admin', role='admin')
    jobs_input = [
        JobCreate(
            model_name='BYD Sealion 7',
            full_vin='LC0SEALION111111',
            template_id='tmpl-1',
            assigned_contractor_id='c-1'
        ),
        JobCreate(
            model_name='BYD Shark 6',
            full_vin='LC0SHARK6222222',
            priority='urgent'
        )
    ]

    res = await bulk_import_contractor_jobs(mock_db, user, jobs_input, apply_templates=True)
    assert res.total_submitted == 2
    assert res.imported_count == 2
    assert res.failed_count == 0
    assert len(res.created_job_ids) == 2
    assert mock_db.contractor_jobs.insert_one.call_count == 2


@pytest.mark.anyio
async def test_process_offline_batch_sync():
    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_jobs.find_one = AsyncMock(return_value={
        'id': 'job-sync-1',
        'model_name': 'BYD Atto 3',
        'full_vin': 'LC0ATTO3999999999',
        'last_6_vin': '999999',
        'status': 'requested',
        'total_time_seconds': 0,
        'checklist': [{'id': 'task-1', 'title': 'Tint', 'completed': False}]
    })
    mock_db.contractor_jobs.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    mock_db.contractor_time_logs = MagicMock()
    mock_db.contractor_time_logs.find_one = AsyncMock(return_value=None)
    mock_db.contractor_time_logs.insert_one = AsyncMock(return_value=MagicMock())
    mock_db.contractor_activities = MagicMock()
    mock_db.contractor_activities.insert_one = AsyncMock(return_value=MagicMock())
    mock_db.contractor_locations = MagicMock()
    mock_db.contractor_locations.insert_one = AsyncMock(return_value=MagicMock())
    mock_db.contractor_sync_logs = MagicMock()
    mock_db.contractor_sync_logs.insert_one = AsyncMock(return_value=MagicMock())

    user = User(id='contractor-1', email='con@byd.com', name='Con', role='contractor')

    actions = [
        OfflineActionItem(
            client_action_id='act-1',
            action_type='clock_in',
            job_id='job-sync-1',
            payload={'notes': 'Start shift offline'},
            queued_at=datetime.now(timezone.utc)
        ),
        OfflineActionItem(
            client_action_id='act-2',
            action_type='update_task',
            job_id='job-sync-1',
            payload={'task_id': 'task-1', 'completed': True},
            queued_at=datetime.now(timezone.utc)
        ),
        OfflineActionItem(
            client_action_id='act-3',
            action_type='location_checkin',
            job_id='job-sync-1',
            payload={'latitude': -33.87, 'longitude': 150.95, 'location_name': 'Bay 4'},
            queued_at=datetime.now(timezone.utc)
        )
    ]

    resp = await process_offline_batch_sync(mock_db, user, actions)
    assert resp.processed == 3
    assert resp.succeeded == 3
    assert resp.failed == 0
    assert len(resp.results) == 3
    assert mock_db.contractor_sync_logs.insert_one.called


# ===========================================================================
# 6. HTTP API Endpoint Tests (TestClient)
# ===========================================================================
from fastapi.testclient import TestClient
from server import app
from auth import get_current_user, require_admin, require_contractor_or_admin


def test_contractor_api_endpoints_with_mock_db():
    admin_user = User(
        id="admin-1",
        email="admin@bydfairfield.com.au",
        name="Admin Boss",
        role="admin"
    )
    
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_contractor_or_admin] = lambda: admin_user

    mock_db = MagicMock()
    mock_db.contractor_jobs = MagicMock()
    mock_db.contractor_job_templates = MagicMock()
    mock_db.contractor_activities = MagicMock()
    mock_db.contractor_sync_logs = MagicMock()
    mock_db.contractor_time_logs = MagicMock()
    mock_db.clients = MagicMock()
    mock_db.users = MagicMock()

    class MockCursor:
        def __init__(self, data):
            self.data = data
        def sort(self, *args, **kwargs):
            return self
        def skip(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            return self
        def __aiter__(self):
            return self._gen()
        async def _gen(self):
            for d in self.data:
                yield d

    sample_job = {
        'id': 'job-route-1',
        'model_name': 'BYD Sealion 7',
        'full_vin': 'LC0SEALION7777777',
        'last_6_vin': '777777',
        'status': 'requested',
        'priority': 'normal',
        'is_urgent': False,
        'assigned_contractor_id': 'admin-1',
        'checklist': [{'id': 'task-1', 'title': 'Ceramic Tint', 'completed': False}],
        'created_at': datetime.now(timezone.utc),
        'updated_at': datetime.now(timezone.utc)
    }

    mock_db.contractor_jobs.find = MagicMock(return_value=MockCursor([sample_job]))
    mock_db.contractor_jobs.find_one = AsyncMock(return_value=sample_job)
    mock_db.contractor_jobs.insert_one = AsyncMock()
    mock_db.contractor_jobs.count_documents = AsyncMock(return_value=1)
    mock_db.contractor_activities.insert_one = AsyncMock()
    mock_db.contractor_activities.find = MagicMock(return_value=MockCursor([]))
    mock_db.contractor_time_logs.count_documents = AsyncMock(return_value=0)
    mock_db.contractor_sync_logs.insert_one = AsyncMock()
    mock_db.contractor_sync_logs.count_documents = AsyncMock(side_effect=[5, 5, 0, 3, 5])
    mock_db.contractor_sync_logs.find = MagicMock(return_value=MockCursor([]))

    with patch('routers.contractor.get_db', return_value=mock_db), \
         patch('services.contractor_service.dispatch_contractor_webhook'):
        
        client = TestClient(app)

        # 1. List jobs
        resp = client.get('/api/contractor/jobs')
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) == 1
        assert jobs[0]['model_name'] == 'BYD Sealion 7'

        # 2. Create job
        resp = client.post('/api/contractor/jobs', json={
            'model_name': 'BYD Shark 6',
            'full_vin': 'LC0SHARK600000000',
            'priority': 'urgent',
            'checklist': [{'title': 'Tint 2 Front'}]
        })
        assert resp.status_code == 201
        created = resp.json()
        assert created['model_name'] == 'BYD Shark 6'
        assert created['last_6_vin'] == '000000'
        assert created['is_urgent'] is True

        # 3. Get single job
        resp = client.get('/api/contractor/jobs/job-route-1')
        assert resp.status_code == 200
        assert resp.json()['id'] == 'job-route-1'

        # 4. Dashboard metrics
        resp = client.get('/api/contractor/dashboard')
        assert resp.status_code == 200
        dash = resp.json()
        assert 'pipeline' in dash
        assert dash['total_jobs'] == 1

        # 5. Integration sync status with health metrics
        resp = client.get('/api/contractor/integrations/sync-status')
        assert resp.status_code == 200
        sync_st = resp.json()
        assert sync_st['health'] == 'Healthy'
        assert sync_st['uptime_status'] == 'Operational'
        assert 'error_rate_pct' in sync_st
        assert 'data_volume_24h' in sync_st

        # 6. Webhook receiver
        resp = client.post('/api/contractor/integrations/webhook', json={
            'event': 'delivery.accessory_ordered',
            'client_id': 'client-1'
        })
        assert resp.status_code == 200
        assert resp.json()['received'] is True

        # 7. Multi-Format Export Endpoints (Excel & PDF)
        mock_db.contractor_jobs.find = MagicMock(return_value=MockCursor([
            {
                'id': 'job-exp-1',
                'model_name': 'BYD Atto 3',
                'full_vin': 'LC0ATTO3000000000',
                'last_6_vin': '000000',
                'status': 'completed',
                'priority': 'normal',
                'checklist': [],
                'total_time_seconds': 0,
                'created_at': '2026-09-29T00:00:00Z'
            }
        ]))

        resp_xlsx = client.get('/api/contractor/export/xlsx')
        assert resp_xlsx.status_code == 200
        assert resp_xlsx.content[:2] == b'PK'

        resp_pdf = client.get('/api/contractor/export/pdf')
        assert resp_pdf.status_code == 200
        assert resp_pdf.content.startswith(b'%PDF-')

        resp_csv = client.get('/api/contractor/export?format=csv')
        assert resp_csv.status_code == 200
        assert 'Job ID' in resp_csv.text

        # 8. Bulk Job Import Endpoint
        resp_bulk = client.post('/api/contractor/jobs/bulk-import', json={
            'jobs': [
                {'model_name': 'BYD Seal', 'full_vin': 'LC0SEAL7777777777'}
            ]
        })
        assert resp_bulk.status_code == 200
        bulk_res = resp_bulk.json()
        assert bulk_res['total_submitted'] == 1
        assert bulk_res['imported_count'] == 1

        # 9. Offline Batch Sync Endpoint
        mock_db.contractor_jobs.find_one = AsyncMock(return_value={
            'id': 'job-route-1',
            'model_name': 'BYD Sealion 7',
            'checklist': []
        })
        resp_sync = client.post('/api/contractor/sync/offline-batch', json={
            'actions': [
                {
                    'client_action_id': 'c-act-1',
                    'action_type': 'add_comment',
                    'job_id': 'job-route-1',
                    'payload': {'message': 'Offline test note'}
                }
            ]
        })
        assert resp_sync.status_code == 200
        sync_res = resp_sync.json()
        assert sync_res['processed'] == 1
        assert sync_res['succeeded'] == 1

        # 10. Web Push Endpoints
        mock_db.app_settings.find_one = AsyncMock(return_value=None)
        mock_db.app_settings.update_one = AsyncMock(return_value=MagicMock())
        mock_db.push_subscriptions.update_one = AsyncMock(return_value=MagicMock())
        mock_db.push_subscriptions.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))

        resp_key = client.get('/api/contractor/push/vapid-public-key')
        assert resp_key.status_code == 200
        assert 'publicKey' in resp_key.json()
        assert len(resp_key.json()['publicKey']) > 20

        resp_sub = client.post('/api/contractor/push/subscribe', json={
            'endpoint': 'https://fcm.googleapis.com/fcm/send/test-sub-1',
            'keys': {'p256dh': 'test-p256dh', 'auth': 'test-auth'},
            'device_info': 'Mobile Chrome'
        })
        assert resp_sub.status_code == 200
        assert resp_sub.json()['success'] is True

        resp_unsub = client.delete('/api/contractor/push/subscribe?endpoint=https://fcm.googleapis.com/fcm/send/test-sub-1')
        assert resp_unsub.status_code == 200
        assert resp_unsub.json()['success'] is True

    # Clean up overrides
    app.dependency_overrides.clear()

