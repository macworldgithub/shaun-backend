from fastapi import HTTPException

from auth import assert_site_access, effective_site
from services.account_access import site_key
import re


def site_filter(site):
    """Match the same site aliases as authorization, preserving the BYD/Denza boundary."""
    text = re.sub(r'^BYD\s+', '', str(site).strip(), flags=re.I)
    text = re.sub(r'\s+VIC$', '', text, flags=re.I)
    names = [text]
    if site_key(site) == 'melbournecity':
        names = ['Melbourne', 'Melbourne City']
    pattern = '|'.join(re.escape(name).replace(r'\ ', r'\s+') for name in names)
    return {'$regex': r'^(?:BYD\s+)?(?:' + pattern + r')(?:\s+VIC)?$', '$options': 'i'}


def job_scope(user, requested_site=None):
    query = {}
    site = effective_site(user, requested_site)
    if site:
        query['site_location'] = site_filter(site)
    if user.role == 'contractor':
        query['assigned_contractor_id'] = user.id
    return query


def assert_job_access(user, job):
    if not job:
        raise HTTPException(404, 'Job not found')
    if user.role not in ('contractor', 'admin', 'super_admin', 'site_admin'):
        raise HTTPException(403, 'Contractor access required')
    assert_site_access(user, job)
    if user.role == 'contractor' and job.get('assigned_contractor_id') != user.id:
        raise HTTPException(403, 'You can only access your assigned jobs')


async def validate_assignment(db, actor, contractor_id, site):
    if not contractor_id:
        return None
    target = await db.users.find_one({'id': contractor_id, 'active': True})
    if not target or target.get('deleted_at') or target.get('role') not in ('contractor', 'admin', 'super_admin', 'site_admin'):
        raise HTTPException(400, 'Select an active contractor or admin')
    from models import User
    from services.account_access import resolve_site
    assignee = User(**target)
    if assignee.locked_site and site_key(assignee.locked_site) != site_key(site):
        raise HTTPException(400, 'Assignee cannot access this site')
    resolve_site(assignee, site)
    return target.get('name')
