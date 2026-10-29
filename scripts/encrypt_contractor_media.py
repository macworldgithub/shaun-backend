"""Encrypt existing contractor evidence in place; references and download URLs stay valid.

Run after setting CONTRACTOR_ENCRYPTION_KEY and backing it up in your secret manager.
This command is restartable and skips evidence already encrypted.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_db, close
from services.storage import _get_stored_bytes, put_bytes, ENCRYPTED_HEADER
from services.contractor_integration import cipher


async def main():
    cipher()  # Validate the key before reading or changing any file.
    encrypted = 0
    async for evidence in get_db().contractor_evidence.find({}):
        key = evidence.get('storage_path')
        if not key:
            continue
        data = await _get_stored_bytes(key)
        if not data.startswith(ENCRYPTED_HEADER):
            await put_bytes(key, data, evidence.get('content_type'))
            encrypted += 1
    print(f'Encrypted {encrypted} existing contractor attachments.')
    close()


if __name__ == '__main__':
    asyncio.run(main())
