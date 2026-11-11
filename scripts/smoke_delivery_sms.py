"""Inspect SMS routing; optionally send one explicitly requested handset test.

Run from the backend root: python scripts/smoke_delivery_sms.py --to +61499918523 --send
Credentials stay in the environment. Only the requested number is shown.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mobilemessage
import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--to', required=True)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--sender', help='An approved sender on this account')
    parser.add_argument('--reference', default='harmony-critical-updates')
    parser.add_argument('--record-site', help='Record the real test send in this Delivery Centre site for reply correlation')
    parser.add_argument('--check', action='store_true', help='Check provider status and matching HARMONY OK replies without sending')
    args = parser.parse_args()
    auth = (os.environ.get('MOBILEMESSAGE_USERNAME'),
            os.environ.get('MOBILEMESSAGE_PASSWORD') or os.environ.get('MOBILEMESSAGE_API_KEY'))
    if not all(auth):
        raise SystemExit('SMS credentials are not configured')
    base = mobilemessage.MOBILEMESSAGE_URL.removesuffix('/v1/messages')
    for resource in ('senders', 'webhooks'):
        response = requests.get(f'{base}/v1/{resource}', auth=auth, timeout=20)
        response.raise_for_status()
        data = response.json()
        if resource == 'webhooks':
            data = {key: data.get(key) for key in ('status', 'webhooks', 'has_signing_secret')}
        else:
            selected = args.sender or os.environ.get('MOBILEMESSAGE_SENDER_ID') or os.environ.get('MOBILEMESSAGE_FROM')
            data = {'configured_sender': selected, 'matching_senders': [row for row in data.get('results', [])
                    if mobilemessage.normalise_au(row.get('sender', '')) == mobilemessage.normalise_au(selected or '')]}
        print(json.dumps({resource: data}))
    if args.check:
        response = requests.get(f'{base}/v1/messages', auth=auth, params={'custom_ref': args.reference}, timeout=20)
        response.raise_for_status()
        print(json.dumps({'provider_test_messages': [{key: row.get(key) for key in ('message_id', 'status', 'sender', 'to')}
                          for row in response.json().get('results', [])
                          if mobilemessage.normalise_au(row.get('to', '')) == mobilemessage.normalise_au(args.to)]}))
        import asyncio
        from db import get_db, close
        from services.sms_event import phone_pattern
        async def check_replies():
            try:
                rows = await get_db().messages.find({'phone': {'$regex': phone_pattern(args.to)},
                    'direction': 'inbound', 'body': {'$regex': '^HARMONY OK', '$options': 'i'}},
                    {'_id': 0, 'id': 1, 'site': 1, 'body': 1, 'sent_at': 1}).sort('sent_at', -1).limit(5).to_list(5)
                print(json.dumps({'handset_test_replies': rows}, default=str))
            finally:
                close()
        asyncio.run(check_replies())
    if args.send:
        if args.sender:
            os.environ['MOBILEMESSAGE_SENDER_ID'] = args.sender
        body = f'Harmony Delivery Centre test {datetime.now(timezone.utc):%d %b %H:%M UTC}. Please reply HARMONY OK to verify incoming messages.'
        result = mobilemessage._send_sync(args.to, body, args.reference)
        print(json.dumps({key: result.get(key) for key in ('success', 'status', 'message_id', 'error')}))
        if not result.get('success'):
            raise SystemExit(1)
        if args.record_site:
            import asyncio
            from db import get_db, close
            from models import Message
            async def record():
                try:
                    message = Message(phone=mobilemessage.normalise_au(args.to), body=body,
                                      site=args.record_site, direction='outbound', status=result['status'],
                                      provider_message_id=result['message_id'], provider_response=result.get('response'))
                    await get_db().messages.insert_one(message.model_dump(mode='json'))
                    print(json.dumps({'recorded': True, 'site': args.record_site, 'message_id': result['message_id']}))
                finally:
                    close()
            asyncio.run(record())


if __name__ == '__main__':
    main()
