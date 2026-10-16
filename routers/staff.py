"""Assignment directory. Memberships describe staffing, never access permissions."""
import re
from typing import Optional
from fastapi import APIRouter, Depends
from auth import get_current_user, effective_site
from db import get_db
from models import User

router = APIRouter(prefix='/api/staff', tags=['staff'])


def site_regex(site: str):
    name = re.sub(r'\s+VIC$', '', site.strip(), flags=re.I)
    name = re.sub(r'^BYD\s+', '', name, flags=re.I)
    names = ['Melbourne', 'Melbourne City'] if name.lower() in ('melbourne', 'melbourne city') else [name]
    return re.compile(r'^(?:BYD\s+)?(?:' + '|'.join(re.escape(n) for n in names) + r')(?:\s+VIC)?$', re.I)


@router.get('')
async def list_staff(site: Optional[str] = None, user: User = Depends(get_current_user)):
    db = get_db()
    site = effective_site(user, site)
    query = {'active': {'$ne': False}, 'role': {'$ne': 'contractor'}}
    if site and site.lower() not in ('all', 'all sites', 'all locations'):
        regex = site_regex(site)
        memberships = await db.staff_site_memberships.find({'sites': regex}).to_list(length=None)
        query['$or'] = [{field: regex} for field in ('site', 'active_site', 'locked_site')]
        if memberships:
            query['$or'].append({'email': {'$in': [re.compile(r'^\s*' + re.escape(m['email']) + r'\s*$', re.I) for m in memberships]}})
    projection = {field: 1 for field in ('id', 'name', 'email', 'role', 'site', 'active_site', 'active')}
    rows = await db.users.find(query, projection).sort('name', 1).to_list(length=None)
    return [{**{k: v for k, v in row.items() if k != '_id'}, 'id': row.get('id') or str(row['_id'])} for row in rows]
