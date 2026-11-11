"""Site-local booking validation and atomic slot ownership via a unique DB index."""
from datetime import date
from fastapi import HTTPException
from services.account_access import site_key

# Half-hour handovers, 08:00–18:00 in the delivery site's local time.
SLOT_TIMES = [f'{hour:02d}:{minute:02d}' for hour in range(8, 18) for minute in (0, 30)]


async def ensure_booking_index(db):
    await db.clients.create_index('delivery_slot_key', unique=True,
                                  partialFilterExpression={'delivery_slot_key': {'$type': 'string'}})


def validate_day(value):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, 'Delivery date must use YYYY-MM-DD')
    return value


def booking_key(record):
    day, time = record.get('delivery_date'), record.get('delivery_time')
    if day:
        validate_day(day)
    if time and time not in SLOT_TIMES:
        raise HTTPException(422, 'Choose a half-hour delivery slot between 08:00 and 17:30')
    if record.get('booking_type', 'estimated') != 'confirmed':
        return None
    if not day or not time:
        raise HTTPException(422, 'Confirmed bookings require a delivery date and time')
    site = site_key(record.get('site_location'))
    if not site or site in ('allsites', 'all'):
        raise HTTPException(422, 'Choose a delivery site before confirming a booking')
    return f'{site}|{day}|{time}'


def slot_conflict():
    return HTTPException(409, 'This delivery slot has just been booked. Choose another time.')
