from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import os
import uuid
import logging
import asyncio
import io
import csv
import httpx
import inspect

from contractor_models import (
    Job, JobCreate, JobUpdate, JobTask, JobTaskCreate, JobStatus,
    JobTimeEntry, JobLocationEntry, JobEvidence, JobActivity,
    IntegrationSyncLog,
    OfflineActionItem, OfflineActionResult, OfflineBatchSyncResponse,
    BulkJobImportResultItem, BulkJobImportResponse
)

log = logging.getLogger('contractor_service')

CONTRACTOR_WEBHOOK_URL = os.environ.get(
    'CONTRACTOR_WEBHOOK_URL',
    os.environ.get('CONTRACTOR_APP_URL', 'https://byd-contractor-app.vercel.app') + '/api/webhooks/contractor'
)


async def _safe_call(coro_or_val):
    if inspect.isawaitable(coro_or_val):
        return await coro_or_val
    return coro_or_val


def _strip(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if d:
        d.pop('_id', None)
    return d


# ---------------------------------------------------------------------------
# Activity & Audit Logging
# ---------------------------------------------------------------------------
async def log_job_activity(
    db,
    job_id: str,
    activity_type: str,
    author_id: str,
    author_name: str,
    message: str,
    meta: Optional[dict] = None,
    created_at: Optional[datetime] = None
):
    activity = JobActivity(
        job_id=job_id,
        activity_type=activity_type,
        author_id=author_id,
        author_name=author_name,
        message=message,
        meta=meta or {},
        created_at=created_at or datetime.now(timezone.utc)
    )
    await db.contractor_activities.insert_one(activity.model_dump(mode='json'))
    return activity


# ---------------------------------------------------------------------------
# Webhook Dispatcher
# ---------------------------------------------------------------------------
async def _send_contractor_webhook_async(payload: dict):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(CONTRACTOR_WEBHOOK_URL, json=payload)
            if resp.status_code >= 400:
                log.warning("Contractor webhook returned %s: %s", resp.status_code, resp.text)
            else:
                log.info("Contractor webhook dispatched successfully: event=%s", payload.get('event'))
    except Exception as exc:
        log.debug("Contractor webhook dispatch failed (non-blocking): %s", exc)


def dispatch_contractor_webhook(event: str, job_id: str, payload_data: dict):
    webhook_payload = {
        "event_id": f"EVT-CONTR-{uuid.uuid4().hex[:12].upper()}",
        "event": event,
        "job_id": job_id,
        "payload": payload_data,
        "dispatched_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_contractor_webhook_async(webhook_payload))
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Two-Way Delivery Centre Integration
# ---------------------------------------------------------------------------
async def sync_job_to_delivery_client(db, job_id: str, actor_name: str = 'System'):
    """
    Syncs contractor job status & completed tasks back into Delivery Centre Client.
    """
    job_doc = await _safe_call(db.contractor_jobs.find_one({'id': job_id}))
    if not job_doc:
        return None

    client_id = job_doc.get('client_id')
    now = datetime.now(timezone.utc)

    # If no client_id or missing, attempt lookup by VIN
    client = None
    if client_id and hasattr(db, 'clients'):
        client = await _safe_call(db.clients.find_one({'id': client_id}))
    if not client and hasattr(db, 'clients') and job_doc.get('full_vin'):
        client = await _safe_call(db.clients.find_one({
            '$or': [
                {'vin': job_doc.get('full_vin')},
                {'vin': {'$regex': job_doc.get('last_6_vin', ''), '$options': 'i'}}
            ]
        }))
        if client:
            client_id = client['id']
            if hasattr(db, 'contractor_jobs'):
                await _safe_call(db.contractor_jobs.update_one(
                    {'id': job_id},
                    {'$set': {'client_id': client_id, 'dc_job_id': client_id}}
                ))

    if not client:
        if hasattr(db, 'contractor_jobs'):
            await _safe_call(db.contractor_jobs.update_one(
                {'id': job_id},
                {
                    '$set': {
                        'sync_status': 'failed',
                        'sync_error': 'No matching Delivery Centre client found',
                        'last_synced_at': now
                    }
                }
            ))
        return None

    completed_tasks = [
        t.get('title', '').strip().lower()
        for t in job_doc.get('checklist', [])
        if t.get('completed')
    ]

    # Check and update matching accessories on the client
    accessories = client.get('accessories', [])
    updated_accessories = []
    accessories_changed = False

    for acc in accessories:
        acc_name = (acc.get('name') or '').strip().lower()
        # If task matches accessory name, mark fitted
        if any(task in acc_name or acc_name in task for task in completed_tasks):
            if acc.get('status') != 'Fitted':
                acc['status'] = 'Fitted'
                acc['updated_at'] = datetime.now(timezone.utc).isoformat()
                accessories_changed = True
        updated_accessories.append(acc)

    client_updates: Dict[str, Any] = {'updated_at': now}
    if accessories_changed:
        client_updates['accessories'] = updated_accessories

    # If contractor job registered vehicle bay location, mirror to client location note
    if job_doc.get('bay_location'):
        client_updates['location'] = f"Fairfield - Bay {job_doc['bay_location']}"

    # If job status completed or returned, leave an audit note on client comments
    job_status = job_doc.get('status')
    if job_status in ('completed', 'returned'):
        comment_body = (
            f"Contractor Job {job_id} ({job_doc.get('model_name')}) status is '{job_status}'. "
            f"Total contractor time: {job_doc.get('total_time_seconds', 0) // 60} minutes."
        )
        new_comment = {
            'id': uuid.uuid4().hex,
            'author_id': 'system-contractor-sync',
            'author_name': f'Contractor App ({actor_name})',
            'body': comment_body,
            'created_at': now
        }
        if hasattr(db, 'clients'):
            await _safe_call(db.clients.update_one(
                {'id': client_id},
                {
                    '$set': client_updates,
                    '$push': {'comments': new_comment}
                }
            ))
    elif accessories_changed or 'location' in client_updates:
        if hasattr(db, 'clients'):
            await _safe_call(db.clients.update_one(
                {'id': client_id},
                {'$set': client_updates}
            ))

    # Persist sync success state on contractor job
    if hasattr(db, 'contractor_jobs'):
        await _safe_call(db.contractor_jobs.update_one(
            {'id': job_id},
            {
                '$set': {
                    'sync_status': 'synced',
                    'sync_error': None,
                    'last_synced_at': now,
                    'dc_job_id': client_id,
                    'client_id': client_id
                }
            }
        ))

    # Log sync event
    sync_log = IntegrationSyncLog(
        job_id=job_id,
        client_id=client_id,
        direction='outbound_to_delivery',
        event='job.synced_to_client',
        status='success',
        details={
            'job_status': job_status,
            'completed_tasks': completed_tasks,
            'accessories_updated': accessories_changed
        },
        synced_at=now
    )
    if hasattr(db, 'contractor_sync_logs'):
        await _safe_call(db.contractor_sync_logs.insert_one(sync_log.model_dump(mode='json')))
    return sync_log


async def create_job_from_delivery_client(
    db,
    client_id: str,
    tasks: Optional[List[str]] = None,
    assigned_contractor_id: Optional[str] = None,
    due_date: Optional[str] = None,
    priority: str = 'normal',
    is_urgent: bool = False,
    notes: Optional[str] = None,
    created_by_user=None
) -> Job:
    """
    Inbound integration: Extracts Client from Delivery Centre and produces a Contractor Job Card.
    """
    client = await db.clients.find_one({'id': client_id})
    if not client:
        raise ValueError('Client not found')

    # Checklist generation
    checklist_items: List[JobTask] = []
    if tasks:
        for t_title in tasks:
            checklist_items.append(JobTask(title=t_title.strip()))
    else:
        # Extract from client's accessories & aftermarket notes
        for acc in client.get('accessories', []):
            name = acc.get('name')
            if name:
                checklist_items.append(JobTask(title=f"Fit {name}", notes=acc.get('note')))
        if not checklist_items:
            # Standard delivery prep tasks
            checklist_items = [
                JobTask(title='Tint 2 Front Windows'),
                JobTask(title='Exterior Clean & Detail'),
                JobTask(title='Fit Accessories & Floor Mats'),
            ]

    full_vin = client.get('vin') or f"VIN-TEMP-{client.get('id', '')[:8].upper()}"
    clean_vin = full_vin.strip().upper()
    last_6 = clean_vin[-6:] if len(clean_vin) >= 6 else clean_vin

    # Contractor name lookup
    contractor_name = None
    if assigned_contractor_id:
        user_doc = await db.users.find_one({'id': assigned_contractor_id})
        if user_doc:
            contractor_name = user_doc.get('name')

    new_job = Job(
        client_id=client_id,
        model_name=client.get('vehicle') or 'BYD Vehicle',
        full_vin=full_vin,
        last_6_vin=last_6,
        rego=client.get('rego'),
        delivery_date_time=client.get('delivery_date'),
        due_date=due_date or client.get('delivery_date'),
        description=notes or client.get('notes') or f"Job generated for client {client.get('name')}",
        priority='urgent' if is_urgent or priority == 'urgent' else priority,
        is_urgent=bool(is_urgent or priority == 'urgent'),
        status='requested',
        assigned_contractor_id=assigned_contractor_id,
        assigned_contractor_name=contractor_name,
        site_location=client.get('site_location') or 'Fairfield',
        checklist=checklist_items,
        sync_status='synced',
        dc_job_id=client_id,
        last_synced_at=datetime.now(timezone.utc),
        created_by_id=created_by_user.id if created_by_user else 'system',
        created_by_name=created_by_user.name if created_by_user else 'Delivery Centre'
    )

    await db.contractor_jobs.insert_one(new_job.model_dump(mode='json'))

    # Log initial activity
    await log_job_activity(
        db=db,
        job_id=new_job.id,
        activity_type='integration_sync',
        author_id=created_by_user.id if created_by_user else 'system',
        author_name=created_by_user.name if created_by_user else 'Delivery Centre',
        message=f"Job created from Delivery Centre Client '{client.get('name')}' (VIN: {new_job.full_vin})"
    )

    # Sync log record
    sync_log = IntegrationSyncLog(
        job_id=new_job.id,
        client_id=client_id,
        direction='inbound_to_contractor',
        event='job.created_from_client',
        status='success',
        details={'model': new_job.model_name, 'vin': new_job.full_vin}
    )
    await db.contractor_sync_logs.insert_one(sync_log.model_dump(mode='json'))

    dispatch_contractor_webhook('job.created', new_job.id, new_job.model_dump(mode='json'))

    return new_job


async def sync_client_updates_to_contractor_jobs(
    db,
    client_id: str,
    updates: dict,
    actor_name: str = 'Delivery Centre'
):
    """
    Inbound integration (Scope §6): When a client's due date, priority, VIN, rego, or notes change in Delivery Centre,
    propagate those updates in real-time to any linked contractor job.
    """
    job_cursor = db.contractor_jobs.find({'client_id': client_id})
    job_updates = {}
    now = datetime.now(timezone.utc)

    if 'delivery_date' in updates and updates['delivery_date']:
        job_updates['delivery_date_time'] = updates['delivery_date']
        job_updates['due_date'] = updates['delivery_date']
    if 'vin' in updates and updates['vin']:
        clean_vin = updates['vin'].strip().upper()
        job_updates['full_vin'] = clean_vin
        job_updates['last_6_vin'] = clean_vin[-6:] if len(clean_vin) >= 6 else clean_vin
    if 'rego' in updates and updates['rego']:
        job_updates['rego'] = updates['rego']
    if 'vehicle' in updates and updates['vehicle']:
        job_updates['model_name'] = updates['vehicle']
    if 'alert' in updates and updates['alert']:
        job_updates['priority'] = 'urgent'
        job_updates['is_urgent'] = True

    if not job_updates:
        return

    job_updates['updated_at'] = now
    job_updates['sync_status'] = 'synced'
    job_updates['last_synced_at'] = now

    async for job in job_cursor:
        await db.contractor_jobs.update_one({'id': job['id']}, {'$set': job_updates})
        await log_job_activity(
            db=db,
            job_id=job['id'],
            activity_type='integration_sync',
            author_id='system',
            author_name=f'Delivery Centre ({actor_name})',
            message=f"Real-time update from Delivery Centre: {', '.join(job_updates.keys())}",
            meta=job_updates,
            created_at=now
        )
        dispatch_contractor_webhook('job.updated', job['id'], {**job, **job_updates})


# ---------------------------------------------------------------------------
# Time Tracking Engine
# ---------------------------------------------------------------------------
async def clock_in_contractor(
    db,
    job_id: str,
    contractor_id: str,
    contractor_name: str,
    task_id: Optional[str] = None,
    notes: Optional[str] = None,
    clock_in_time: Optional[datetime] = None,
    client_action_id: Optional[str] = None
) -> JobTimeEntry:
    # If client_action_id given (e.g., offline queue sync), check for idempotent duplicate
    if client_action_id:
        existing = await db.contractor_time_logs.find_one({'client_action_id': client_action_id})
        if existing:
            return JobTimeEntry(**_strip(existing))

    # Verify no active un-clocked-out entry for this contractor & job
    active_entry = await db.contractor_time_logs.find_one({
        'job_id': job_id,
        'contractor_id': contractor_id,
        'clock_out': None
    })
    if active_entry:
        if client_action_id:
            return JobTimeEntry(**_strip(active_entry))
        raise ValueError('Contractor is already clocked on for this job')

    now = clock_in_time or datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace('Z', '+00:00'))
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    entry = JobTimeEntry(
        job_id=job_id,
        task_id=task_id,
        contractor_id=contractor_id,
        contractor_name=contractor_name,
        clock_in=now,
        notes=notes
    )
    doc = entry.model_dump(mode='json')
    if client_action_id:
        doc['client_action_id'] = client_action_id
    await db.contractor_time_logs.insert_one(doc)

    # Auto-move job to in_progress if still requested or collected
    job = await db.contractor_jobs.find_one({'id': job_id})
    if job and job.get('status') in ('requested', 'collected'):
        await db.contractor_jobs.update_one(
            {'id': job_id},
            {
                '$set': {
                    'status': 'in_progress',
                    'in_progress_at': now,
                    'updated_at': now
                }
            }
        )

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='time_log',
        author_id=contractor_id,
        author_name=contractor_name,
        message=f"Clocked ON ({notes or 'Shift started'})",
        meta={'time_entry_id': entry.id, 'task_id': task_id, 'client_action_id': client_action_id},
        created_at=now
    )

    return entry


