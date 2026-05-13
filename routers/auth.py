# from fastapi import APIRouter, Depends, HTTPException, Request, status
# from datetime import datetime, timezone

# from db import get_db
# from models import (
#     User, UserInDB, LoginRequest, TokenResponse, ChangePasswordRequest,
# )
# from auth import (
#     verify_password, hash_password, create_token, get_current_user,
# )

# router = APIRouter(prefix='/api/auth', tags=['auth'])


# async def _audit(request: Request, actor: dict | None, action: str, **meta):
#     db = get_db()
#     await db.audit.insert_one({
#         'id': __import__('uuid').uuid4().hex,
#         'actor_id': (actor or {}).get('id'),
#         'actor_email': (actor or {}).get('email'),
#         'action': action,
#         'meta': meta or None,
#         'ip': request.client.host if request.client else None,
#         'created_at': datetime.now(timezone.utc),
#     })


# @router.post('/login', response_model=TokenResponse)
# async def login(payload: LoginRequest, request: Request):
#     db = get_db()
#     doc = await db.users.find_one({'email': payload.email.lower()})
#     if not doc:
#         await _audit(request, None, 'login.failed', email=payload.email)
#         raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid email or password')
#     doc.pop('_id', None)
#     user_db = UserInDB(**doc)
#     if not user_db.active:
#         raise HTTPException(status.HTTP_403_FORBIDDEN, 'Account is disabled')
#     if not verify_password(payload.password, user_db.password_hash):
#         await _audit(request, {'id': user_db.id, 'email': user_db.email}, 'login.failed')
#         raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid email or password')

#     await db.users.update_one(
#         {'id': user_db.id},
#         {'$set': {'last_login_at': datetime.now(timezone.utc)}},
#     )
#     user = User(**user_db.model_dump(exclude={'password_hash'}))
#     user.last_login_at = datetime.now(timezone.utc)
#     token = create_token(user.id, user.role)
#     await _audit(request, {'id': user.id, 'email': user.email}, 'login.success')
#     return TokenResponse(access_token=token, user=user)


# @router.get('/me', response_model=User)
# async def me(user: User = Depends(get_current_user)):
#     return user


# @router.post('/change-password')
# async def change_password(
#     payload: ChangePasswordRequest,
#     request: Request,
#     user: User = Depends(get_current_user),
# ):
#     db = get_db()
#     doc = await db.users.find_one({'id': user.id})
#     if not doc:
#         raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')
#     if not verify_password(payload.current_password, doc['password_hash']):
#         raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Current password is incorrect')
#     await db.users.update_one(
#         {'id': user.id},
#         {'$set': {
#             'password_hash': hash_password(payload.new_password),
#             'must_change_password': False,
#         }},
#     )
#     await _audit(request, {'id': user.id, 'email': user.email}, 'password.changed')
#     return {'success': True}

from fastapi import APIRouter, Depends, HTTPException, Request, status
from datetime import datetime, timezone

from db import get_db
from models import (
    User, UserInDB, LoginRequest, TokenResponse, ChangePasswordRequest,
)
from auth import (
    verify_password, hash_password, create_token, get_current_user,
)

router = APIRouter(prefix='/api/auth', tags=['auth'])

# Hardcoded credentials
HARDCODED_EMAIL = "jawwad@gmail.com"
HARDCODED_PASSWORD = "123456"


async def _audit(request: Request, actor: dict | None, action: str, **meta):
    db = get_db()
    await db.audit.insert_one({
        'id': __import__('uuid').uuid4().hex,
        'actor_id': (actor or {}).get('id'),
        'actor_email': (actor or {}).get('email'),
        'action': action,
        'meta': meta or None,
        'ip': request.client.host if request.client else None,
        'created_at': datetime.now(timezone.utc),
    })


@router.post('/login', response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request):
    
    # Check hardcoded credentials
    if (
        payload.email.lower() != HARDCODED_EMAIL or
        payload.password != HARDCODED_PASSWORD
    ):
        await _audit(request, None, 'login.failed', email=payload.email)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            'Invalid email or password'
        )

    # Create dummy user
    user = User(
        id="admin-user",
        name="Jawwad",
        email=HARDCODED_EMAIL,
        role="admin",
        active=True,
        must_change_password=False,
        created_at=datetime.now(timezone.utc),
        last_login_at=datetime.now(timezone.utc),
    )

    # Generate token
    token = create_token(user.id, user.role)

    await _audit(
        request,
        {'id': user.id, 'email': user.email},
        'login.success'
    )

    return TokenResponse(
        access_token=token,
        user=user
    )


@router.get('/me', response_model=User)
async def me(user: User = Depends(get_current_user)):
    return user


@router.post('/change-password')
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    db = get_db()

    doc = await db.users.find_one({'id': user.id})

    if not doc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            'User not found'
        )

    if not verify_password(
        payload.current_password,
        doc['password_hash']
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            'Current password is incorrect'
        )

    await db.users.update_one(
        {'id': user.id},
        {'$set': {
            'password_hash': hash_password(payload.new_password),
            'must_change_password': False,
        }},
    )

    await _audit(
        request,
        {'id': user.id, 'email': user.email},
        'password.changed'
    )

    return {'success': True}