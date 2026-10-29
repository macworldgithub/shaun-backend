from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, Request, status
from fastapi.responses import Response
from typing import Optional, List, Dict, Any, Annotated
from datetime import datetime, timezone, timedelta
import uuid
import logging

log = logging.getLogger('contractor_router')

from db import get_db
from models import (
    User,
    Job, JobCreate, JobUpdate, JobStatusUpdate, JobFlagIssue,
    JobTask, JobTaskCreate, JobTaskUpdate,
    JobTemplate, JobTemplateCreate, JobTemplateUpdate, JobFromTemplateRequest,
    JobTimeEntry, ClockInRequest, ClockOutRequest,
    JobEvidence,
    JobLocationEntry, LocationCheckInRequest,
    JobActivity, JobCommentCreate,
    IntegrationSyncLog, ConvertClientToJobRequest,
    PushSubscriptionPayload, SendPushTestRequest,
    BulkJobImportRequest, BulkJobImportResponse,
    OfflineBatchSyncRequest, OfflineBatchSyncResponse,
    ClientDocument
)
from auth import (
    get_current_user, require_admin, require_super, effective_site, assert_site_access,
    require_contractor_or_admin
)
from services.storage import put_bytes, get_bytes, exists
from services.contractor_service import (
    _strip,
    log_job_activity,
    dispatch_contractor_webhook,
    sync_job_to_delivery_client,
    create_job_from_delivery_client,
    clock_in_contractor,
    clock_out_contractor,
    register_vehicle_location,
    compute_contractor_analytics,
    generate_contractor_jobs_csv,
    generate_contractor_jobs_xlsx,
    generate_contractor_jobs_pdf,
    process_offline_batch_sync,
    bulk_import_contractor_jobs,
    poll_delivery_centre_integration
)
from services.push_service import (
    get_vapid_public_key,
    save_push_subscription,
    remove_push_subscription,
    send_web_push,
    notify_contractor_job_assigned,
    notify_urgent_issue_flagged
)

from services.contractor_access import assert_job_access, job_scope, validate_assignment, site_filter
from services.contractor_integration import authenticate_webhook, integration_config, save_config, queue_job_event, run_integration_worker

async def _safe_dispatch(db, event, job_id, payload):
    await queue_job_event(db, event, job_id, payload)


router = APIRouter(prefix='/api/contractor', tags=['contractor'])


# ---------------------------------------------------------------------------
# 0. Contractors & Users
# ---------------------------------------------------------------------------

@router.get('/contractors', response_model=List[User])
async def list_contractors(user: User = Depends(require_admin)):
    """List available contractors for job assignment dropdowns."""
    db = get_db()
    out = []
    query = {'role': {'$in': ['contractor', 'admin', 'super_admin', 'site_admin']}, 'active': True, 'deleted_at': None}
    site = effective_site(user)
    if site:
        match = site_filter(site)
        query['$or'] = [{'locked_site': match}, {'locked_site': None, 'active_site': match}, {'locked_site': None, 'all_sites': True}, {'locked_site': None, 'allowed_sites': match}]
    async for d in db.users.find(query).sort('name', 1):
        d.pop('password_hash', None)
        out.append(User(**_strip(d)))
    return out


# ---------------------------------------------------------------------------
# 1. Job Management & Kanban Workflow
# ---------------------------------------------------------------------------

@router.get('/jobs', response_model=List[Job])
async def list_jobs(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    is_urgent: Optional[bool] = None,
    issue_flag: Optional[bool] = None,
    model: Optional[str] = None,
    vin: Optional[str] = None,
    client_id: Optional[str] = None,
    contractor_id: Optional[str] = None,
    site: Optional[str] = None,
    limit: int = Query(200, le=1000),
    skip: int = Query(0, ge=0),
    user: User = Depends(require_contractor_or_admin)
):
    """
    List contractor jobs.
    Contractors only see jobs assigned to them.
    Admins/Super Admins see all jobs with rich filtering.
    """
    db = get_db()
    query: Dict[str, Any] = job_scope(user, site)

    if user.role == 'contractor':
        query['assigned_contractor_id'] = user.id
    elif contractor_id:
        query['assigned_contractor_id'] = contractor_id

    if status:
        query['status'] = status
    if priority:
        query['priority'] = priority
    if is_urgent is not None:
        query['is_urgent'] = is_urgent
    if model:
        query['model_name'] = {'$regex': model, '$options': 'i'}
    if vin:
        query['$or'] = [
            {'full_vin': {'$regex': vin, '$options': 'i'}},
            {'last_6_vin': {'$regex': vin, '$options': 'i'}}
        ]
    if client_id:
        client_or = [{'client_id': client_id}, {'dc_job_id': client_id}]
        if '$or' in query:
            vin_or = query.pop('$or')
            query['$and'] = [{'$or': vin_or}, {'$or': client_or}]
        else:
            query['$or'] = client_or
    if issue_flag is not None:
        query['issue_flag'] = issue_flag

    out = []
    async for doc in db.contractor_jobs.find(query).sort('created_at', -1).skip(skip).limit(limit):
        out.append(Job(**_strip(doc)))
    return out