async def clock_out_contractor(
    db,
    job_id: str,
    contractor_id: str,
    contractor_name: str,
    notes: Optional[str] = None,
    clock_out_time: Optional[datetime] = None
) -> JobTimeEntry:
    # Find active time log
    active_entry = await db.contractor_time_logs.find_one({
        'job_id': job_id,
        'contractor_id': contractor_id,
        'clock_out': None
    })
    if not active_entry:
        raise ValueError('No active clock-in found for this job')

    now = clock_out_time or datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace('Z', '+00:00'))
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    clock_in_dt = active_entry['clock_in']
    if isinstance(clock_in_dt, str):
        clock_in_dt = datetime.fromisoformat(clock_in_dt.replace('Z', '+00:00'))
    elif clock_in_dt.tzinfo is None:
        clock_in_dt = clock_in_dt.replace(tzinfo=timezone.utc)

    duration = max(0, int((now - clock_in_dt).total_seconds()))

    await db.contractor_time_logs.update_one(
        {'id': active_entry['id']},
        {
            '$set': {
                'clock_out': now,
                'duration_seconds': duration,
                'notes': notes or active_entry.get('notes')
            }
        }
    )

    # Accumulate into job's total_time_seconds
    await db.contractor_jobs.update_one(
        {'id': job_id},
        {
            '$inc': {'total_time_seconds': duration},
            '$set': {'updated_at': now}
        }
    )

    mins = duration // 60
    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='time_log',
        author_id=contractor_id,
        author_name=contractor_name,
        message=f"Clocked OFF. Duration: {mins}m ({duration}s). Notes: {notes or 'Shift ended'}",
        meta={'time_entry_id': active_entry['id'], 'duration_seconds': duration},
        created_at=now
    )

    # Sync updated time tracking data to delivery centre client
    await sync_job_to_delivery_client(db, job_id, contractor_name)

    updated_entry = await db.contractor_time_logs.find_one({'id': active_entry['id']})
    return JobTimeEntry(**_strip(updated_entry))


