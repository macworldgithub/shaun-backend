"""Create / update the site-locked Nunawading user for the Delivery Centre (Shaun) platform.

Usage (from shaun-backend/):
    python scripts/create_site_user.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_db, close  # noqa: E402
from auth import hash_password  # noqa: E402
from models import UserInDB  # noqa: E402

EMAIL = 'nunawading@byd.com'
PASSWORD = '123456'
NAME = 'BYD Nunawading'
SITE = 'BYD Nunawading'


async def main():
    db = get_db()
    existing = await db.users.find_one({'email': EMAIL})
    if existing:
        await db.users.update_one(
            {'id': existing['id']},
            {'$set': {
                'password_hash': hash_password(PASSWORD),
                'locked_site': SITE,
                'active_site': SITE,
                'role': 'agent',
                'active': True,
                'must_change_password': False,
            }},
        )
        print(f'Updated existing user {EMAIL}')
    else:
        user = UserInDB(
            email=EMAIL, name=NAME, role='agent',
            active_site=SITE, locked_site=SITE,
            password_hash=hash_password(PASSWORD),
            must_change_password=False,
            created_at=datetime.now(timezone.utc),
        )
        await db.users.insert_one(user.model_dump(mode='json'))
        print(f'Created user {EMAIL}')
    close()


if __name__ == '__main__':
    asyncio.run(main())