@router.post('/jobs', response_model=Job, status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    user: User = Depends(require_admin)
):
    """Create a new job card (modeled on Trello Kanban)."""
    db = get_db()
    full_vin = payload.full_vin.strip().upper()
    last_6 = payload.last_6_vin or (full_vin[-6:] if len(full_vin) >= 6 else full_vin)

    checklist_items: List[JobTask] = []
    if payload.checklist:
        for t in payload.checklist:
            checklist_items.append(JobTask(title=t.title, notes=t.notes))

    # Lookup contractor name if assigned
    site = effective_site(user, payload.site_location) or 'Fairfield'
    contractor_name = await validate_assignment(db, user, payload.assigned_contractor_id, site)

    job_data = Job(
        client_id=payload.client_id,
        model_name=payload.model_name,
        full_vin=full_vin,
        last_6_vin=last_6,
        rego=payload.rego,
        delivery_date_time=payload.delivery_date_time,
        due_date=payload.due_date,
        description=payload.description,
        priority=payload.priority or 'normal',
        is_urgent=bool(payload.is_urgent or payload.priority == 'urgent'),
        status=payload.status or 'requested',
        assigned_contractor_id=payload.assigned_contractor_id,
        assigned_contractor_name=contractor_name,
        site_location=site,
        bay_location=payload.bay_location,
        checklist=checklist_items,
        template_id=payload.template_id,
        created_by_id=user.id,
        created_by_name=user.name
    )

    await db.contractor_jobs.insert_one(job_data.model_dump(mode='json'))

    await log_job_activity(
        db=db,
        job_id=job_data.id,
        activity_type='status_change',
        author_id=user.id,
        author_name=user.name,
        message=f"Job created for {job_data.model_name} (VIN: {job_data.full_vin}) with status '{job_data.status}'"
    )

    if job_data.assigned_contractor_id:
        try:
            await notify_contractor_job_assigned(db, job_data.model_dump(mode='json'), job_data.assigned_contractor_id)
        except Exception as pe:
            log.warning("Could not dispatch push notification on job create: %s", pe)

    await sync_job_to_delivery_client(db, job_data.id, user.name)
    await _safe_dispatch(db, 'job.created', job_data.id, job_data.model_dump(mode='json'))
    return job_data


@router.get('/jobs/{job_id}', response_model=Job)
async def get_job(job_id: str, user: User = Depends(require_contractor_or_admin)):
    db = get_db()
    job_doc = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job_doc)

    if user.role == 'contractor' and job_doc.get('assigned_contractor_id') != user.id:
        raise HTTPException(403, 'You can only view your assigned jobs')

    return Job(**_strip(job_doc))


@router.patch('/jobs/{job_id}', response_model=Job)
async def update_job(
    job_id: str,
    payload: JobUpdate,
    user: User = Depends(require_contractor_or_admin)
):
    db = get_db()
    existing = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, existing)

    if user.role == 'contractor' and existing.get('assigned_contractor_id') != user.id:
        raise HTTPException(403, 'Permission denied')

    update_dict = payload.model_dump(exclude_unset=True, mode='json')
    if user.role == 'contractor':
        raise HTTPException(403, 'Only admins can edit job details or assignments')
    if 'site_location' in update_dict:
        assert_site_access(user, {'site_location': update_dict['site_location']})
    if update_dict.get('full_vin'):
        update_dict['full_vin'] = update_dict['full_vin'].strip().upper()
        update_dict['last_6_vin'] = update_dict['full_vin'][-6:]
    if 'is_urgent' in update_dict:
        update_dict['priority'] = 'urgent' if update_dict['is_urgent'] else 'normal'
    elif 'priority' in update_dict:
        update_dict['is_urgent'] = update_dict['priority'] == 'urgent'
    update_dict.pop('sync_status', None)
    update_dict.pop('sync_error', None)
    update_dict.pop('last_synced_at', None)
    if 'status' in update_dict:
        status_value = update_dict.pop('status')
        await update_job_status(job_id, JobStatusUpdate(status=status_value), user)
    if 'assigned_contractor_id' in update_dict:
        update_dict['assigned_contractor_name'] = await validate_assignment(db, user, update_dict['assigned_contractor_id'], update_dict.get('site_location', existing.get('site_location')))

    update_dict['updated_at'] = datetime.now(timezone.utc)

    updated = await db.contractor_jobs.find_one_and_update(
        {'id': job_id},
        {'$set': update_dict},
        return_document=True
    )

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='assignment' if 'assigned_contractor_id' in update_dict else 'comment',
        author_id=user.id,
        author_name=user.name,
        message=f"Job updated: {', '.join(update_dict.keys())}",
        meta=update_dict
    )

    if update_dict.get('assigned_contractor_id'):
        try:
            await notify_contractor_job_assigned(db, updated, update_dict['assigned_contractor_id'])
        except Exception as pe:
            log.warning("Could not dispatch push notification on job assignment: %s", pe)

    await sync_job_to_delivery_client(db, job_id, user.name)
    return Job(**_strip(updated))


@router.delete('/jobs/{job_id}')
async def delete_job(job_id: str, user: User = Depends(require_admin)):
    db = get_db()
    assert_job_access(user, await db.contractor_jobs.find_one({'id': job_id}))
    res = await db.contractor_jobs.delete_one({'id': job_id})
    if res.deleted_count == 0:
        raise HTTPException(404, 'Job not found')
    await log_job_activity(db, job_id, 'status_change', user.id, user.name, 'Job deleted by administrator')
    return {'success': True, 'job_id': job_id}


@router.patch('/jobs/{job_id}/status', response_model=Job)
async def update_job_status(
    job_id: str,
    payload: JobStatusUpdate,
    user: User = Depends(require_contractor_or_admin)
):
    """
    Update workflow status: requested → collected → in_progress → returned → completed → invoiced.
    """
    db = get_db()
    existing = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, existing)

    if user.role == 'contractor' and existing.get('assigned_contractor_id') != user.id:
        raise HTTPException(403, 'Permission denied')

    valid_statuses = ['requested', 'collected', 'in_progress', 'returned', 'completed', 'invoiced']
    if payload.status not in valid_statuses:
        raise HTTPException(400, f'Invalid status. Must be one of {valid_statuses}')

    now = datetime.now(timezone.utc)
    set_fields: Dict[str, Any] = {
        'status': payload.status,
        'updated_at': now
    }

    # Record milestone timestamps
    if payload.status == 'collected' and existing.get('status') != payload.status:
        set_fields['collected_at'] = now
    elif payload.status == 'in_progress' and existing.get('status') != payload.status:
        set_fields['in_progress_at'] = now
    elif payload.status == 'returned' and existing.get('status') != payload.status:
        set_fields['returned_at'] = now
    elif payload.status == 'completed' and existing.get('status') != payload.status:
        set_fields['completed_at'] = now
    elif payload.status == 'invoiced' and existing.get('status') != payload.status:
        set_fields['invoiced_at'] = now

    updated = await db.contractor_jobs.find_one_and_update(
        {'id': job_id},
        {'$set': set_fields},
        return_document=True
    )

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='status_change',
        author_id=user.id,
        author_name=user.name,
        message=f"Status changed from '{existing.get('status')}' to '{payload.status}'. Notes: {payload.notes or 'None'}",
        meta={'from': existing.get('status'), 'to': payload.status, 'notes': payload.notes}
    )

    await _safe_dispatch(db, 'job.status_updated', job_id, {'status': payload.status})

    await sync_job_to_delivery_client(db, job_id, user.name)
    return Job(**_strip(updated))