# ---------------------------------------------------------------------------
# Location & Evidence
# ---------------------------------------------------------------------------
async def register_vehicle_location(
    db,
    job_id: str,
    user_id: str,
    user_name: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy: Optional[float] = None,
    location_name: Optional[str] = None,
    notes: Optional[str] = None,
    recorded_at: Optional[datetime] = None
) -> JobLocationEntry:
    rec_dt = recorded_at or datetime.now(timezone.utc)
    if isinstance(rec_dt, str):
        rec_dt = datetime.fromisoformat(rec_dt.replace('Z', '+00:00'))
    elif rec_dt.tzinfo is None:
        rec_dt = rec_dt.replace(tzinfo=timezone.utc)

    entry = JobLocationEntry(
        job_id=job_id,
        latitude=latitude,
        longitude=longitude,
        accuracy=accuracy,
        location_name=location_name,
        notes=notes,
        recorded_at=rec_dt,
        recorded_by_id=user_id,
        recorded_by_name=user_name
    )
    await db.contractor_locations.insert_one(entry.model_dump(mode='json'))

    # Update job's bay_location or location string
    job_update: Dict[str, Any] = {'updated_at': rec_dt}
    if location_name:
        job_update['bay_location'] = location_name

    await db.contractor_jobs.update_one({'id': job_id}, {'$set': job_update})

    # Sync back to delivery centre if linked
    await sync_job_to_delivery_client(db, job_id, user_name)

    await log_job_activity(
        db=db,
        job_id=job_id,
        activity_type='location_update',
        author_id=user_id,
        author_name=user_name,
        message=f"Vehicle location check-in: {location_name or 'GPS coordinates registered'}",
        meta={'location_id': entry.id, 'lat': latitude, 'lng': longitude, 'name': location_name},
        created_at=rec_dt
    )

    return entry


