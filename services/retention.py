import os
from datetime import datetime, timedelta, timezone

from db import get_db
from services.storage import delete


async def enforce_document_retention():
    configured_days = os.environ.get('DOCUMENT_RETENTION_DAYS')
    if not configured_days:
        return 0
    days = int(configured_days)
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db = get_db()
    removed = 0
    async for client in db.clients.find({}):
        for document in client.get('documents', []):
            created_at = document.get('created_at')
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            if not created_at or created_at >= cutoff:
                continue
            if document.get('storage_path'):
                await delete(document['storage_path'])
            await db.clients.update_one({'id': client['id']}, {'$pull': {'documents': {'id': document.get('id')}}})
            await db.audit.insert_one({'id': f'retention-{document.get("id")}', 'actor_id': None, 'actor_email': None, 'action': 'document.retention_delete', 'entity': 'client', 'entity_id': client['id'], 'meta': {'document_id': document.get('id'), 'retention_days': days}, 'created_at': datetime.now(timezone.utc)})
            removed += 1
    return removed