@router.patch('/jobs/{job_id}/tasks/{task_id}', response_model=Job)
async def update_job_task(
    job_id: str,
    task_id: str,
    payload: JobTaskUpdate,
    user: User = Depends(require_contractor_or_admin)
):
    """Mark task completed or edit task title/notes."""
    db = get_db()
    existing = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, existing)

    if user.role == 'contractor' and existing.get('assigned_contractor_id') != user.id:
        raise HTTPException(403, 'Permission denied')

    checklist = existing.get('checklist', [])
    task_found = False
    task_title = ''
    now = datetime.now(timezone.utc)

    for task in checklist:
        if task.get('id') == task_id:
            task_found = True
            task_title = task.get('title')
            if payload.title is not None:
                task['title'] = payload.title
            if payload.notes is not None:
                task['notes'] = payload.notes
            if payload.completed is not None:
                task['completed'] = payload.completed
                task['completed_at'] = now if payload.completed else None
                task['completed_by_id'] = user.id if payload.completed else None
                task['completed_by_name'] = user.name if payload.completed else None
            break

    if not task_found:
        raise HTTPException(404, 'Task not found in job checklist')

    updated = await db.contractor_jobs.find_one_and_update(
        {'id': job_id},
        {'$set': {'checklist': checklist, 'updated_at': now}},
        return_document=True
    )

    action_msg = "completed" if payload.completed else ("updated" if payload.completed is None else "unmarked")
    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='checklist_update',
        author_id=user.id,
        author_name=user.name,
        message=f"Task '{task_title}' {action_msg}",
        meta={'task_id': task_id, 'completed': payload.completed}
    )


    await sync_job_to_delivery_client(db, job_id, user.name)
    return Job(**_strip(updated))


@router.post('/jobs/{job_id}/tasks', response_model=Job)
async def add_job_task(
    job_id: str,
    payload: JobTaskCreate,
    user: User = Depends(require_admin)
):
    """Add a new checklist task to a job."""
    db = get_db()
    existing = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, existing)

    new_task = JobTask(title=payload.title, notes=payload.notes)
    updated = await db.contractor_jobs.find_one_and_update(
        {'id': job_id},
        {
            '$push': {'checklist': new_task.model_dump(mode='json')},
            '$set': {'updated_at': datetime.now(timezone.utc)}
        },
        return_document=True
    )

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='checklist_update',
        author_id=user.id,
        author_name=user.name,
        message=f"Added checklist task '{payload.title}'"
    )

    await sync_job_to_delivery_client(db, job_id, user.name)
    return Job(**_strip(updated))


# ---------------------------------------------------------------------------
# 2. Job Templates
# ---------------------------------------------------------------------------

@router.get('/templates', response_model=List[JobTemplate])
async def list_job_templates(_: User = Depends(require_contractor_or_admin)):
    db = get_db()
    out = []
    async for doc in db.contractor_job_templates.find().sort('name', 1):
        out.append(JobTemplate(**_strip(doc)))
    return out


@router.post('/templates', response_model=JobTemplate, status_code=status.HTTP_201_CREATED)
async def create_job_template(
    payload: JobTemplateCreate,
    user: User = Depends(require_admin)
):
    db = get_db()
    template = JobTemplate(
        name=payload.name,
        description=payload.description,
        tasks=payload.tasks,
        estimated_duration_minutes=payload.estimated_duration_minutes,
        category=payload.category or 'General'
    )
    await db.contractor_job_templates.insert_one(template.model_dump(mode='json'))
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'action': 'contractor.template.create', 'entity_id': template.id, 'created_at': datetime.now(timezone.utc)})
    return template


@router.patch('/templates/{template_id}', response_model=JobTemplate)
async def update_job_template(
    template_id: str,
    payload: JobTemplateUpdate,
    user: User = Depends(require_admin)
):
    db = get_db()
    update_dict = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update_dict['updated_at'] = datetime.now(timezone.utc)

    res = await db.contractor_job_templates.find_one_and_update(
        {'id': template_id},
        {'$set': update_dict},
        return_document=True
    )
    if not res:
        raise HTTPException(404, 'Template not found')
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'action': 'contractor.template.update', 'entity_id': template_id, 'created_at': datetime.now(timezone.utc)})
    return JobTemplate(**_strip(res))


@router.delete('/templates/{template_id}')
async def delete_job_template(template_id: str, user: User = Depends(require_admin)):
    db = get_db()
    res = await db.contractor_job_templates.delete_one({'id': template_id})
    if res.deleted_count == 0:
        raise HTTPException(404, 'Template not found')
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'action': 'contractor.template.delete', 'entity_id': template_id, 'created_at': datetime.now(timezone.utc)})
    return {'success': True, 'template_id': template_id}


