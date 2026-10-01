import os
from motor.motor_asyncio import AsyncIOMotorClient
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ.get('DB_NAME', 'test_database')

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URL)
    return _client


def get_db():
    return get_client()[DB_NAME]


async def ensure_indexes():
    db = get_db()

    async def safe_index(col, *args, **kwargs):
        try:
            await col.create_index(*args, **kwargs)
        except Exception:
            pass

    await safe_index(db.users, 'email', unique=True)
    await safe_index(db.clients, 'vy_order_id')
    await safe_index(db.clients, 'assigned_agent_id')
    await safe_index(db.clients, 'contact_status')
    await safe_index(db.clients, 'stage')
    await safe_index(db.clients, 'delivery_date')
    await safe_index(db.messages, [('client_id', 1), ('sent_at', -1)])
    await safe_index(db.messages, [('sent_at', -1)])
    await safe_index(db.share_links, 'token', unique=True)
    await safe_index(db.upload_links, 'token', unique=True)
    await safe_index(db.upload_links, 'client_id')
    await safe_index(db.offers, 'source_key', unique=True, sparse=True)
    await safe_index(db.offer_snapshots, [('fetched_at', -1)])
    await safe_index(db.share_views, [('share_link_id', 1), ('viewed_at', -1)])
    await safe_index(db.audit, [('created_at', -1)])
    # Contractor App indexes
    await safe_index(db.contractor_jobs, 'id', unique=True)
    await safe_index(db.contractor_jobs, 'client_id')
    await safe_index(db.contractor_jobs, 'assigned_contractor_id')
    await safe_index(db.contractor_jobs, 'status')
    await safe_index(db.contractor_jobs, 'full_vin')
    await safe_index(db.contractor_jobs, 'last_6_vin')
    await safe_index(db.contractor_jobs, 'is_urgent')
    await safe_index(db.contractor_jobs, 'due_date')
    await safe_index(db.contractor_jobs, [('created_at', -1)])
    await safe_index(db.contractor_time_logs, [('job_id', 1), ('contractor_id', 1)])
    await safe_index(db.contractor_time_logs, [('contractor_id', 1), ('clock_out', 1)])
    await safe_index(db.contractor_evidence, [('job_id', 1), ('captured_at', -1)])
    await safe_index(db.contractor_locations, [('job_id', 1), ('recorded_at', -1)])
    await safe_index(db.contractor_job_templates, 'id', unique=True)
    await safe_index(db.contractor_activities, [('job_id', 1), ('created_at', -1)])
    await safe_index(db.contractor_sync_logs, [('synced_at', -1)])


def close():
    global _client
    if _client is not None:
        _client.close()
        _client = None
