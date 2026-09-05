import email
import imaplib
import os
import re
from email.message import Message


def _classify(filename: str) -> str:
    value = filename.lower()
    if 'atr' in value or 'authority' in value:
        return 'ATR signed'
    if 'licence' in value or 'license' in value:
        return 'Licence front' if 'front' in value else 'Licence back' if 'back' in value else 'Other'
    if 'eft' in value or 'refund' in value:
        return 'EFT form'
    if 'bank' in value or 'statement' in value:
        return 'Bank statement'
    if 'handover' in value or 'checklist' in value:
        return 'Handover checklist'
    return 'Other'


def fetch_unread_messages():
    host = os.environ.get('IMAP_HOST')
    username = os.environ.get('IMAP_USERNAME')
    password = os.environ.get('IMAP_PASSWORD')
    if not host or not username or not password:
        return []
    port = int(os.environ.get('IMAP_PORT', '993'))
    mailbox = os.environ.get('IMAP_FOLDER', 'INBOX')
    connection = imaplib.IMAP4_SSL(host, port)
    connection.login(username, password)
    connection.select(mailbox)
    _, result = connection.search(None, 'UNSEEN')
    messages = []
    for message_id in result[0].split():
        _, payload = connection.fetch(message_id, '(RFC822)')
        raw = payload[0][1]
        parsed = email.message_from_bytes(raw)
        sender = email.utils.parseaddr(parsed.get('From', ''))[1].lower() or None
        body_parts = []
        attachments = []
        for part in parsed.walk():
            filename = part.get_filename()
            if filename:
                attachments.append({'filename': filename, 'content_type': part.get_content_type(), 'data': part.get_payload(decode=True) or b''})
            elif part.get_content_type() == 'text/plain':
                body_parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='replace'))
        body = '\n'.join(body_parts)
        po_match = re.search(r'\b(?:INV|PO)[A-Za-z0-9_-]+\b', body, re.IGNORECASE)
        phone_match = re.search(r'\b(?:0\d{3}[ -]?\d{3}[ -]?\d{3}|\+61\s?\d{1,2}[ -]?\d{4}[ -]?\d{4})\b', body)
        messages.append({'sender': sender, 'subject': parsed.get('Subject'), 'body': body, 'po_number': po_match.group(0) if po_match else None, 'phone': phone_match.group(0) if phone_match else None, 'attachments': attachments})
        connection.store(message_id, '+FLAGS', '\\Seen')
    connection.logout()
    return messages
