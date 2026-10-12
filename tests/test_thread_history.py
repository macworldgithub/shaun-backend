import os, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from dotenv import load_dotenv
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[1]/'.env')
from services.thread_history import merge_rows, phone_key, site_key, allowed_site, assert_message_access
from fastapi import HTTPException
def test_phone_and_site_identity():
    assert phone_key('+61 499 918 523') == phone_key('0499-918-523') == '61499918523'
    assert site_key('Melbourne CBD') == site_key('BYD Melbourne')
    assert site_key('Denza Melbourne') != site_key('BYD Melbourne')
def test_status_and_direction_deduplication():
    rows=[
        {'id':'1','provider_message_id':'provider','direction':'outbound','status':'sent','sent_at':'2026-10-12T00:00:00Z'},
        {'id':'2','provider_message_id':'provider','direction':'outbound','status':'delivered','sent_at':'2026-10-12T00:00:00Z'},
        {'id':'3','provider_message_id':'reply','direction':'inbound','status':'received','sent_at':'2026-10-12T00:01:00Z'},
        {'id':'4','provider_message_id':'reply','direction':'inbound','status':'received','sent_at':'2026-10-12T00:01:00Z'},
    ]
    merged=merge_rows(rows)
    assert len(merged)==2
    assert [m['status'] for m in merged]==['received','delivered']
def test_site_lock_overrides_admin_role_and_blocks_direct_client_access():
    user=SimpleNamespace(role='super_admin',locked_site='BYD Caroline Springs',active_site='All Sites')
    assert allowed_site(user,'Caroline Springs')
    assert not allowed_site(user,'BYD Nunawading')
    assert not allowed_site(user,'')
    with pytest.raises(HTTPException) as error:
        assert_message_access(user,{'site_location':'BYD Nunawading'})
    assert error.value.status_code==404
def test_contractor_cannot_read_customer_threads():
    assert not allowed_site(SimpleNamespace(role='contractor',locked_site=None),'BYD Melbourne')
def test_history_merges_stores_and_restricts_client_reads(monkeypatch):
    import asyncio
    import services.thread_history as module
    class Cursor:
        def __init__(self, docs): self.docs = docs
        async def to_list(self, length): return [dict(d) for d in self.docs]
    class Collection:
        def __init__(self, docs): self.docs = docs
        def find(self, *args): return Cursor(self.docs)
        async def find_one(self, query): return next((dict(d) for d in self.docs if all(d.get(k)==v for k,v in query.items())), None)
    client={'id':'client','name':'Test','phone':'+61 499 918 523','site':'Denza Melbourne'}
    conversations=[
        {'phone':'0499 918 523','dealer':'BYD Melbourne','prospectName':'Test','messages':[{'id':'a','sender':'agent','text':'sales','createdAt':'2026-10-12T00:00:00Z'}]},
        {'phone':'+61499918523','dealer':'Denza Melbourne','prospectName':'Test','messages':[{'id':'b','sender':'user','text':'delivery','createdAt':'2026-10-12T00:01:00Z'}]},
    ]
    db=SimpleNamespace(clients=Collection([client]),messages=Collection([{'_id':'record','provider_message_id':'a','phone':'+61499918523','body':'sales','direction':'outbound','status':'delivered','sent_at':'2026-10-12T00:00:00Z'}]))
    lead_db=SimpleNamespace(conversations=Collection(conversations),leads=Collection([]))
    monkeypatch.setattr(module,'get_db',lambda:db)
    monkeypatch.setattr(module,'get_lead_db',lambda:lead_db)
    admin=SimpleNamespace(role='super_admin',locked_site=None,active_site=None)
    rows=asyncio.run(module.history(admin,client_id='client'))
    assert len(rows)==2
    assert rows[1]['status']=='delivered'
    locked=SimpleNamespace(role='super_admin',locked_site='BYD Melbourne',active_site=None)
    assert len(asyncio.run(module.history(locked)))==1
    with pytest.raises(HTTPException):
        asyncio.run(module.history(locked,client_id='client'))
def test_provider_phone_matching_and_stable_event_identity():
    import re
    from services.sms_event import phone_pattern, inbound_id, received_at
    pattern=phone_pattern('+61499918523')
    for value in ['+61 499 918 523','0499 918 523','61499918523']:
        assert re.fullmatch(pattern,value)
    assert not re.fullmatch(pattern,'+61499918524')
    payload={'sender':'61499918523','to':'61468104118','message':'SYNC CHECK 1210','received_at':'2026-10-12 09:00:00','type':'inbound','original_message_id':'outgoing-id'}
    ident=inbound_id(payload,payload['sender'],payload['message'])
    assert ident==inbound_id(dict(payload),'+61 499 918 523',payload['message'])
    assert ident!='outgoing-id'
    assert ident!=inbound_id({**payload,'received_at':'2026-10-12 09:00:01'},payload['sender'],payload['message'])
    assert received_at(payload).isoformat()=='2026-10-12T09:00:00+00:00'
def test_legacy_handset_reply_is_visible_to_the_original_outbound_site(monkeypatch):
    import asyncio
    import services.thread_history as module
    class Cursor:
        def __init__(self,docs): self.docs=docs
        async def to_list(self,length): return [dict(d) for d in self.docs]
    class Collection:
        def __init__(self,docs): self.docs=docs
        def find(self,*args): return Cursor(self.docs)
        async def find_one(self,query): return next((dict(d) for d in self.docs if all(d.get(k)==v for k,v in query.items())), None)
    clients=[{'id':'denza','phone':'+61499918523','site':'Denza Melbourne','name':'Test'}]
    messages=[
        {'_id':'out','provider_message_id':'out','client_id':'denza','phone':'+61499918523','body':'outgoing','direction':'outbound','status':'sent','sent_at':'2026-10-12T09:00:00Z'},
        {'_id':'reply','provider_message_id':'reply','phone':'+61499918523','body':'handset reply','direction':'inbound','status':'received','sent_at':'2026-10-12T09:01:00Z','provider_response':{'original_message_id':'out'}},
    ]
    db=SimpleNamespace(clients=Collection(clients),messages=Collection(messages))
    lead_db=SimpleNamespace(conversations=Collection([{'phone':'+61499918523','dealer':'BYD Melbourne','messages':[]}]),leads=Collection([]))
    monkeypatch.setattr(module,'get_db',lambda:db)
    monkeypatch.setattr(module,'get_lead_db',lambda:lead_db)
    user=SimpleNamespace(role='agent',locked_site='Denza Melbourne',active_site='Denza Melbourne')
    rows=asyncio.run(module.history(user,client_id='denza'))
    assert len(rows)==2
    assert rows[0]['body']=='handset reply'
