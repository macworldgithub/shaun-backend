import asyncio
import os
import uuid
from pathlib import Path

import boto3


LOCAL_ROOT = Path(__file__).resolve().parents[1] / 'storage' / 'documents'
ENCRYPTED_HEADER = b'BYD-CONTRACTOR-ENC1\n'


def _contractor_media(key):
    return 'contractor-jobs/' in str(key).replace('\\', '/')


def _use_s3() -> bool:
    return os.environ.get('DOCUMENT_STORAGE_BACKEND', 'local').lower() == 's3'


def _s3_client():
    return boto3.client(
        's3',
        region_name=os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION'),
        endpoint_url=os.environ.get('AWS_ENDPOINT_URL') or None,
    )


async def put_bytes(key: str, data: bytes, content_type: str | None = None):
    if _contractor_media(key):
        from services.contractor_integration import cipher
        data = ENCRYPTED_HEADER + cipher().encrypt(data)
    if _use_s3():
        bucket = os.environ.get('DOCUMENT_S3_BUCKET')
        if not bucket:
            raise RuntimeError('DOCUMENT_S3_BUCKET is required when DOCUMENT_STORAGE_BACKEND=s3')
        kwargs = {'Bucket': bucket, 'Key': key, 'Body': data, 'ServerSideEncryption': 'AES256'}
        if os.environ.get('DOCUMENT_S3_KMS_KEY_ID'):
            kwargs.update(ServerSideEncryption='aws:kms', SSEKMSKeyId=os.environ['DOCUMENT_S3_KMS_KEY_ID'])
        if content_type:
            kwargs['ContentType'] = content_type
        await asyncio.to_thread(_s3_client().put_object, **kwargs)
        return key
    path = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() not in path.parents:
        raise ValueError('Storage path must stay inside the documents directory')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


async def _get_stored_bytes(key: str) -> bytes:
    if _use_s3():
        bucket = os.environ.get('DOCUMENT_S3_BUCKET')
        if not bucket:
            raise RuntimeError('DOCUMENT_S3_BUCKET is required when DOCUMENT_STORAGE_BACKEND=s3')
        result = await asyncio.to_thread(_s3_client().get_object, Bucket=bucket, Key=key)
        return await asyncio.to_thread(result['Body'].read)
    path = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() not in path.parents or not path.is_file():
        raise FileNotFoundError(key)
    return path.read_bytes()


async def get_bytes(key: str) -> bytes:
    data = await _get_stored_bytes(key)
    if data.startswith(ENCRYPTED_HEADER):
        from services.contractor_integration import cipher
        return cipher().decrypt(data[len(ENCRYPTED_HEADER):])
    return data


async def exists(key: str) -> bool:
    if _use_s3():
        bucket = os.environ.get('DOCUMENT_S3_BUCKET')
        if not bucket:
            return False
        try:
            await asyncio.to_thread(_s3_client().head_object, Bucket=bucket, Key=key)
            return True
        except Exception:
            return False
    return (LOCAL_ROOT / key).is_file()


async def delete(key: str):
    if _use_s3():
        bucket = os.environ.get('DOCUMENT_S3_BUCKET')
        if bucket:
            await asyncio.to_thread(_s3_client().delete_object, Bucket=bucket, Key=key)
        return
    path = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() in path.parents and path.is_file():
        path.unlink()