@router.post('/jobs/from-template', response_model=Job, status_code=status.HTTP_201_CREATED)
async def create_job_from_template(
    payload: JobFromTemplateRequest,
    user: User = Depends(require_admin)
):
    """Instantiate a new job card pre-populated with tasks from a selected template."""
    db = get_db()
    template = await db.contractor_job_templates.find_one({'id': payload.template_id})
    if not template:
        raise HTTPException(404, 'Job template not found')

    tasks = [JobTask(title=t) for t in template.get('tasks', [])]
    full_vin = payload.full_vin.strip().upper()
    last_6 = full_vin[-6:] if len(full_vin) >= 6 else full_vin

    site = effective_site(user, payload.site_location) or 'Fairfield'
    contractor_name = await validate_assignment(db, user, payload.assigned_contractor_id, site)

    job_data = Job(
        client_id=payload.client_id,
        model_name=payload.model_name,
        full_vin=full_vin,
        last_6_vin=last_6,
        rego=payload.rego,
        delivery_date_time=payload.delivery_date_time,
        due_date=payload.due_date,
        description=f"Created from template: {template.get('name')}",
        priority=payload.priority or 'normal',
        is_urgent=bool(payload.is_urgent or payload.priority == 'urgent'),
        status='requested',
        assigned_contractor_id=payload.assigned_contractor_id,
        assigned_contractor_name=contractor_name,
        site_location=site,
        bay_location=payload.bay_location,
        checklist=tasks,
        template_id=template.get('id'),
        created_by_id=user.id,
        created_by_name=user.name
    )

    await db.contractor_jobs.insert_one(job_data.model_dump(mode='json'))

    await log_job_activity(
        db=db,
        job_id=job_data.id,
        activity_type='status_change',
        author_id=user.id,
        author_name=user.name,
        message=f"Job created from template '{template.get('name')}' with {len(tasks)} tasks"
    )

    if job_data.assigned_contractor_id:
        await notify_contractor_job_assigned(db, job_data.model_dump(mode='json'), job_data.assigned_contractor_id)
    await sync_job_to_delivery_client(db, job_data.id, user.name)
    return job_data


# ---------------------------------------------------------------------------
# 3. Time Tracking (Clock On / Clock Off)
# ---------------------------------------------------------------------------

@router.post('/jobs/{job_id}/clock-in', response_model=JobTimeEntry)
async def clock_in(
    job_id: str,
    payload: ClockInRequest = ClockInRequest(),
    user: User = Depends(require_contractor_or_admin)
):
    """One-click clock on per job or individual task."""
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    try:
        entry = await clock_in_contractor(
            db=db,
            job_id=job_id,
            contractor_id=user.id,
            contractor_name=user.name,
            task_id=payload.task_id,
            notes=payload.notes
        )
        return entry
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post('/jobs/{job_id}/clock-out', response_model=JobTimeEntry)
async def clock_out(
    job_id: str,
    payload: ClockOutRequest = ClockOutRequest(),
    user: User = Depends(require_contractor_or_admin)
):
    """One-click clock off per job."""
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    try:
        entry = await clock_out_contractor(
            db=db,
            job_id=job_id,
            contractor_id=user.id,
            contractor_name=user.name,
            notes=payload.notes
        )
        return entry
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get('/jobs/{job_id}/time-logs', response_model=List[JobTimeEntry])
async def list_job_time_logs(job_id: str, user: User = Depends(require_contractor_or_admin)):
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    if user.role == 'contractor' and job.get('assigned_contractor_id') != user.id:
        raise HTTPException(403, 'Permission denied')

    out = []
    async for doc in db.contractor_time_logs.find({'job_id': job_id}).sort('clock_in', -1):
        out.append(JobTimeEntry(**_strip(doc)))
    return out


@router.get('/time-logs')
async def list_all_time_logs(skip: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=5000), user: User = Depends(require_admin)):
    db = get_db()
    ids = [j['id'] async for j in db.contractor_jobs.find(job_scope(user))]
    return [JobTimeEntry(**_strip(doc)) async for doc in db.contractor_time_logs.find({'job_id': {'$in': ids}}).sort('clock_in', -1).skip(skip).limit(limit)]


@router.get('/time-logs/active')
async def get_active_clock_in(user: User = Depends(require_contractor_or_admin)):
    """Check if current user is currently clocked into any job."""
    db = get_db()
    doc = await db.contractor_time_logs.find_one({
        'contractor_id': user.id,
        'clock_out': None
    })
    if not doc:
        return {'is_clocked_in': False, 'entry': None}

    job_doc = await db.contractor_jobs.find_one({'id': doc.get('job_id')})
    assert_job_access(user, job_doc)
    return {
        'is_clocked_in': True,
        'entry': JobTimeEntry(**_strip(doc)),
        'job': Job(**_strip(job_doc)) if job_doc else None
    }


# ---------------------------------------------------------------------------
# 4. Evidence Capture (Before / During / After Photos & Videos)
# ---------------------------------------------------------------------------

