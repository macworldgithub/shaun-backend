"""Read-only endpoint timings from the application server; never prints tokens or customer data."""
import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_db, close
from auth import create_token


async def account():
    db = get_db()
    users = await db.users.find({'active': True, 'deleted_at': None,
                                'role': {'$in': ['super_admin', 'admin']}}).to_list(100)
    users = [user for user in users if user.get('id') and not user.get('locked_site')
             and (user.get('allowed_sites') is None or user.get('all_sites'))
             and (user.get('platform_access') is None or 'delivery_centre' in user['platform_access'])]
    if not users:
        raise RuntimeError('No unrestricted delivery management account available for read-only profiling')
    row = await db.clients.find_one({'site_location': {'$nin': [None, '', 'All Sites']}},
                                    {'site_location': 1, 'id': 1})
    user = users[0]
    token = create_token(user['id'], user['role'], extra={'auth_version': user.get('auth_version', 0)}, expires_in_hours=1)
    close()
    return token, row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:5020')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--scope', choices=['site', 'all'], default='site')
    parser.add_argument('--output')
    parser.add_argument('--only', help='Comma-separated endpoint names to measure')
    args = parser.parse_args()
    token, row = asyncio.run(account())
    site = row['site_location'] if args.scope == 'site' else 'All Sites'
    query = urllib.parse.urlencode({'site_location': site})
    alt = urllib.parse.urlencode({'site': site})
    headers = {'Authorization': 'Bearer ' + token, 'X-Site': site, 'Accept-Encoding': 'gzip'}
    endpoints = [
        ('auth', '/api/auth/me'), ('dashboard', '/api/admin/stats?' + query),
        ('clients_summary', '/api/clients?summary=true&' + query),
        ('clients_page', '/api/clients/page?limit=20&' + query),
        ('calendar', '/api/clients?summary=true&calendar=true&date_from=2026-11-01&date_to=2026-11-30&' + query),
        ('client_detail', '/api/clients/' + row['id']),
        ('alerts', '/api/clients/alerts'), ('staff', '/api/staff?' + alt),
        ('deals', '/api/virtualyard/deals?limit=25&' + alt),
        ('messages', '/api/messages?limit=200&' + query),
    ]
    results = []
    for name, path in endpoints:
        if args.only and name not in args.only.split(','):
            continue
        durations, codes, sizes = [], [], []
        for _ in range(args.samples):
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(urllib.request.Request(args.base + path, headers=headers), timeout=20) as response:
                    content = response.read()
                    codes.append(response.status)
                    sizes.append(len(content))
            except urllib.error.HTTPError as error:
                codes.append(error.code)
                sizes.append(len(error.read()))
            except (TimeoutError, urllib.error.URLError):
                codes.append('timeout')
                sizes.append(0)
            durations.append(round((time.perf_counter() - start) * 1000, 1))
        result = {'endpoint': name, 'scope': args.scope, 'ms': durations,
                  'median_ms': round(statistics.median(durations), 1), 'status': codes, 'bytes': sizes}
        results.append(result)
        print(json.dumps(result), flush=True)
    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
