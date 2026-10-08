import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

load_dotenv(Path(__file__).resolve().parents[1] / '.env')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models import Client

client = MongoClient(os.environ['MONGO_URL'])
db = client[os.environ['DB_NAME']]

updates = []
cursor = db.clients.find({'site_location': 'Denza Melbourne'})

ALLOWED_SALE_TYPES = {'Retail', 'Lease', 'Novated', 'Novated lease', 'Fleet', 'Government', 'Rental', 'Cash', 'Demo', 'Other'}
ALLOWED_TRADE_IN = {'Pending', 'Quoted', 'Accepted', 'Vehicle received', 'Settled', 'Valid', 'Expiring soon', 'Expiring', 'Expired', 'Cancelled'}
ALLOWED_STAGES = {'Scheduled', 'Pre-Delivery Inspection', 'In Transit', 'Ready for Pickup', 'Delivered'}
ALLOWED_CONTACT = {'Not Contacted', 'Contacted', 'Booked', 'Awaiting Reply'}

total = 0
for doc in cursor:
    total += 1
    set_fields = {}
    if doc.get('sale_type') not in ALLOWED_SALE_TYPES:
        set_fields['sale_type'] = 'Retail'
    if doc.get('trade_in_status') not in ALLOWED_TRADE_IN:
        set_fields['trade_in_status'] = 'Pending'
    if doc.get('stage') not in ALLOWED_STAGES:
        set_fields['stage'] = 'Scheduled'
    if doc.get('contact_status') not in ALLOWED_CONTACT:
        set_fields['contact_status'] = 'Contacted'

    if set_fields:
        updates.append(UpdateOne({'_id': doc['_id']}, {'$set': set_fields}))

print(f"Total Denza Melbourne docs: {total}, Docs needing fix: {len(updates)}")
if updates:
    res = db.clients.bulk_write(updates)
    print(f"Updated {res.modified_count} client documents to comply with Client schema.")

# Verify all docs with Client model
verification_errors = []
for doc in db.clients.find({'site_location': 'Denza Melbourne'}):
    doc.pop('_id', None)
    try:
        Client(**doc)
    except Exception as e:
        verification_errors.append(str(e))

print(f"Post-fix verification errors: {len(verification_errors)}")
if verification_errors:
    print("Sample error:", verification_errors[0])
else:
    print("All Denza Melbourne client records now validate successfully against Client model!")
