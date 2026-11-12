import asyncio
import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth import get_current_user
from models import User
from routers import sms_reply_notifications as routes
from services import sms_reply_notifications as service


def matches(row, query):
    for key, value in query.items():
        actual = row.get(key)
        if key == '$or':
            if not any(matches(row, q) for q in value): return False
        elif isinstance(value, dict) and '$ne' in value:
            if value['$ne'] in actual if isinstance(actual, list) else actual == value['$ne']: return False
        elif isinstance(value, dict) and '$in' in value:
            if actual not in value['$in']: return False
        elif actual != value: return False
    return True


class Collection:
    def __init__(self): self.rows = []
    async def update_one(self, query, update, **kwargs):
        if not any(matches(row, query) for row in self.rows): self.rows.append(copy.deepcopy(update['$setOnInsert']))
    async def update_many(self, query, update):
        for row in self.rows:
            if matches(row, query) and update['$addToSet']['read_by'] not in row['read_by']: row['read_by'].append(update['$addToSet']['read_by'])
    async def find_one(self, query): return next((copy.deepcopy(row) for row in self.rows if matches(row, query)), None)
    async def count_documents(self, query): return sum(matches(row, query) for row in self.rows)
    def find(self, query):
        rows = [copy.deepcopy(row) for row in self.rows if matches(row, query)]
        class Cursor:
            def sort(self, *args): rows.sort(key=lambda r:(r['created_at'],r['id']),reverse=True); return self
            def skip(self, n): del rows[:n]; return self
            def limit(self, n): del rows[n:]; return self
            async def to_list(self, n): return rows[:n]
        return Cursor()


def fixture(monkeypatch):
    db = SimpleNamespace(sms_reply_notifications=Collection())
    monkeypatch.setattr(routes, 'get_db', lambda: db)
    user = User(id='delivery-id', name='Fixture Staff', email='staff@example.com', role='delivery_consultant', locked_site='BYD Fairfield')
    app=FastAPI(); app.include_router(routes.router); app.dependency_overrides[get_current_user]=lambda:user
    return db,user,TestClient(app)


def message(identity, **changes):
    return {'provider_message_id':identity,'direction':'inbound','phone':'+61499918523','site':'BYD Fairfield',
            'client_name':'Fixture Customer','body':'Normal reply with no special keywords','sent_at':'2026-11-12T10:00:00Z',**changes}


def test_inbound_identity_dedup_and_read_state_persist_across_receivers(monkeypatch):
    db,user,api=fixture(monkeypatch)
    identity=asyncio.run(service.record_reply(db,message('event-1')))
    assert identity == hashlib.sha256(b'sms-reply:event-1').hexdigest()
    assert api.get('/api/notifications/sms-replies').json()['data']['unread_count']==1
    assert api.post(f'/api/notifications/sms-replies/{identity}/read',json={}).status_code==200
    asyncio.run(service.record_reply(db,message('event-1')))
    assert len(db.sms_reply_notifications.rows)==1
    assert api.get('/api/notifications/sms-replies').json()['data']['unread_count']==0
    assert service.reader(user)==hashlib.sha256(b'staff@example.com').hexdigest()
    user.email='other@example.com'
    assert api.get('/api/notifications/sms-replies').json()['data']['unread_count']==1


def test_ambiguous_and_outbound_events_do_not_notify(monkeypatch):
    db,user,api=fixture(monkeypatch)
    for data in [message('out',direction='outbound'),message('unknown',site='')]: asyncio.run(service.record_reply(db,data))
    assert api.get('/api/notifications/sms-replies').json()['data']['unread_count']==0


def test_cross_site_notifications_cannot_be_listed_opened_or_marked_read(monkeypatch):
    db,user,api=fixture(monkeypatch)
    identity=asyncio.run(service.record_reply(db,message('private',site='BYD Nunawading')))
    assert api.get('/api/notifications/sms-replies').json()['data']['items']==[]
    assert api.get(f'/api/notifications/sms-replies/{identity}/messages').status_code==404
    assert api.post(f'/api/notifications/sms-replies/{identity}/read',json={}).status_code==404
    assert api.get('/api/notifications/sms-replies?site=BYD%20Nunawading').json()['data']['items']==[]


def test_opened_conversation_is_scoped_and_acknowledges_only_viewed_messages(monkeypatch):
    db,user,api=fixture(monkeypatch)
    first=asyncio.run(service.record_reply(db,message('first')))
    asyncio.run(service.record_reply(db,message('newer')))
    asyncio.run(service.record_reply(db,message('other-phone',phone='+61400000000')))
    history=AsyncMock(return_value=[{'id':'first','provider_message_id':'first','body':'Reply','direction':'inbound','sent_at':'2026-11-12T10:00:00Z'}])
    monkeypatch.setattr(service,'history',history)
    response=api.get(f'/api/notifications/sms-replies/{first}/messages')
    assert response.status_code==200 and response.json()['data']['recipient']['site']=='BYD Fairfield'
    assert history.call_args.kwargs['requested_site']=='BYD Fairfield'
    api.post(f'/api/notifications/sms-replies/{first}/read',json={'seen_message_ids':['first','other-phone']})
    assert api.get('/api/notifications/sms-replies').json()['data']['unread_count']==2


def test_pagination_counts_and_site_aliases(monkeypatch):
    db,user,api=fixture(monkeypatch)
    for i in range(25): asyncio.run(service.record_reply(db,message(f'page-{i}')))
    page=api.get('/api/notifications/sms-replies').json()['data']
    assert len(page['items'])==20 and page['has_more'] and page['unread_count']==25
    assert 'read_by' not in page['items'][0]
    assert len(api.get('/api/notifications/sms-replies?offset=20').json()['data']['items'])==5
    user.locked_site='BYD Melbourne City'
    asyncio.run(service.record_reply(db,message('alias',site='Melbourne CBD')))
    asyncio.run(service.record_reply(db,message('denza',site='Denza Melbourne')))
    assert api.get('/api/notifications/sms-replies').json()['data']['unread_count']==1
