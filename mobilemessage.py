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
from typing import Optional

import requests

log = logging.getLogger('mobilemessage')

MOBILEMESSAGE_USERNAME = os.environ.get('MOBILEMESSAGE_USERNAME')
MOBILEMESSAGE_PASSWORD = os.environ.get('MOBILEMESSAGE_PASSWORD')
MOBILEMESSAGE_FROM = os.environ.get('MOBILEMESSAGE_FROM', 'BYDMELB')
MOBILEMESSAGE_URL = 'https://api.mobilemessage.com.au/v1/messages'


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
    if not MOBILEMESSAGE_USERNAME or not MOBILEMESSAGE_PASSWORD:
        log.warning('MobileMessage credentials missing; SMS not sent')
        return {'success': False, 'status': 'unconfigured', 'error': 'SMS provider not configured'}

    to_norm = normalise_au(to)
    payload = {
        'messages': [{
            'to': to_norm,
            'message': body[:1530],
            'sender': MOBILEMESSAGE_FROM,
            **({'custom_ref': custom_ref} if custom_ref else {}),
        }]
    }
    try:
        r = requests.post(
            MOBILEMESSAGE_URL,
            auth=(MOBILEMESSAGE_USERNAME, MOBILEMESSAGE_PASSWORD),
            json=payload,
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
                ok = api_status in ('success', 'queued', 'sent', 'accepted')
                return {
                    'success': ok,
                    'status': api_status or 'queued',
                    'message_id': first.get('message_id'),
                    'price': first.get('cost'),
                    'error': None if ok else (first.get('error') or first.get('message')),
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
        out = out.replace('{{' + k + '}}', str(v) if v is not None else '')
    return out
