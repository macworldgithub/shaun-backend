import re
from fastapi import HTTPException


def site_key(value):
    value = re.sub(r'^byd\s+', '', str(value or '').strip().lower())
    value = re.sub(r'\s+vic$', '', value)
    value = re.sub(r'[^a-z0-9]', '', value)
    return 'melbournecity' if value == 'melbourne' else value


def has_platform(user, platform):
    access = getattr(user, 'platform_access', None)
    return access is None or platform in access


def resolve_site(user, requested=None):
    sites = getattr(user, 'allowed_sites', None)
    if sites is None or getattr(user, 'all_sites', False):
        return getattr(user, 'locked_site', None)
    if not sites:
        raise HTTPException(403, 'No sites are assigned to this account')
    if not requested or requested.lower() in ('all', 'all sites', 'all yards', 'all locations'):
        home = getattr(user, 'active_site', None)
        return next((s for s in sites if site_key(s) == site_key(home)), sites[0])
    match = next((s for s in sites if site_key(s) == site_key(requested)), None)
    if not match:
        raise HTTPException(403, 'You do not have access to this site')
    return match
