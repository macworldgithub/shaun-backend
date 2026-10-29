from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import re

from fastapi import HTTPException

MELBOURNE = ZoneInfo('Australia/Melbourne')
STATUSES = ('requested', 'collected', 'in_progress', 'returned', 'completed', 'invoiced')


def parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def report_query(contractor_id=None, date_from=None, date_to=None, model=None, status=None, site=None):
    query = {}
    if contractor_id:
        query['assigned_contractor_id'] = contractor_id
    if site:
        from services.contractor_access import site_filter
        query['site_location'] = site_filter(site)
    if model:
        query['model_name'] = {'$regex': re.escape(model), '$options': 'i'}
    if status:
        if status not in STATUSES:
            raise HTTPException(400, 'Invalid job status')
        query['status'] = status
    comparisons = []
    for name, value, op in [('From', date_from, '$gte'), ('To', date_to, '$lte')]:
        if not value:
            continue
        dt = parse_date(value)
        if dt is None:
            raise HTTPException(400, name + ' date must be ISO format')
        if name == 'To' and len(str(value)) == 10:
            dt = dt + timedelta(days=1) - timedelta(microseconds=1)
        comparisons.append({op: [{'$convert': {'input': '$created_at', 'to': 'date', 'onError': None, 'onNull': None}}, dt]})
    if date_from and date_to and parse_date(date_from) > parse_date(date_to):
        raise HTTPException(400, 'From date must be before To date')
    if comparisons:
        query['$expr'] = {'$and': comparisons}
    return query


async def analytics(db, **filters):
    query = report_query(**filters)
    jobs = [j async for j in db.contractor_jobs.find(query)]
    today = datetime.now(MELBOURNE).date()
    counts = defaultdict(int)
    models = defaultdict(int)
    workers = defaultdict(int)
    task_durations = defaultdict(list)
    weeks, months, trends = {}, {}, {}
    completed = []
    overdue = 0
    for job in jobs:
        status = job.get('status', 'requested')
        counts[status] += 1
        model = job.get('model_name') or 'Other'
        worker = job.get('assigned_contractor_name') or 'Unassigned'
        models[model] += 1
        workers[worker] += 1
        due = parse_date(job.get('due_date'))
        if due and due.astimezone(MELBOURNE).date() < today and status not in ('completed', 'invoiced'):
            overdue += 1
        created = parse_date(job.get('created_at'))
        if created:
            local = created.astimezone(MELBOURNE).date()
            week = (local - timedelta(days=local.weekday())).isoformat()
            for bucket, period in ((weeks, week), (months, local.strftime('%Y-%m'))):
                key = (period, worker, model, status)
                bucket[key] = bucket.get(key, 0) + 1
        if status in ('completed', 'invoiced'):
            completed.append({'job_id': job.get('id'), 'vin': job.get('full_vin'),
                'last_6_vin': job.get('last_6_vin'), 'model': model, 'contractor': worker,
                'completed_at': job.get('completed_at'), 'total_minutes': round(job.get('total_time_seconds', 0) / 60, 1)})
            finished = parse_date(job.get('completed_at'))
            if finished:
                day = finished.astimezone(MELBOURNE).date().isoformat()
                trends[day] = trends.get(day, 0) + 1
        for task in job.get('checklist', []):
            if task.get('duration_seconds', 0) > 0:
                task_durations[task.get('title', 'Task')].append(task['duration_seconds'])
    timed = [j.get('total_time_seconds', 0) for j in jobs if j.get('total_time_seconds', 0) > 0]
    rate = round(100 * len(completed) / len(jobs), 1) if jobs else 0
    avg = round(sum(timed) / len(timed), 1) if timed else 0
    def periods(bucket):
        return [{'period': k[0], 'contractor': k[1], 'model': k[2], 'status': k[3], 'jobs': v} for k, v in sorted(bucket.items())]
    return {'total_jobs': len(jobs), 'completed_jobs': len(completed),
        'completion_rate': rate, 'completion_rate_percent': rate,
        'avg_time_per_job_seconds': avg, 'average_time_minutes': round(avg / 60, 1),
        'total_time_seconds': sum(timed), 'overdue_jobs': overdue, 'overdue_jobs_count': overdue,
        'jobs_by_status': dict(counts), 'jobs_by_model': dict(models), 'jobs_by_contractor': dict(workers),
        'completed_per_vin': completed,
        'average_time_per_task': [{'task_title': name, 'avg_minutes': round(sum(values) / len(values) / 60, 1), 'total_completed': len(values)} for name, values in sorted(task_durations.items())],
        'jobs_per_week': periods(weeks), 'jobs_per_month': periods(months),
        'completion_trends': [{'date': day, 'completed': value} for day, value in sorted(trends.items())]}
