from unittest.mock import MagicMock, AsyncMock
import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def contractor_environment(monkeypatch):
    monkeypatch.setenv('CONTRACTOR_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('CONTRACTOR_WEBHOOK_SECRET', 'test-webhook-secret')
    yield


class Cursor:
    def __init__(self, data=()): self.data = list(data)
    def sort(self, *args, **kwargs): return self
    def skip(self, value): return Cursor(self.data[value:])
    def limit(self, value): return Cursor(self.data[:value])
    def __aiter__(self): return self.generate()
    async def generate(self):
        for item in self.data:
            yield dict(item)


def async_collection():
    collection = MagicMock()
    for name in ('find_one', 'find_one_and_update'):
        setattr(collection, name, AsyncMock(return_value=None))
    for name in ('insert_one', 'update_one', 'update_many', 'delete_one'):
        setattr(collection, name, AsyncMock(return_value=MagicMock(matched_count=1, modified_count=1, deleted_count=1)))
    collection.count_documents = AsyncMock(return_value=0)
    collection.find.return_value = Cursor()
    return collection


def mock_database():
    db = MagicMock()
    for name in ('users', 'clients', 'settings', 'audit', 'app_settings', 'push_subscriptions', 'contractor_jobs', 'contractor_time_logs',
                 'contractor_activities', 'contractor_evidence', 'contractor_locations',
                 'contractor_sync_logs', 'contractor_job_templates', 'contractor_outbox',
                 'contractor_offline_actions', 'contractor_webhook_receipts'):
        setattr(db, name, async_collection())
    return db
