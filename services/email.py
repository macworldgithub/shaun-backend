import asyncio
import os
import smtplib
from email.message import EmailMessage


async def send_email(
    *,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
):
    host = os.environ.get('SMTP_HOST')
    username = os.environ.get('SMTP_USERNAME')
    password = os.environ.get('SMTP_PASSWORD')
    sender = os.environ.get('SMTP_FROM') or username
    port = int(os.environ.get('SMTP_PORT', '587'))
    use_ssl = os.environ.get('SMTP_SSL', 'false').lower() == 'true'
    if not host or not sender:
        raise RuntimeError('SMTP_HOST and SMTP_FROM/SMTP_USERNAME must be configured')

    message = EmailMessage()
    message['From'] = sender
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(body)
    for filename, content, content_type in attachments or []:
        maintype, subtype = (content_type.split('/', 1) + ['octet-stream'])[:2]
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    def deliver():
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as connection:
                if username and password:
                    connection.login(username, password)
                connection.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as connection:
                connection.ehlo()
                connection.starttls()
                connection.ehlo()
                if username and password:
                    connection.login(username, password)
                connection.send_message(message)

    await asyncio.to_thread(deliver)