# ---------------------------------------------------------------------------
# Analytics, Reporting & CSV Export
# ---------------------------------------------------------------------------
async def compute_contractor_analytics(
    db,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    contractor_id: Optional[str] = None,
    model: Optional[str] = None
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if contractor_id:
        query['assigned_contractor_id'] = contractor_id
    if model:
        query['model_name'] = {'$regex': model, '$options': 'i'}
    if date_from or date_to:
        created_range = {}
        if date_from:
            dt_from = datetime.fromisoformat(date_from)
            if dt_from.tzinfo is None:
                dt_from = dt_from.replace(tzinfo=timezone.utc)
            created_range['$gte'] = dt_from
        if date_to:
            dt_to = datetime.fromisoformat(date_to)
            if len(date_to) <= 10:
                dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)
            if dt_to.tzinfo is None:
                dt_to = dt_to.replace(tzinfo=timezone.utc)
            created_range['$lte'] = dt_to
        query['created_at'] = created_range

    total_jobs = await db.contractor_jobs.count_documents(query)
    completed_query = {**query, 'status': 'completed'}
    completed_jobs = await db.contractor_jobs.count_documents(completed_query)

    # Average time calculation
    total_time_seconds = 0
    jobs_with_time = 0
    today_iso = datetime.now(timezone.utc).date().isoformat()
    overdue_count = 0

    jobs_by_status: Dict[str, int] = {}
    jobs_by_model: Dict[str, int] = {}
    jobs_per_contractor: Dict[str, int] = {}
    completed_per_vin: List[Dict[str, Any]] = []

    async for j in db.contractor_jobs.find(query):
        st = j.get('status', 'requested')
        jobs_by_status[st] = jobs_by_status.get(st, 0) + 1

        mdl = j.get('model_name') or 'Other'
        jobs_by_model[mdl] = jobs_by_model.get(mdl, 0) + 1

        c_name = j.get('assigned_contractor_name') or 'Unassigned'
        jobs_per_contractor[c_name] = jobs_per_contractor.get(c_name, 0) + 1

        # Turnaround time
        t_sec = j.get('total_time_seconds', 0)
        if t_sec > 0:
            total_time_seconds += t_sec
            jobs_with_time += 1

        # Overdue check
        due = j.get('due_date')
        if due and due < today_iso and st not in ('completed', 'invoiced'):
            overdue_count += 1

        if st in ('completed', 'invoiced'):
            completed_per_vin.append({
                'job_id': j.get('id'),
                'vin': j.get('full_vin'),
                'last_6_vin': j.get('last_6_vin'),
                'model': j.get('model_name'),
                'contractor': j.get('assigned_contractor_name'),
                'completed_at': j.get('completed_at'),
                'total_minutes': round(j.get('total_time_seconds', 0) / 60, 1)
            })

    avg_job_minutes = round((total_time_seconds / jobs_with_time) / 60, 1) if jobs_with_time > 0 else 0
    completion_rate = round((completed_jobs / total_jobs * 100), 1) if total_jobs > 0 else 0.0

    return {
        'total_jobs': total_jobs,
        'completed_jobs': completed_jobs,
        'completion_rate_percent': completion_rate,
        'overdue_jobs_count': overdue_count,
        'average_time_minutes': avg_job_minutes,
        'jobs_by_status': jobs_by_status,
        'jobs_by_model': jobs_by_model,
        'jobs_by_contractor': jobs_per_contractor,
        'completed_per_vin': completed_per_vin
    }


