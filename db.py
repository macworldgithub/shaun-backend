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
    await db.users.create_index('email', unique=True)
    await db.clients.create_index('vy_order_id')
    await db.clients.create_index('assigned_agent_id')
    await db.clients.create_index('contact_status')
    await db.clients.create_index('stage')
    await db.clients.create_index('delivery_date')
    await db.messages.create_index([('client_id', 1), ('sent_at', -1)])
    await db.messages.create_index([('sent_at', -1)])
    await db.share_links.create_index('token', unique=True)
    await db.upload_links.create_index('token', unique=True)
    await db.upload_links.create_index('client_id')
    await db.offers.create_index('source_key', unique=True, sparse=True)
    await db.offer_snapshots.create_index([('fetched_at', -1)])
    await db.share_views.create_index([('share_link_id', 1), ('viewed_at', -1)])
    await db.audit.create_index([('created_at', -1)])
    # Contractor App indexes
    await db.contractor_jobs.create_index('id', unique=True)
    await db.contractor_jobs.create_index('client_id')
    await db.contractor_jobs.create_index('assigned_contractor_id')
    await db.contractor_jobs.create_index('status')
    await db.contractor_jobs.create_index('full_vin')
    await db.contractor_jobs.create_index('last_6_vin')
    await db.contractor_jobs.create_index('is_urgent')
    await db.contractor_jobs.create_index('due_date')
    await db.contractor_jobs.create_index([('created_at', -1)])
    await db.contractor_time_logs.create_index([('job_id', 1), ('contractor_id', 1)])
    await db.contractor_time_logs.create_index([('contractor_id', 1), ('clock_out', 1)])
    await db.contractor_evidence.create_index([('job_id', 1), ('captured_at', -1)])
    await db.contractor_locations.create_index([('job_id', 1), ('recorded_at', -1)])
    await db.contractor_job_templates.create_index('id', unique=True)
    await db.contractor_activities.create_index([('job_id', 1), ('created_at', -1)])
    await db.contractor_sync_logs.create_index([('synced_at', -1)])


def close():
    global _client
    if _client is not None:
        _client.close()
        _client = None
