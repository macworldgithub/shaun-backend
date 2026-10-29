from models import ClientDocument


async def mirror_evidence(db, job, evidence, uploader):
    client_id = job.get('client_id') or job.get('dc_job_id')
    if not client_id:
        return
    document = ClientDocument(id=evidence.id, client_id=client_id, document_type='Other', status='complete',
        file_name=f'[{evidence.stage.upper()}] {evidence.file_name}', source='staff-upload', uploaded_by=uploader,
        storage_path=evidence.storage_path, content_type=evidence.content_type, size_bytes=evidence.size_bytes,
        notes=f'Contractor evidence ({evidence.stage}): {evidence.caption or "Uploaded via Contractor App"}')
    await db.clients.update_one({'id': client_id, 'documents.id': {'$ne': evidence.id}},
        {'$push': {'documents': document.model_dump(mode='json')}})
