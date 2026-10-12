import sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mobilemessage

def environment():
    return patch.dict(mobilemessage.os.environ, {'MOBILEMESSAGE_USERNAME':'fixture','MOBILEMESSAGE_PASSWORD':'fixture','MOBILEMESSAGE_SENDER_ID':'+61468104118'}, clear=True)

def test_unconfigured_sms_does_not_report_success():
    with patch.dict(mobilemessage.os.environ, {}, clear=True), patch.object(mobilemessage,'MOBILEMESSAGE_USERNAME',None), patch.object(mobilemessage,'MOBILEMESSAGE_PASSWORD',None), patch.object(mobilemessage.requests,'post') as post:
        result=mobilemessage._send_sync('+61499918523','test')
    assert result['success'] is False
    assert result['status']=='unconfigured'
    post.assert_not_called()

def test_rejected_or_untrackable_send_is_failed():
    for response in ({'status':'FAILED','error':'rejected'}, {'status':'success'}, {'status':'unknown','message_id':'id'}):
        with environment(), patch.object(mobilemessage.requests,'post',return_value=SimpleNamespace(status_code=200,json=lambda: {'results':[response]})):
            result=mobilemessage._send_sync('0499 918 523','test')
        assert result['success'] is False
        assert result['status']=='failed'
        assert result['error']

def test_live_payload_keeps_text_and_sender_and_tracks_provider_id():
    body='Hello — 🚗 ' + 'a'*1600
    def respond(url,**kwargs):
        assert url.endswith('/v1/messages')
        assert kwargs['json']['enable_unicode'] is True
        message=kwargs['json']['messages'][0]
        assert message['message']==body
        assert message['to']=='+61499918523'
        assert message['sender']=='+61468104118'
        uuid.UUID(kwargs['headers']['Idempotency-Key'])
        return SimpleNamespace(status_code=200,json=lambda: {'results':[{'status':'SUCCESS','message_id':'provider-id'}]})
    with environment(), patch.object(mobilemessage.requests,'post',side_effect=respond):
        result=mobilemessage._send_sync('0499 918 523',body)
    assert result['success'] is True
    assert result['message_id']=='provider-id'


def test_inbound_site_follows_correlation_and_ambiguous_owners_fail_closed():
    from services.sms_event import inbound_site
    leads=[{'dealer':'BYD Melbourne'},{'dealer':'BYD Nunawading'}]
    clients=[{'id':'delivery-1','site_location':'BYD Melbourne'}]
    assert inbound_site(None,None,leads,clients)==''
    assert inbound_site({'client_id':'delivery-1'},None,leads,clients)=='BYD Melbourne'
    assert inbound_site(None,{'dealer':'BYD Nunawading'},leads,clients)=='BYD Nunawading'
    assert inbound_site(None,None,[{'dealer':'Melbourne CBD'}],clients)=='Melbourne CBD'
