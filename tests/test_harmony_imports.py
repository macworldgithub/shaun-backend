"""Backend regression tests for the Harmony Auto admin import endpoint and
basic regression smoke for existing flows.

Covers:
  - Auth gating (401, 403)
  - File validation (.txt, empty, malformed xlsx)
  - dry_run + replace=true (does not mutate DB)
  - replace=false + dry_run=true (no wipe)
  - dry_run=false + replace=true (real commit + audit row)
  - Smoke: /api/clients list, /api/admin/users, /api/share-links
"""
import io
import os
import time
import zipfile
import requests
import pytest

BASE_URL = os.environ.get(
    'REACT_APP_BACKEND_URL', 'https://handover-central.preview.emergentagent.com'
).rstrip('/')

SUPER_EMAIL = os.environ.get('TEST_SUPER_EMAIL', 'shaun@omnisuiteai.com')
SUPER_PASSWORD = os.environ.get('TEST_SUPER_PASSWORD', '12344321')
HARMONY_XLSX = os.environ.get('TEST_HARMONY_XLSX', '/tmp/harmony2.xlsx')


# ---------- fixtures ----------
@pytest.fixture(scope='session')
def super_token():
    r = requests.post(f'{BASE_URL}/api/auth/login',
                      json={'email': SUPER_EMAIL, 'password': SUPER_PASSWORD}, timeout=30)
    assert r.status_code == 200, f'super login failed: {r.status_code} {r.text}'
    return r.json()['access_token']


@pytest.fixture(scope='session')
def super_headers(super_token):
    return {'Authorization': f'Bearer {super_token}'}


@pytest.fixture(scope='session')
def regular_agent(super_headers):
    """Create a non-super agent (or reuse) and return (email, password, token)."""
    email = 'TEST_agent_imports@example.com'
    password = os.environ.get('TEST_AGENT_PASSWORD', 'AgentPass123!')
    payload = {'email': email, 'name': 'TEST Agent Imports', 'role': 'agent', 'password': password}
    r = requests.post(f'{BASE_URL}/api/admin/users', json=payload, headers=super_headers, timeout=30)
    if r.status_code == 409:
        # already exists – recreate with a unique timestamped email so we can log in deterministically
        email = f'TEST_agent_imports_{int(time.time())}@example.com'
        payload['email'] = email
        r = requests.post(f'{BASE_URL}/api/admin/users', json=payload, headers=super_headers, timeout=30)
    assert r.status_code in (200, 201), f'create agent failed: {r.status_code} {r.text}'
    login = requests.post(f'{BASE_URL}/api/auth/login',
                          json={'email': email, 'password': password}, timeout=30)
    assert login.status_code == 200, f'agent login failed: {login.text}'
    return email, password, login.json()['access_token']