async def generate_contractor_jobs_csv(db, contractor_id: Optional[str] = None) -> str:
    query: Dict[str, Any] = {}
    if contractor_id:
        query['assigned_contractor_id'] = contractor_id

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Job ID',
        'Model Name',
        'Full VIN',
        'Last 6 VIN',
        'Rego',
        'Status',
        'Priority',
        'Due Date',
        'Delivery Date',
        'Assigned Contractor',
        'Total Time (Minutes)',
        'Checklist Total Tasks',
        'Checklist Completed Tasks',
        'Issue Flagged',
        'Issue Description',
        'Created At'
    ])

    async for j in db.contractor_jobs.find(query).sort('created_at', -1):
        checklist = j.get('checklist', [])
        total_tasks = len(checklist)
        done_tasks = len([t for t in checklist if t.get('completed')])
        total_mins = round(j.get('total_time_seconds', 0) / 60, 1)

        writer.writerow([
            j.get('id', ''),
            j.get('model_name', ''),
            j.get('full_vin', ''),
            j.get('last_6_vin', ''),
            j.get('rego', ''),
            j.get('status', ''),
            j.get('priority', ''),
            j.get('due_date', ''),
            j.get('delivery_date_time', ''),
            j.get('assigned_contractor_name', ''),
            total_mins,
            total_tasks,
            done_tasks,
            'YES' if j.get('issue_flag') else 'NO',
            j.get('issue_description', '') or '',
            j.get('created_at', '')
        ])

    return output.getvalue()