@router.post('/jobs/{job_id}/evidence', response_model=JobEvidence)
async def upload_job_evidence(
    job_id: str,
    stage: str = Form('before'),  # 'before', 'during', 'after', 'issue'
    caption: Optional[str] = Form(None),
    file: UploadFile = File(...),
    client_action_id: Annotated[Optional[str], Form()] = None,
    captured_at: Annotated[Optional[str], Form()] = None,
    user: User = Depends(require_contractor_or_admin)
):
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    if stage not in ('before', 'during', 'after', 'issue'):
        raise HTTPException(400, 'Stage must be before, during, after, or issue')

    if client_action_id:
        previous = await db.contractor_evidence.find_one({'job_id': job_id, 'client_action_id': client_action_id, 'uploaded_by_id': user.id})
        if previous:
            from services.contractor_evidence import mirror_evidence
            evidence = JobEvidence(**_strip(previous))
            await mirror_evidence(db, job, evidence, user.name)
            await sync_job_to_delivery_client(db, job_id, user.name)
            return evidence
    import os
    max_size = int(os.environ.get('CONTRACTOR_MAX_MEDIA_BYTES', 104857600))
    file_bytes = await file.read(max_size + 1)
    if len(file_bytes) > max_size:
        raise HTTPException(413, 'Media exceeds the upload size limit')
    if not file_bytes:
        raise HTTPException(400, 'Uploaded file is empty')

    content_type = file.content_type or 'application/octet-stream'
    allowed_types = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/heic', 'image/heif', 'video/mp4', 'video/webm', 'video/quicktime', 'video/3gpp'}
    if content_type not in allowed_types:
        raise HTTPException(415, 'Upload a supported image or video')
    media_type = 'video' if content_type.startswith('video/') else 'image'

    from pathlib import PurePath
    fname = PurePath((file.filename or 'evidence.jpg').replace('\\', '/')).name
    fname = fname or f'evidence-{uuid.uuid4().hex[:8]}.jpg'
    evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, user.id + ':' + job_id + ':' + client_action_id)) if client_action_id else str(uuid.uuid4())
    storage_path = f"contractor-jobs/{job_id}/{evidence_id}-{fname}"

    await put_bytes(storage_path, file_bytes, content_type)

    evidence = JobEvidence(
        id=evidence_id, job_id=job_id,
        stage=stage,
        media_type=media_type,
        file_name=fname,
        storage_path=storage_path,
        content_type=content_type,
        size_bytes=len(file_bytes),
        caption=caption,
        uploaded_by_id=user.id,
        uploaded_by_name=user.name
    )

    from services.contractor_reporting import parse_date
    if captured_at:
        stamp = parse_date(captured_at)
        if not stamp or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise HTTPException(400, 'Invalid capture time')
        evidence.captured_at = stamp
    evidence_doc = evidence.model_dump(mode='json')
    evidence_doc['_id'] = evidence.id
    if client_action_id:
        evidence_doc['client_action_id'] = client_action_id
    await db.contractor_evidence.insert_one(evidence_doc)

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='comment',
        author_id=user.id,
        author_name=user.name,
        message=f"Uploaded {stage.upper()} evidence photo/video: {fname} ({caption or 'No caption'})",
        meta={'evidence_id': evidence.id, 'stage': stage}
    )

    from services.contractor_evidence import mirror_evidence
    await mirror_evidence(db, job, evidence, user.name)

    await sync_job_to_delivery_client(db, job_id, user.name)
    return evidence


@router.get('/jobs/{job_id}/evidence', response_model=List[JobEvidence])
async def list_job_evidence(job_id: str, user: User = Depends(require_contractor_or_admin)):
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    out = []
    async for doc in db.contractor_evidence.find({'job_id': job_id}).sort('captured_at', 1):
        out.append(JobEvidence(**_strip(doc)))
    return out


@router.get('/jobs/{job_id}/evidence/{evidence_id}/download')
async def download_job_evidence(
    job_id: str,
    evidence_id: str,
    user: User = Depends(require_contractor_or_admin)
):
    db = get_db()
    assert_job_access(user, await db.contractor_jobs.find_one({'id': job_id}))
    doc = await db.contractor_evidence.find_one({'id': evidence_id, 'job_id': job_id})
    if not doc or not doc.get('storage_path'):
        raise HTTPException(404, 'Evidence not found')

    if not await exists(doc['storage_path']):
        raise HTTPException(404, 'File missing in storage')

    data = await get_bytes(doc['storage_path'])
    return Response(
        content=data,
        media_type=doc.get('content_type') or 'application/octet-stream',
        headers={'Content-Disposition': f'inline; filename="{doc.get("file_name")}"', 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'}
    )


# ---------------------------------------------------------------------------
# 5. One-Click Location Registration (GPS Check-In & History)
# ---------------------------------------------------------------------------

@router.post('/jobs/{job_id}/location', response_model=JobLocationEntry)
async def check_in_location(
    job_id: str,
    payload: LocationCheckInRequest,
    user: User = Depends(require_contractor_or_admin)
):
    """GPS vehicle check-in and yard movement tracking."""
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    entry = await register_vehicle_location(
        db=db,
        job_id=job_id,
        user_id=user.id,
        user_name=user.name,
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy=payload.accuracy,
        location_name=payload.location_name,
        notes=payload.notes
    )
    return entry


@router.get('/jobs/{job_id}/location-history', response_model=List[JobLocationEntry])
async def get_location_history(job_id: str, user: User = Depends(require_contractor_or_admin)):
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    out = []
    async for doc in db.contractor_locations.find({'job_id': job_id}).sort('recorded_at', -1):
        out.append(JobLocationEntry(**_strip(doc)))
    return out


# ---------------------------------------------------------------------------
# 6. Communication, Activity Feed & Issue Escalation
# ---------------------------------------------------------------------------

@router.get('/jobs/{job_id}/activity', response_model=List[JobActivity])
async def get_job_activity(job_id: str, user: User = Depends(require_contractor_or_admin)):
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    out = []
    async for doc in db.contractor_activities.find({'job_id': job_id}).sort('created_at', -1):
        out.append(JobActivity(**_strip(doc)))
    return out


@router.post('/jobs/{job_id}/comments', response_model=JobActivity)
async def add_job_comment(
    job_id: str,
    payload: JobCommentCreate,
    user: User = Depends(require_contractor_or_admin)
):
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    activity = await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='comment',
        author_id=user.id,
        author_name=user.name,
        message=payload.message
    )
    return activity


