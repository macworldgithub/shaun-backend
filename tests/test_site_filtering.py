import os
import re
import sys
import pytest
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[1] / '.env')

from pymongo import MongoClient
from models import User
from routers.clients import list_clients
from routers.admin import stats
import db as db_module

SITES = ('BYD Caroline Springs', 'BYD Nunawading', 'Denza Melbourne')


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def database():
    with MongoClient(os.environ['MONGO_URL'], serverSelectionTimeoutMS=15000) as client:
        yield client[os.environ.get('DB_NAME', 'test_database')]


def test_database_site_counts(database):
    # Imports legitimately change these totals; verify the site partition rather
    # than the size of a historical snapshot.
    counts = [database.clients.count_documents({'site_location': site}) for site in SITES]
    assert all(count > 0 for count in counts)
    assert sum(counts) <= database.clients.count_documents({})


@pytest.mark.anyio
async def test_api_site_filtering(database):
    db_module.close()
    admin = User(id='test-admin', email='admin@test.com', name='Admin', role='super_admin')

    try:
        for site in SITES:
            expected = database.clients.count_documents({
                'site_location': {'$regex': f'^{re.escape(site)}$', '$options': 'i'},
            })
            clients = await list_clients(user=admin, site_location=site, limit=50)
            assert len(clients) == min(50, expected)
            assert all(c.site_location.casefold() == site.casefold() for c in clients)
            site_stats = await stats(site_location=site, user=admin)
            assert site_stats['total_clients'] == expected
            assert sum(site_stats['by_stage'].values()) == expected
    finally:
        db_module.close()