def _stats(headers):
    r = requests.get(f'{BASE_URL}/api/admin/stats', headers=headers, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- auth gating ----------
class TestAuthGating:
    def test_imports_requires_auth(self):
        with open(HARMONY_XLSX, 'rb') as f:
            r = requests.post(
                f'{BASE_URL}/api/admin/imports/harmony',
                files={'file': ('harmony2.xlsx', f, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                data={'replace': 'true', 'dry_run': 'true'},
                timeout=60,
            )
        assert r.status_code == 401, f'expected 401, got {r.status_code} {r.text}'

    def test_imports_forbidden_for_non_super(self, regular_agent):
        _, _, token = regular_agent
        with open(HARMONY_XLSX, 'rb') as f:
            r = requests.post(
                f'{BASE_URL}/api/admin/imports/harmony',
                files={'file': ('harmony2.xlsx', f, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                data={'replace': 'true', 'dry_run': 'true'},
                headers={'Authorization': f'Bearer {token}'},
                timeout=60,
            )
        assert r.status_code == 403, f'expected 403, got {r.status_code} {r.text}'


# ---------- validation ----------
class TestValidation:
    def test_rejects_txt_file(self, super_headers):
        r = requests.post(
            f'{BASE_URL}/api/admin/imports/harmony',
            files={'file': ('hello.txt', b'not an xlsx', 'text/plain')},
            data={'replace': 'true', 'dry_run': 'true'},
            headers=super_headers, timeout=30,
        )
        assert r.status_code == 400
        assert 'xlsx' in r.json().get('detail', '').lower()

    def test_rejects_empty_file(self, super_headers):
        r = requests.post(
            f'{BASE_URL}/api/admin/imports/harmony',
            files={'file': ('empty.xlsx', b'', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
            data={'replace': 'true', 'dry_run': 'true'},
            headers=super_headers, timeout=30,
        )
        assert r.status_code == 400
        assert 'empty' in r.json().get('detail', '').lower()

    def test_rejects_malformed_xlsx(self, super_headers):
        # Non-zip bytes with .xlsx extension – openpyxl will raise
        r = requests.post(
            f'{BASE_URL}/api/admin/imports/harmony',
            files={'file': ('bad.xlsx', b'this is definitely not a zip',
                           'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
            data={'replace': 'true', 'dry_run': 'true'},
            headers=super_headers, timeout=30,
        )
        assert r.status_code == 400
        assert 'parse' in r.json().get('detail', '').lower()


# ---------- dry-run / commit ----------
class TestImportFlows:
    def test_dry_run_replace_true_does_not_mutate(self, super_headers):
        before = _stats(super_headers)['total_clients']
        with open(HARMONY_XLSX, 'rb') as f:
            r = requests.post(
                f'{BASE_URL}/api/admin/imports/harmony',
                files={'file': ('harmony2.xlsx', f,
                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                data={'replace': 'true', 'dry_run': 'true'},
                headers=super_headers, timeout=120,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        s = body['summary']
        assert s['dry_run'] is True
        assert s['inserted'] > 0
        assert isinstance(s['by_stage'], dict) and len(s['by_stage']) > 0
        # DB unchanged
        after = _stats(super_headers)['total_clients']
        assert after == before, f'DB count changed during dry_run: {before} -> {after}'
        # store preview inserted count for the next test
        TestImportFlows.preview_inserted = s['inserted']

    def test_replace_false_dry_run_no_wipe(self, super_headers):
        with open(HARMONY_XLSX, 'rb') as f:
            r = requests.post(
                f'{BASE_URL}/api/admin/imports/harmony',
                files={'file': ('harmony2.xlsx', f,
                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                data={'replace': 'false', 'dry_run': 'true'},
                headers=super_headers, timeout=120,
            )
        assert r.status_code == 200, r.text
        s = r.json()['summary']
        assert s['dry_run'] is True
        assert s['replace'] is False
        assert s['wiped'] == 0

    def test_commit_replace_true_persists(self, super_headers):
        with open(HARMONY_XLSX, 'rb') as f:
            r = requests.post(
                f'{BASE_URL}/api/admin/imports/harmony',
                files={'file': ('harmony2.xlsx', f,
                               'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')},
                data={'replace': 'true', 'dry_run': 'false'},
                headers=super_headers, timeout=180,
            )
        assert r.status_code == 200, r.text
        s = r.json()['summary']
        assert s['dry_run'] is False
        assert s['inserted'] > 0
        # Should match the dry-run preview count
        if hasattr(TestImportFlows, 'preview_inserted'):
            assert s['inserted'] == TestImportFlows.preview_inserted, (
                f"commit inserted ({s['inserted']}) != preview inserted ({TestImportFlows.preview_inserted})"
            )
        # Verify total_clients reflects the commit (>= inserted, manual rows preserved)
        total = _stats(super_headers)['total_clients']
        assert total >= s['inserted'], f"total_clients ({total}) < inserted ({s['inserted']})"

        # Audit row exists
        audit = requests.get(f'{BASE_URL}/api/admin/audit', headers=super_headers, timeout=30).json()
        actions = [a.get('action') for a in audit[:50]]
        assert 'imports.harmony.run' in actions, f'audit missing imports.harmony.run; recent: {actions[:10]}'


# ---------- smoke regression ----------
class TestSmoke:
    def test_clients_list(self, super_headers):
        r = requests.get(f'{BASE_URL}/api/clients', headers=super_headers, timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_admin_users_list(self, super_headers):
        r = requests.get(f'{BASE_URL}/api/admin/users', headers=super_headers, timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert any(u['email'] == SUPER_EMAIL for u in r.json())

    def test_share_links_list(self, super_headers):
        r = requests.get(f'{BASE_URL}/api/share-links', headers=super_headers, timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