@router.post('/jobs/{job_id}/flag-issue', response_model=Job)
async def flag_job_issue(
    job_id: str,
    payload: JobFlagIssue,
    user: User = Depends(require_contractor_or_admin)
):
    """
    Contractors flag an issue (e.g. paint chip, missing parts, tint bubble).
    Sets issue_flag=True, raises urgent flag, notifies Delivery Centre.
    """
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    now = datetime.now(timezone.utc)
    updated = await db.contractor_jobs.find_one_and_update(
        {'id': job_id},
        {
            '$set': {
                'issue_flag': True,
                'issue_description': payload.description,
                'issue_reported_by_id': user.id,
                'issue_reported_by_name': user.name,
                'issue_reported_at': now,
                'priority': 'urgent' if payload.is_urgent else job.get('priority', 'normal'),
                'is_urgent': True if payload.is_urgent else job.get('is_urgent', False),
                'updated_at': now
            }
        },
        return_document=True
    )

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='issue_flag',
        author_id=user.id,
        author_name=user.name,
        message=f"ISSUE FLAGGED: {payload.description}",
        meta={'photos': payload.photos, 'is_urgent': payload.is_urgent}
    )

    try:
        await notify_urgent_issue_flagged(db, updated, payload.description, user.name)
    except Exception as pe:
        log.warning("Could not dispatch push notification on issue flag: %s", pe)

    # If linked to Delivery Centre client, push an urgent alert comment to Delivery Team
    if job.get('client_id'):
        client_comment = {
            'id': uuid.uuid4().hex,
            'author_id': user.id,
            'author_name': f"Contractor Alert ({user.name})",
            'body': f"🚨 ISSUE FLAGGED on Contractor Job: {payload.description}",
            'created_at': now
        }
        await db.clients.update_one(
            {'id': job['client_id']},
            {'$push': {'comments': client_comment}}
        )

    await _safe_dispatch(db, 'job.issue_flagged', job_id, {
        'description': payload.description,
        'reported_by': user.name
    })

    await sync_job_to_delivery_client(db, job_id, user.name)
    return Job(**_strip(updated))


@router.post('/jobs/{job_id}/resolve-issue', response_model=Job)
async def resolve_job_issue(
    job_id: str,
    notes: Optional[str] = Form(None),
    user: User = Depends(require_admin)
):
    """Admin resolves a previously flagged issue."""
    db = get_db()
    job = await db.contractor_jobs.find_one({'id': job_id})
    assert_job_access(user, job)

    now = datetime.now(timezone.utc)
    updated = await db.contractor_jobs.find_one_and_update(
        {'id': job_id},
        {
            '$set': {
                'issue_flag': False,
                'updated_at': now
            }
        },
        return_document=True
    )

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='issue_resolved',
        author_id=user.id,
        author_name=user.name,
        message=f"Issue resolved by {user.name}. Resolution note: {notes or 'None'}"
    )

    await sync_job_to_delivery_client(db, job_id, user.name)
    return Job(**_strip(updated))


# ---------------------------------------------------------------------------
# 7. Dashboard, Reporting & Analytics
# ---------------------------------------------------------------------------

@router.get('/dashboard')
async def contractor_dashboard(user: User = Depends(require_contractor_or_admin)):
    from services.contractor_reporting import parse_date, MELBOURNE
    db = get_db()
    query = job_scope(user)
    jobs = [doc async for doc in db.contractor_jobs.find(query)]
    ids = [job['id'] for job in jobs]
    pipeline = {key: sum(j.get('status') == key for j in jobs) for key in ('requested', 'collected', 'in_progress', 'returned', 'completed', 'invoiced')}
    today = datetime.now(MELBOURNE).date()
    urgent = sum(bool(j.get('is_urgent')) for j in jobs)
    flagged = sum(bool(j.get('issue_flag')) for j in jobs)
    overdue = sum(1 for j in jobs if parse_date(j.get('due_date')) and parse_date(j['due_date']).astimezone(MELBOURNE).date() < today and j.get('status') not in ('completed', 'invoiced'))
    timed = [j.get('total_time_seconds', 0) for j in jobs if j.get('total_time_seconds', 0) > 0]
    activity = [_strip(doc) async for doc in db.contractor_activities.find({'job_id': {'$in': ids}}).sort('created_at', -1).limit(6)]
    workload = {}
    for job in jobs:
        name = job.get('assigned_contractor_name') or 'Unassigned'
        workload[name] = workload.get(name, 0) + (job.get('status') not in ('completed', 'invoiced'))
    oldest = sorted([j for j in jobs if j.get('status') not in ('completed', 'invoiced')], key=lambda j: parse_date(j.get('updated_at')) or datetime.now(timezone.utc))[:8]
    return {'role': user.role, 'total_jobs': len(jobs), 'pipeline': pipeline,
        'in_progress_jobs': pipeline['in_progress'], 'urgent_jobs': urgent, 'urgent_jobs_count': urgent,
        'flagged_jobs': flagged, 'flagged_issues_count': flagged, 'escalated_jobs': sum(1 for j in jobs if j.get('is_urgent') or j.get('issue_flag')), 'overdue_jobs': overdue, 'overdue_jobs_count': overdue,
        'completed_today': sum(1 for j in jobs if j.get('status') in ('completed', 'invoiced') and parse_date(j.get('completed_at')) and parse_date(j['completed_at']).astimezone(MELBOURNE).date() == today),
        'active_contractors': len({j['assigned_contractor_id'] for j in jobs if j.get('assigned_contractor_id') and j.get('status') not in ('completed', 'invoiced')}),
        'avg_time_per_job_seconds': sum(timed) / len(timed) if timed else 0,
        'active_clocked_in_count': await db.contractor_time_logs.count_documents({'job_id': {'$in': ids}, 'clock_out': None}),
        'recent_activity': activity, 'team_workload': workload,
        'personal_jobs': [Job(**_strip(j)) for j in jobs if j.get('assigned_contractor_id') == user.id and j.get('status') not in ('completed', 'invoiced')],
        'bottlenecks': [Job(**_strip(j)) for j in oldest]}


@router.get('/analytics')
async def contractor_analytics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    contractor_id: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    user: User = Depends(require_admin)
):
    """Reporting and analytics engine."""
    db = get_db()
    return await compute_contractor_analytics(
        db=db,
        date_from=date_from,
        date_to=date_to,
        contractor_id=contractor_id,
        model=model, status=status, site=effective_site(user)
    )


# ---------------------------------------------------------------------------
# 7. Reporting, Analytics & Multi-Format Exports (CSV / Excel / PDF)
# ---------------------------------------------------------------------------