async def generate_contractor_jobs_xlsx(db, contractor_id: Optional[str] = None) -> bytes:
    """Generate Excel (.xlsx) spreadsheet of contractor jobs."""
    query: Dict[str, Any] = {}
    if contractor_id:
        query['assigned_contractor_id'] = contractor_id

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Contractor Jobs"

    # Title Banner
    ws.merge_cells('A1:Q1')
    title_cell = ws['A1']
    title_cell.value = "BYD FAIRFIELD — CONTRACTOR JOB MANAGEMENT REPORT"
    title_cell.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    # Headers
    headers = [
        'Job ID', 'Model Name', 'Full VIN', 'Last 6 VIN', 'Rego',
        'Status', 'Priority', 'Due Date', 'Delivery Date',
        'Assigned Contractor', 'Total Time (Mins)',
        'Checklist Total Tasks', 'Checklist Completed Tasks',
        'Issue Flagged', 'Issue Description', 'Bay Location', 'Created At'
    ]
    ws.append(headers)
    ws.row_dimensions[2].height = 24

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    # Rows
    row_idx = 3
    alt_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    regular_font = Font(name="Calibri", size=10)

    async for j in db.contractor_jobs.find(query).sort('created_at', -1):
        checklist = j.get('checklist', [])
        total_tasks = len(checklist)
        done_tasks = len([t for t in checklist if t.get('completed')])
        total_mins = round(j.get('total_time_seconds', 0) / 60, 1)

        row_data = [
            j.get('id', ''),
            j.get('model_name', ''),
            j.get('full_vin', ''),
            j.get('last_6_vin', ''),
            j.get('rego', ''),
            j.get('status', ''),
            j.get('priority', ''),
            j.get('due_date', ''),
            j.get('delivery_date_time', ''),
            j.get('assigned_contractor_name', ''),
            total_mins,
            total_tasks,
            done_tasks,
            'YES' if j.get('issue_flag') else 'NO',
            j.get('issue_description', '') or '',
            j.get('bay_location', '') or '',
            str(j.get('created_at', ''))
        ]
        ws.append(row_data)
        ws.row_dimensions[row_idx].height = 20

        is_even = (row_idx % 2 == 0)
        for col_idx in range(1, len(row_data) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = regular_font
            cell.border = thin_border
            if is_even:
                cell.fill = alt_fill
            if col_idx in (4, 5, 6, 7, 8, 9, 11, 12, 13, 14):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(vertical="center")

        row_idx += 1

    # Adjust column widths
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val_str = str(cell.value or '')
            if '\n' in val_str:
                val_str = max(val_str.split('\n'), key=len)
            max_len = max(max_len, len(val_str))
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def generate_contractor_jobs_pdf(db, contractor_id: Optional[str] = None) -> bytes:
    """Generate PDF report of contractor jobs."""
    query: Dict[str, Any] = {}
    contractor_name_filter = "All Contractors"
    if contractor_id:
        query['assigned_contractor_id'] = contractor_id
        contractor_doc = await db.users.find_one({'id': contractor_id})
        if contractor_doc:
            contractor_name_filter = contractor_doc.get('name', contractor_id)

    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'RepTitle',
        parent=styles['Heading1'],
        fontSize=15,
        leading=18,
        textColor=colors.HexColor('#0F172A'),
        spaceAfter=3
    )
    subtitle_style = ParagraphStyle(
        'RepSub',
        parent=styles['Normal'],
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#64748B'),
        spaceAfter=10
    )
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#1E293B')
    )
    cell_bold_style = ParagraphStyle(
        'TableCellBold',
        parent=cell_style,
        fontName='Helvetica-Bold'
    )
    header_cell_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
        textColor=colors.white,
        fontName='Helvetica-Bold'
    )

    jobs = []
    total_time_all = 0
    issues_count = 0
    completed_count = 0

    async for j in db.contractor_jobs.find(query).sort('created_at', -1):
        jobs.append(j)
        total_time_all += j.get('total_time_seconds', 0)
        if j.get('issue_flag'):
            issues_count += 1
        if j.get('status') == 'completed':
            completed_count += 1

    gen_time = datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')
    story = [
        Paragraph("BYD Fairfield — Contractor Job Management Report", title_style),
        Paragraph(
            f"Generated: {gen_time} | Scope: {contractor_name_filter} | "
            f"Total Jobs: {len(jobs)} | Completed: {completed_count} | "
            f"Flagged Issues: {issues_count} | Total Hours: {round(total_time_all / 3600, 1)}h",
            subtitle_style
        ),
        Spacer(1, 3 * mm)
    ]

    table_data = [
        [
            Paragraph("Model / VIN", header_cell_style),
            Paragraph("Rego", header_cell_style),
            Paragraph("Status", header_cell_style),
            Paragraph("Priority", header_cell_style),
            Paragraph("Contractor", header_cell_style),
            Paragraph("Due Date", header_cell_style),
            Paragraph("Tasks", header_cell_style),
            Paragraph("Time (m)", header_cell_style),
            Paragraph("Location", header_cell_style),
            Paragraph("Issue / Notes", header_cell_style)
        ]
    ]

    for j in jobs:
        checklist = j.get('checklist', [])
        total_t = len(checklist)
        done_t = len([t for t in checklist if t.get('completed')])
        tasks_progress = f"{done_t}/{total_t}" if total_t > 0 else "-"
        mins = round(j.get('total_time_seconds', 0) / 60, 1)

        vin_short = j.get('last_6_vin') or (j.get('full_vin', '')[-6:] if j.get('full_vin') else '')
        model_vin = f"<b>{j.get('model_name', '')}</b><br/>...{vin_short}"

        issue_desc = ""
        if j.get('issue_flag'):
            issue_desc = f"<font color='#DC2626'><b>ISSUE:</b> {j.get('issue_description') or 'Flagged'}</font>"
        elif j.get('description'):
            issue_desc = j.get('description', '')[:50]

        table_data.append([
            Paragraph(model_vin, cell_style),
            Paragraph(j.get('rego', '') or '-', cell_style),
            Paragraph(j.get('status', '').upper(), cell_bold_style),
            Paragraph(j.get('priority', '').upper(), cell_style),
            Paragraph(j.get('assigned_contractor_name', '') or 'Unassigned', cell_style),
            Paragraph(j.get('due_date', '') or '-', cell_style),
            Paragraph(tasks_progress, cell_style),
            Paragraph(str(mins), cell_style),
            Paragraph(j.get('bay_location', '') or j.get('site_location', '') or '-', cell_style),
            Paragraph(issue_desc or '-', cell_style)
        ])

    col_widths = [45*mm, 20*mm, 25*mm, 20*mm, 35*mm, 25*mm, 18*mm, 20*mm, 25*mm, 44*mm]

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
    ]))

    story.append(t)
    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Offline Batch Synchronization
