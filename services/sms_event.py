"""Provider event identities and phone matching, shared with Node SMS routes."""
import re
import json
import hashlib
import uuid
from datetime import datetime, timezone
from services.thread_history import phone_key

def phone_pattern(value):
    digits = phone_key(value)
    if not digits:
        return '^$'
    national = digits[2:] if re.fullmatch(r'61\d{9}', digits) else digits
    prefix = r'(?:61\D*|0\D*)?' if len(national) == 9 else ''
    return r'^\D*' + prefix + r'\D*'.join(national) + r'\D*$'

def inbound_id(payload, phone, text):
    explicit = payload.get('message_id') or payload.get('MessageId') or payload.get('id')
    if explicit:
        return str(explicit)
    if not payload.get('received_at'):
        return 'in-' + str(uuid.uuid4())
    event = [phone_key(phone), phone_key(payload.get('to')), str(text).strip(), str(payload['received_at']), str(payload.get('type') or 'inbound').lower(), str(payload.get('original_message_id') or '')]
    return 'in-' + hashlib.sha256(json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()

def received_at(payload):
    try:
        value = datetime.fromisoformat(str(payload.get('received_at', '')).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except ValueError:
        return datetime.now(timezone.utc)


def inbound_document_id(ident):
    return hashlib.sha256(('sms-inbound:' + ident).encode('utf-8')).hexdigest()[:24]