@router.get('/export')
async def export_jobs(
    format: Optional[str] = Query('csv', pattern='^(csv|xlsx|excel|pdf)$'),
    contractor_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    user: User = Depends(require_admin)
):
    """
    Export jobs and time tracking records in CSV, Excel (.xlsx), or PDF format.
    Defaults to CSV for 100% backwards compatibility with existing clients.
    """
    db = get_db()
    fmt = (format or 'csv').lower()
    if fmt in ('xlsx', 'excel'):
        content = await generate_contractor_jobs_xlsx(db, contractor_id, date_from=date_from, date_to=date_to, model=model, status=status, site=effective_site(user))
        return Response(
            content=content,
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment; filename="contractor-jobs.xlsx"'}
        )
    elif fmt == 'pdf':
        content = await generate_contractor_jobs_pdf(db, contractor_id, date_from=date_from, date_to=date_to, model=model, status=status, site=effective_site(user))
        return Response(
            content=content,
            media_type='application/pdf',
            headers={'Content-Disposition': 'attachment; filename="contractor-jobs.pdf"'}
        )
    else:
        csv_data = await generate_contractor_jobs_csv(db, contractor_id, date_from=date_from, date_to=date_to, model=model, status=status, site=effective_site(user))
        return Response(
            content=csv_data,
            media_type='text/csv',
            headers={'Content-Disposition': 'attachment; filename="contractor-jobs.csv"'}
        )


@router.get('/export/xlsx')
async def export_jobs_xlsx(
    contractor_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    user: User = Depends(require_admin)
):
    """Export jobs and time tracking records as an Excel (.xlsx) workbook."""
    db = get_db()
    content = await generate_contractor_jobs_xlsx(db, contractor_id, date_from=date_from, date_to=date_to, model=model, status=status, site=effective_site(user))
    return Response(
        content=content,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="contractor-jobs.xlsx"'}
    )


@router.get('/export/pdf')
async def export_jobs_pdf(
    contractor_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    user: User = Depends(require_admin)
):
    """Export jobs and time tracking records as a formatted PDF report."""
    db = get_db()
    content = await generate_contractor_jobs_pdf(db, contractor_id, date_from=date_from, date_to=date_to, model=model, status=status, site=effective_site(user))
    return Response(
        content=content,
        media_type='application/pdf',
        headers={'Content-Disposition': 'attachment; filename="contractor-jobs.pdf"'}
    )


# ---------------------------------------------------------------------------
# 7B. Bulk Job Operations (Trello Migration & Ongoing)
# ---------------------------------------------------------------------------

@router.post('/jobs/bulk-import', response_model=BulkJobImportResponse)
async def bulk_import_jobs(
    payload: BulkJobImportRequest,
    user: User = Depends(require_admin)
):
    """
    Bulk job import for Trello migration and ongoing operations.
    Validates job records, expands templates, and triggers notifications.
    """
    db = get_db()
    for item in payload.jobs:
        item.site_location = effective_site(user, item.site_location) or 'Fairfield'
        await validate_assignment(db, user, item.assigned_contractor_id, item.site_location)
    return await bulk_import_contractor_jobs(
        db=db,
        user=user,
        jobs_data=payload.jobs,
        apply_templates=payload.apply_templates
    )


# ---------------------------------------------------------------------------
# 7C. Offline Queue Batch Synchronization
# ---------------------------------------------------------------------------

@router.post('/sync/offline-batch', response_model=OfflineBatchSyncResponse)
async def sync_offline_batch(
    payload: OfflineBatchSyncRequest,
    user: User = Depends(require_contractor_or_admin)
):
    """
    Processes a batch of offline-queued contractor actions (clock in/out,
    location check-in, comments, checklist updates, issue flags) idempotently
    preserving original queued timestamps.
    """
    db = get_db()
    return await process_offline_batch_sync(
        db=db,
        user=user,
        actions=payload.actions
    )


# ---------------------------------------------------------------------------
# 8. Two-Way Integration with deliverycentre.com.au & Health Monitoring
# ---------------------------------------------------------------------------

@router.post('/integrations/from-client/{client_id}', response_model=Job)
async def generate_job_from_delivery_client(
    client_id: str,
    payload: ConvertClientToJobRequest = ConvertClientToJobRequest(),
    user: User = Depends(require_admin)
):
    """
    Inbound integration: Extracts Client from Delivery Centre and produces a Contractor Job Card.
    """
    db = get_db()
    try:
        job = await create_job_from_delivery_client(
            db=db,
            client_id=client_id,
            tasks=payload.tasks,
            assigned_contractor_id=payload.assigned_contractor_id,
            due_date=payload.due_date,
            priority=payload.priority or 'normal',
            is_urgent=payload.is_urgent or False,
            notes=payload.notes,
            created_by_user=user
        )
        return job
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post('/integrations/sync-to-client/{job_id}')
async def trigger_sync_to_client(
    job_id: str,
    user: User = Depends(require_admin)
):
    """
    Outbound integration trigger: Synchronizes Contractor Job status & completed tasks to Client.
    """
    db = get_db()
    assert_job_access(user, await db.contractor_jobs.find_one({'id': job_id}))
    await db.contractor_outbox.update_many({'job_id': job_id, 'status': 'retry'}, {'$set': {'next_attempt_at': datetime.now(timezone.utc)}})
    res = await sync_job_to_delivery_client(db, job_id, user.name)
    if not res:
        raise HTTPException(404, 'Job not found or not linked to any Delivery Centre client')
    return {'success': True, 'sync_log': res}


