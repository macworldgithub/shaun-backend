import asyncio
import os
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

os.environ.setdefault('JWT_SECRET', 'account-access-test-secret-at-least-32-characters')
import auth
from models import UserInDB
from services.account_access import resolve_site


def account(**changes):
    return UserInDB(**{'id': 'staff-id', 'email': 'staff@example.com', 'name': 'Staff', 'role': 'sales_consultant', 'password_hash': 'unused',
                      'active': True, 'auth_version': 1, 'platform_access': ['delivery_centre'], 'allowed_sites': ['BYD Fairfield', 'BYD Nunawading'], 'all_sites': False, 'active_site': 'BYD Fairfield', **changes})


def authenticate(monkeypatch, user, site=None):
    async def load(_):
        return user
    monkeypatch.setattr(auth, '_load_user', load)
    token = auth.create_token('staff-id', 'super_admin', extra={'auth_version': 1})
    request = Request({'type': 'http', 'headers': [(b'x-site', site.encode())] if site else [], 'query_string': b''})
    return asyncio.run(auth.get_current_user(request, HTTPAuthorizationCredentials(scheme='Bearer', credentials=token)))


def test_assigned_site_selection(monkeypatch):
    result = authenticate(monkeypatch, account(), 'Nunawading')
    assert auth.effective_site(result) == 'BYD Nunawading'
    assert 'request_site' not in result.model_dump()
    with pytest.raises(HTTPException) as error:
        authenticate(monkeypatch, account(), 'Denza Melbourne')
    assert error.value.status_code == 403


@pytest.mark.parametrize('changes', [{'active': False}, {'auth_version': 2}, {'deleted_at': '2026-10-27T10:00:00Z'}])
def test_sessions_are_revoked(monkeypatch, changes):
    with pytest.raises(HTTPException) as error:
        authenticate(monkeypatch, account(**changes))
    assert error.value.status_code == 401


def test_platform_access_is_enforced(monkeypatch):
    with pytest.raises(HTTPException) as error:
        authenticate(monkeypatch, account(platform_access=['crm']))
    assert error.value.status_code == 403


def test_all_sites_does_not_allow_substring_aliases():
    assert resolve_site(account(), 'All Sites') == 'BYD Fairfield'
    with pytest.raises(HTTPException):
        resolve_site(account(allowed_sites=['Denza Melbourne']), 'BYD Melbourne City')


def test_delivery_user_management_is_reserved_for_the_named_owner():
    owner = account(email='devs@omnisuiteai.com', role='super_admin')
    assert asyncio.run(auth.require_user_manager(owner)) == owner
    for user in [account(role='super_admin'), account(email='devs@omnisuiteai.com')]:
        with pytest.raises(HTTPException) as error:
            asyncio.run(auth.require_user_manager(user))
        assert error.value.status_code == 403
