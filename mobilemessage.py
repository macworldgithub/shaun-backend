"""MobileMessage.com.au SMS provider wrapper.

Replaces ClickSend. Exposes the same interface:
  - send_sms(to, body) -> dict
  - render_template(body, data) -> str
  - normalise_au(phone) -> str

API docs: https://mobilemessage.com.au/api-documentation
Auth: HTTP Basic (API Username + API Password)
Endpoint: POST https://api.mobilemessage.com.au/v1/messages
"""
import os
import asyncio
import logging
import uuid
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

# Ensure .env is loaded if imported standalone
load_dotenv(Path(__file__).resolve().parent / '.env')

import requests

log = logging.getLogger('mobilemessage')

MOBILEMESSAGE_USERNAME = os.environ.get('MOBILEMESSAGE_USERNAME')
MOBILEMESSAGE_PASSWORD = os.environ.get('MOBILEMESSAGE_PASSWORD')
MOBILEMESSAGE_FROM = os.environ.get('MOBILEMESSAGE_FROM', 'HARMONY')
MOBILEMESSAGE_URL = os.environ.get('MOBILEMESSAGE_API_URL', 'https://api.mobilemessage.com.au/v1/messages').rstrip('/')
if not MOBILEMESSAGE_URL.endswith('/v1/messages'):
    MOBILEMESSAGE_URL = MOBILEMESSAGE_URL.rstrip('/') + '/v1/messages'


def normalise_au(phone: str) -> str:
    """Normalise an Australian phone number to E.164 (+61...)."""
    digits = ''.join(c for c in (phone or '') if c.isdigit() or c == '+')
    if digits.startswith('+'):
        return digits
    if digits.startswith('61') and len(digits) == 11:
        return '+' + digits
    if digits.startswith('04') and len(digits) == 10:
        return '+61' + digits[1:]
    if digits.startswith('4') and len(digits) == 9:
        return '+61' + digits
    return digits


def _send_sync(to: str, body: str, custom_ref: Optional[str] = None) -> dict:
    username = os.environ.get('MOBILEMESSAGE_USERNAME') or MOBILEMESSAGE_USERNAME
    password = os.environ.get('MOBILEMESSAGE_PASSWORD') or os.environ.get('MOBILEMESSAGE_API_KEY') or MOBILEMESSAGE_PASSWORD
    from_sender = os.environ.get('MOBILEMESSAGE_SENDER_ID') or os.environ.get('MOBILEMESSAGE_FROM') or MOBILEMESSAGE_FROM or 'HARMONY'
    url = MOBILEMESSAGE_URL

    if not username or not password:
        log.warning('MobileMessage credentials missing; SMS not sent')
        return {'success': False, 'status': 'unconfigured', 'error': 'SMS provider not configured'}

    to_norm = normalise_au(to)
    payload = {
        'enable_unicode': True,
        'messages': [{
            'to': to_norm,
            'message': body,
            'sender': from_sender,
            **({'custom_ref': custom_ref} if custom_ref else {}),
        }]
    }
    try:
        r = requests.post(
            url,
            auth=(username, password),
            json=payload,
            headers={'Idempotency-Key': str(uuid.uuid4())},
            timeout=20,
        )
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text}

        if r.status_code == 429:
            return {
                'success': False,
                'status': 'rate_limited',
                'error': 'Too many concurrent requests (max 5)',
                'response': data,
            }

        if r.status_code == 200:
            results = (data or {}).get('results') or []
            if results:
                first = results[0]
                api_status = str(first.get('status', '')).lower()
                ok = api_status in ('success', 'queued', 'sent', 'accepted') and bool(first.get('message_id'))
                return {
                    'success': ok,
                    'status': api_status if ok else 'failed',
                    'message_id': first.get('message_id'),
                    'price': first.get('cost'),
                    'error': None if ok else (first.get('error') or first.get('message') or 'SMS provider rejected the message or returned no message ID'),
                    'response': data,
                }
            return {
                'success': False,
                'status': 'failed',
                'error': data.get('message') or 'Empty results from MobileMessage',
                'response': data,
            }

        return {
            'success': False,
            'status': 'failed',
            'error': (data.get('message') if isinstance(data, dict) else None) or f'HTTP {r.status_code}',
            'response': data,
        }
    except requests.RequestException as e:
        log.exception('MobileMessage send failed')
        return {'success': False, 'status': 'error', 'error': str(e)}


async def send_sms(to: str, body: str, custom_ref: Optional[str] = None) -> dict:
    return await asyncio.to_thread(_send_sync, to, body, custom_ref)


def render_template(body: str, data: dict) -> str:
    out = body
    for k, v in data.items():
        val = str(v) if v is not None else ''
        out = out.replace('{{' + k + '}}', val)
        out = out.replace('{' + k + '}', val)
    return out