# ---------------------------------------------------------------------------
async def process_offline_batch_sync(
    db,
    user,
    actions: List[OfflineActionItem]
) -> OfflineBatchSyncResponse:
    """
    Applies offline queued actions (clock_in, clock_out, location_checkin,
    add_comment, flag_issue, update_task) idempotently preserving original queued_at timestamps.
    """
    results: List[OfflineActionResult] = []
    succeeded = 0
    failed = 0

    for action in actions:
        action_id = action.client_action_id
        act_type = action.action_type
        job_id = action.job_id
        payload = action.payload or {}
        queued_at = action.queued_at or datetime.now(timezone.utc)

        try:
            job = await db.contractor_jobs.find_one({'id': job_id})
            if not job:
                raise ValueError(f"Job '{job_id}' not found")

            if act_type == 'clock_in':
                await clock_in_contractor(
                    db=db,
                    job_id=job_id,
                    contractor_id=user.id,
                    contractor_name=user.name,
                    task_id=payload.get('task_id'),
                    notes=payload.get('notes'),
                    clock_in_time=queued_at,
                    client_action_id=action_id
                )
            elif act_type == 'clock_out':
                await clock_out_contractor(
                    db=db,
                    job_id=job_id,
                    contractor_id=user.id,
                    contractor_name=user.name,
                    notes=payload.get('notes'),
                    clock_out_time=queued_at
                )
            elif act_type == 'location_checkin':
                await register_vehicle_location(
                    db=db,
                    job_id=job_id,
                    user_id=user.id,
                    user_name=user.name,
                    latitude=payload.get('latitude'),
                    longitude=payload.get('longitude'),
                    accuracy=payload.get('accuracy'),
                    location_name=payload.get('location_name'),
                    notes=payload.get('notes'),
                    recorded_at=queued_at
                )
            elif act_type == 'add_comment':
                msg = payload.get('message') or payload.get('comment') or 'Offline note'
                await log_job_activity(
                    db=db,
                    job_id=job_id,
                    activity_type='comment',
                    author_id=user.id,
                    author_name=user.name,
                    message=msg,
                    created_at=queued_at
                )
            elif act_type == 'update_task':
                task_id = payload.get('task_id')
                if not task_id:
                    raise ValueError("task_id is required for update_task")
                completed = payload.get('completed', True)
                notes = payload.get('notes')

                update_fields: Dict[str, Any] = {
                    'checklist.$.completed': completed,
                    'checklist.$.completed_at': queued_at if completed else None,
                    'checklist.$.completed_by_id': user.id if completed else None,
                    'checklist.$.completed_by_name': user.name if completed else None,
                    'updated_at': queued_at
                }
                if notes is not None:
                    update_fields['checklist.$.notes'] = notes

                res = await db.contractor_jobs.update_one(
                    {'id': job_id, 'checklist.id': task_id},
                    {'$set': update_fields}
                )
                if res.matched_count == 0:
                    raise ValueError(f"Task '{task_id}' not found in job '{job_id}'")

                await log_job_activity(
                    db=db,
                    job_id=job_id,
                    activity_type='checklist_update',
                    author_id=user.id,
                    author_name=user.name,
                    message=f"Checklist task updated ({'Completed' if completed else 'Unchecked'})",
                    meta={'task_id': task_id, 'completed': completed},
                    created_at=queued_at
                )
            elif act_type == 'flag_issue':
                desc = payload.get('description') or 'Issue reported offline'
                is_urg = payload.get('is_urgent', True)
                await db.contractor_jobs.update_one(
                    {'id': job_id},
                    {
                        '$set': {
                            'issue_flag': True,
                            'issue_description': desc,
                            'issue_reported_by_id': user.id,
                            'issue_reported_by_name': user.name,
                            'issue_reported_at': queued_at,
                            'priority': 'urgent' if is_urg else job.get('priority', 'normal'),
                            'is_urgent': is_urg or job.get('is_urgent', False),
                            'updated_at': queued_at
                        }
                    }
                )
                await log_job_activity(
                    db=db,
                    job_id=job_id,
                    activity_type='issue_flag',
                    author_id=user.id,
                    author_name=user.name,
                    message=f"ISSUE FLAGGED: {desc}",
                    meta={'is_urgent': is_urg},
                    created_at=queued_at
                )
                try:
                    from services.push_service import notify_urgent_issue_flagged
                    await notify_urgent_issue_flagged(db, job, desc, user.name)
                except Exception:
                    pass
            else:
                raise ValueError(f"Unknown action_type: {act_type}")

            succeeded += 1
            results.append(OfflineActionResult(
                client_action_id=action_id,
                success=True,
                message=f"Action '{act_type}' applied"
            ))
        except Exception as e:
            failed += 1
            results.append(OfflineActionResult(
                client_action_id=action_id,
                success=False,
                message=str(e)
            ))

    # Log sync event in contractor_sync_logs
    sync_log = IntegrationSyncLog(
        direction='inbound_to_contractor',
        event='offline_batch_sync',
        status='success' if failed == 0 else ('failed' if succeeded == 0 else 'pending'),
        details={'total': len(actions), 'succeeded': succeeded, 'failed': failed, 'user_id': user.id}
    )
    await db.contractor_sync_logs.insert_one(sync_log.model_dump(mode='json'))

    return OfflineBatchSyncResponse(
        processed=len(actions),
        succeeded=succeeded,
        failed=failed,
        results=results
    )


