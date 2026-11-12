import asyncio
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from auth import get_current_user
from db import get_db
from models import User
from services import sms_reply_notifications as service

router = APIRouter(prefix='/api/notifications/sms-replies', tags=['SMS reply notifications'])


class ReadRequest(BaseModel):
    seen_message_ids: List[str] = Field(default_factory=list, max_length=100)


@router.get('')
async def notifications(response: Response, user: User = Depends(get_current_user), site: Optional[str] = None,
                        all: bool = False, offset: int = Query(0, ge=0, le=100000)):
    response.headers['Cache-Control'] = 'no-store'
    db = get_db(); base = service.scope(user, site); key = service.reader(user)
    query = base if all else {**base, 'read_by': {'$ne': key}}
    rows, count = await asyncio.gather(
        db.sms_reply_notifications.find(query).sort([('created_at', -1), ('_id', -1)]).skip(offset).limit(21).to_list(21),
        db.sms_reply_notifications.count_documents({**base, 'read_by': {'$ne': key}}),
    )
    items = [{**{k: v for k, v in row.items() if k not in ('_id', 'read_by', 'phone_key')}, 'read': key in row.get('read_by', [])} for row in rows[:20]]
    return {'success': True, 'data': {'items': items, 'unread_count': count, 'has_more': len(rows) > 20, 'next_offset': offset + 20}}


@router.get('/{identity}/messages')
async def messages(identity: str, response: Response, user: User = Depends(get_current_user)):
    response.headers['Cache-Control'] = 'no-store'
    return {'success': True, 'data': await service.conversation(get_db(), user, identity)}


@router.post('/{identity}/read')
async def read(identity: str, payload: ReadRequest, user: User = Depends(get_current_user)):
    db = get_db(); row = await service.notification(db, user, identity)
    await db.sms_reply_notifications.update_many({'site': row['site'], 'phone_key': row['phone_key'],
        '$or': [{'_id': row['_id']}, {'message_id': {'$in': payload.seen_message_ids}}]}, {'$addToSet': {'read_by': service.reader(user)}})
    return {'success': True}
