import os
import bcrypt
import jwt
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from db import get_db
from models import User, UserInDB

JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGO = os.environ.get('JWT_ALGO', 'HS256')
JWT_EXPIRES_HOURS = int(os.environ.get('JWT_EXPIRES_HOURS', '12'))

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except Exception:
        return False


def generate_password(length: int = 14) -> str:
    alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def create_token(
    subject: str,
    role: str,
    *,
    extra: Optional[dict] = None,
    expires_in_hours: Optional[int] = None,
) -> str:
    now = datetime.now(timezone.utc)
    hours = expires_in_hours if expires_in_hours is not None else JWT_EXPIRES_HOURS
    payload = {
        'sub': subject,
        'role': role,
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(hours=hours)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Session expired')
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid token')


async def _load_user(user_id: str) -> Optional[UserInDB]:
    doc = await get_db().users.find_one({'id': user_id})
    if not doc:
        return None
    doc.pop('_id', None)
    return UserInDB(**doc)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> User:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Authentication required')
    payload = decode_token(creds.credentials)
    role = payload.get('role')
    if role and role.startswith('share:'):
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Share token cannot access this resource')
    user_id = payload.get('sub')
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid token')
    user = await _load_user(user_id)
    if not user or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'User not found or inactive')
    return User(**user.model_dump(exclude={'password_hash'}))


def require_role(*roles: str):
    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, 'Insufficient permissions')
        return user
    return _dep


require_admin = require_role('super_admin', 'admin')
require_super = require_role('super_admin')


async def get_share_payload(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Share access required')
    payload = decode_token(creds.credentials)
    role = payload.get('role', '')
    if not role.startswith('share:'):
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Not a share token')
    return payload