@router.get('/integrations/sync-status')
async def get_sync_status(user: User = Depends(require_admin)):
    """
    Sync dashboard showing integration health, API uptime status, error rates, data volume, and sync history.
    Spec §6: API uptime, error rates, data volume.
    """
    db = get_db()
    scope = {}
    if effective_site(user):
        ids = [j['id'] async for j in db.contractor_jobs.find(job_scope(user))]
        scope = {'job_id': {'$in': ids}}
    total_syncs = await db.contractor_sync_logs.count_documents(scope)
    success_syncs = await db.contractor_sync_logs.count_documents({**scope, 'status': 'success'})
    failed_syncs = await db.contractor_sync_logs.count_documents({**scope, 'status': 'failed'})

    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)
    seven_days_ago = now - timedelta(days=7)

    volume_24h = await db.contractor_sync_logs.count_documents({**scope, 'synced_at': {'$gte': one_day_ago}})
    volume_7d = await db.contractor_sync_logs.count_documents({**scope, 'synced_at': {'$gte': seven_days_ago}})

    error_rate = round((failed_syncs / total_syncs * 100), 2) if total_syncs > 0 else 0.0

    recent_logs = []
    last_sync_at = None
    async for doc in db.contractor_sync_logs.find(scope).sort('synced_at', -1).limit(20):
        if last_sync_at is None:
            last_sync_at = doc.get('synced_at')
        recent_logs.append(_strip(doc))

    if failed_syncs == 0:
        health_status = 'Healthy'
        uptime_status = 'Operational'
    elif error_rate < 15.0:
        health_status = 'Degraded'
        uptime_status = 'Partial Outage / Elevated Errors'
    else:
        health_status = 'Critical'
        uptime_status = 'Degraded Service'

    config = await integration_config(db)
    probe = await db.settings.find_one({'key': 'contractor_integration_health'}) or {}
    return {
        'outbound_configured': bool(config['outbound_webhook_url']),
        'last_health_check': probe.get('checked_at'),
        'remote_reachable': probe.get('reachable'),
        'pending_outbound': await db.contractor_outbox.count_documents({**scope, 'status': {'$in': ['pending', 'retry', 'sending']}}),
        'total_sync_events': total_syncs,
        'success_count': success_syncs,
        'failed_count': failed_syncs,
        'error_rate_pct': error_rate,
        'data_volume_24h': volume_24h,
        'data_volume_7d': volume_7d,
        'last_sync_at': last_sync_at,
        'health': health_status,
        'uptime_status': ('Shared database' if not config['api_url'] else ('Operational' if probe.get('reachable') else 'Unreachable' if probe.get('reachable') is False else 'Not checked')),
        'recent_logs': recent_logs
    }


@router.get('/integrations/requests')
async def integration_requests(request: Request, skip: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    db = get_db()
    await authenticate_webhook(request, db)
    docs = [doc async for doc in db.clients.find({'stage': {'$nin': ['Delivered', 'Cancelled']}}).sort('id', 1).skip(skip).limit(limit + 1)]
    requests = []
    for client in docs[:limit]:
        vin = client.get('vin')
        if not vin:
            continue
        requests.append({'client_id': client['id'], 'dc_job_id': client['id'], 'model_name': client.get('vehicle') or 'BYD Vehicle',
            'full_vin': vin, 'last_6_vin': vin[-6:], 'delivery_date_time': client.get('delivery_date'), 'due_date': client.get('delivery_date'),
            'description': client.get('notes'), 'priority': 'urgent' if client.get('alert') else 'normal', 'is_urgent': bool(client.get('alert')),
            'site_location': client.get('site_location') or 'Fairfield',
            'checklist': [JobTask(title='Fit ' + a['name']).model_dump(mode='json') for a in client.get('accessories', []) if a.get('name')]})
    return {'requests': requests, 'next_skip': skip + limit if len(docs) > limit else None}


@router.post('/integrations/webhook')
async def receive_external_webhook(request: Request):
    from services.contractor_integration import receive_event
    return await receive_event(get_db(), request)


@router.post('/integrations/poll')
async def trigger_manual_integration_poll(user: User = Depends(require_admin)):
    """
    Manually triggers the background 5-15 min scheduled polling sync from Delivery Centre.
    """
    db = get_db()
    await db.audit.insert_one({'id': uuid.uuid4().hex, 'actor_id': user.id, 'action': 'contractor.integration.manual_poll', 'created_at': datetime.now(timezone.utc)})
    res = await poll_delivery_centre_integration(db, site=effective_site(user))
    return res


@router.get('/integrations/config')
async def get_integration_config(user: User = Depends(require_super)):
    return await integration_config(get_db(), public=True)


@router.post('/integrations/config')
async def save_integration_config(request: Request, user: User = Depends(require_super)):
    return await save_config(get_db(), await request.json(), user)


# ---------------------------------------------------------------------------
# 9. Push Notifications (VAPID / Web Push)
# ---------------------------------------------------------------------------

@router.get('/push/vapid-public-key')
async def get_push_public_key(_: User = Depends(require_contractor_or_admin)):
    """Returns VAPID public key for frontend PWA push subscription."""
    db = get_db()
    public_key = await get_vapid_public_key(db)
    return {'publicKey': public_key}


@router.post('/push/subscribe')
async def subscribe_push_notifications(
    payload: PushSubscriptionPayload,
    user: User = Depends(require_contractor_or_admin)
):
    """Register or update a browser push subscription for the logged-in user."""
    db = get_db()
    sub_dict = payload.model_dump()
    await save_push_subscription(
        db=db,
        user_id=user.id,
        role=user.role,
        subscription_dict=sub_dict,
        device_info=payload.device_info
    )
    return {'success': True, 'message': 'Push subscription registered successfully'}


@router.delete('/push/subscribe')
async def unsubscribe_push_notifications(
    endpoint: Optional[str] = None,
    user: User = Depends(require_contractor_or_admin)
):
    """Unregister push subscription(s) for the logged-in user."""
    db = get_db()
    deleted = await remove_push_subscription(db, user_id=user.id, endpoint=endpoint)
    return {'success': True, 'unsubscribed_count': deleted}


@router.post('/push/send-test')
async def send_test_push(
    payload: SendPushTestRequest,
    user: User = Depends(require_contractor_or_admin)
):
    """Sends a test push notification to verify device receipt."""
    db = get_db()
    count = await send_web_push(
        db=db,
        user_id=user.id,
        title=payload.title,
        body=payload.body,
        data=payload.data
    )
    return {'sent_count': count, 'recipient': user.name}

