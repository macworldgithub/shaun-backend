import os
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


def test_database_site_counts():
    client = MongoClient(os.environ['MONGO_URL'])
    db = client[os.environ.get('DB_NAME', 'test_database')]
    
    total = db.clients.count_documents({})
    assert total >= 3600
    
    cs_count = db.clients.count_documents({'site_location': 'BYD Caroline Springs'})
    assert cs_count == 2258
    
    nw_count = db.clients.count_documents({'site_location': 'BYD Nunawading'})
    assert nw_count == 290
    
    dm_count = db.clients.count_documents({'site_location': 'Denza Melbourne'})
    assert dm_count == 921


@pytest.mark.anyio
async def test_api_site_filtering():
    db_module.close()
    admin = User(id='test-admin', email='admin@test.com', name='Admin', role='super_admin')
    
    # 1. Caroline Springs
    cs_clients = await list_clients(user=admin, site_location='BYD Caroline Springs', limit=50)
    assert len(cs_clients) > 0
    assert all(c.site_location == 'BYD Caroline Springs' for c in cs_clients)

    # 2. Nunawading
    nw_clients = await list_clients(user=admin, site_location='BYD Nunawading', limit=50)
    assert len(nw_clients) > 0
    assert all(c.site_location == 'BYD Nunawading' for c in nw_clients)

    # 3. Denza Melbourne
    dm_clients = await list_clients(user=admin, site_location='Denza Melbourne', limit=50)
    assert len(dm_clients) > 0
    assert all(c.site_location == 'Denza Melbourne' for c in dm_clients)

    # 4. Stats filtering
    cs_stats = await stats(site_location='BYD Caroline Springs', _=admin)
    assert cs_stats['total_clients'] == 2258

    nw_stats = await stats(site_location='BYD Nunawading', _=admin)
    assert nw_stats['total_clients'] == 290

    dm_stats = await stats(site_location='Denza Melbourne', _=admin)
    assert dm_stats['total_clients'] == 921
    
    db_module.close()