# ---------------------------------------------------------------------------
# Bulk Job Import (Trello Migration & Operations)
# ---------------------------------------------------------------------------
async def bulk_import_contractor_jobs(
    db,
    user,
    jobs_data: List[JobCreate],
    apply_templates: bool = True
) -> BulkJobImportResponse:
    """
    Imports multiple jobs in bulk (e.g. from Trello migration or batch requests).
    Applies templates, resolves contractor names, and triggers notifications.
    """
    imported = 0
    failed = 0
    created_ids: List[str] = []
    results: List[BulkJobImportResultItem] = []

    templates_cache: Dict[str, dict] = {}
    users_cache: Dict[str, str] = {}

    for idx, item in enumerate(jobs_data):
        try:
            # Resolve template if needed
            checklist_tasks: List[JobTask] = []
            if item.checklist:
                checklist_tasks = [
                    JobTask(title=t.title, notes=t.notes)
                    for t in item.checklist
                ]
            elif apply_templates and item.template_id:
                if item.template_id not in templates_cache:
                    tmpl = await db.contractor_templates.find_one({'id': item.template_id})
                    if tmpl:
                        templates_cache[item.template_id] = tmpl
                tmpl = templates_cache.get(item.template_id)
                if tmpl:
                    checklist_tasks = [
                        JobTask(title=t) for t in tmpl.get('tasks', [])
                    ]

            # Resolve contractor name
            contractor_name = None
            if item.assigned_contractor_id:
                cid = item.assigned_contractor_id
                if cid not in users_cache:
                    u_doc = await db.users.find_one({'id': cid})
                    if u_doc:
                        users_cache[cid] = u_doc.get('name', 'Contractor')
                contractor_name = users_cache.get(cid)

            now = datetime.now(timezone.utc)
            new_job = Job(
                client_id=item.client_id,
                model_name=item.model_name,
                full_vin=item.full_vin,
                last_6_vin=item.last_6_vin,
                rego=item.rego,
                delivery_date_time=item.delivery_date_time,
                due_date=item.due_date,
                description=item.description,
                priority=item.priority or 'normal',
                is_urgent=item.is_urgent or False,
                status=item.status or 'requested',
                assigned_contractor_id=item.assigned_contractor_id,
                assigned_contractor_name=contractor_name,
                site_location=item.site_location or 'Fairfield',
                bay_location=item.bay_location,
                checklist=checklist_tasks,
                template_id=item.template_id,
                created_by_id=user.id if user else 'system',
                created_by_name=user.name if user else 'Admin',
                created_at=now,
                updated_at=now
            )

            await db.contractor_jobs.insert_one(new_job.model_dump(mode='json'))

            await log_job_activity(
                db=db,
                job_id=new_job.id,
                activity_type='status_change',
                author_id=user.id if user else 'system',
                author_name=user.name if user else 'Admin',
                message=f"Job created via bulk import for {new_job.model_name} (VIN: {new_job.full_vin})"
            )

            # If assigned, send push
            if new_job.assigned_contractor_id:
                try:
                    from services.push_service import notify_contractor_job_assigned
                    await notify_contractor_job_assigned(db, new_job.model_dump(mode='json'), new_job.assigned_contractor_id)
                except Exception:
                    pass

            imported += 1
            created_ids.append(new_job.id)
            results.append(BulkJobImportResultItem(
                index=idx,
                full_vin=new_job.full_vin,
                success=True,
                job_id=new_job.id
            ))
        except Exception as e:
            failed += 1
            results.append(BulkJobImportResultItem(
                index=idx,
                full_vin=getattr(item, 'full_vin', None),
                success=False,
                error=str(e)
            ))

    return BulkJobImportResponse(
        total_submitted=len(jobs_data),
        imported_count=imported,
        failed_count=failed,
        created_job_ids=created_ids,
        results=results
    )

