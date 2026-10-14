"""Import Virtual Yard deal and payment data from virtualyard-all-yards-all-dates.json into MongoDB.
"""
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne, ASCENDING

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / '.env')

MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ.get('DB_NAME', 'test_database')

DEFAULT_JSON_PATH = Path(r"D:\salesfloor\virtualyard-all-yards-all-dates.json")


def clean_str(val):
    if val is None:
        return ""
    return str(val).strip()


def parse_deal_line(line):
    line = line.strip()
    if not line or line in (']', '}', ']}'):
        return None
    if line.endswith(','):
        line = line[:-1]
    if not line.startswith('{'):
        return None
    try:
        raw = json.loads(line)
    except Exception:
        return None

    invoice_id = clean_str(raw.get('invoiceId'))
    if not invoice_id:
        return None

    listing = raw.get('listing') or {}
    attrs = listing.get('attributes') or {}
    columns = listing.get('columns') or {}

    # Extract clean attributes
    def first_attr(key):
        arr = attrs.get(key)
        if isinstance(arr, list) and arr:
            return clean_str(arr[0])
        return ""

    customer = clean_str(listing.get('customer')) or clean_str(columns.get('Details'))
    # Clean customer name if it has Email/Phone mashed in
    if ' Email' in customer:
        customer = customer.split(' Email')[0].strip()

    email = first_attr('Email')
    phone = first_attr('Phone')
    sold_date = first_attr('Sold')
    exp_delivery = first_attr('Exp. Delivery')
    order_no = first_attr('Order')
    deal_type = first_attr('Type')
    stock_no = first_attr('Stock#')
    rego = first_attr('Reg#')
    deal_status = first_attr('Status') or clean_str(columns.get('Status'))
    rego_expiry = first_attr('Rego Expiry')

    vehicle = clean_str(listing.get('vehicle'))
    photo = listing.get('photo') or None

    total_amount = float(listing.get('total') or 0.0)
    balance_due = float(listing.get('due') or 0.0)
    payment_total = float(raw.get('recordedPaymentTotal') or 0.0)

    # Payments list
    payments = []
    for p in raw.get('payments') or []:
        payments.append({
            'line_id': clean_str(p.get('lineId')),
            'date': clean_str(p.get('date')),
            'method': clean_str(p.get('method') or 'Unspecified'),
            'details': clean_str(p.get('details')),
            'amount': float(p.get('amount') or 0.0),
            'gst': float(p.get('gst') or 0.0),
            'account_code': clean_str(p.get('accountCode')),
            'transaction_id': clean_str(p.get('transactionId')),
        })

    # Refunds list
    refunds = []
    for r in raw.get('refunds') or []:
        vals = r.get('values') or {}
        refunds.append({
            'line_id': clean_str(vals.get('lineId')),
            'date': clean_str(vals.get('dateIncurred')),
            'details': clean_str(vals.get('details')),
            'amount': float(vals.get('totalIncGST') or 0.0),
        })

    # Summary invoice lines (optional, key items)
    invoice_lines = []
    for il in raw.get('invoiceLines') or []:
        vals = il.get('values') or {}
        line_type = clean_str(vals.get('lineType'))
        desc = clean_str(vals.get('details'))
        amt = float(vals.get('totalIncGST') or 0.0)
        ref = clean_str(vals.get('referenceNo'))
        if desc or amt:
            invoice_lines.append({
                'line_id': clean_str(vals.get('lineId')),
                'type': line_type,
                'reference_no': ref,
                'details': desc,
                'amount': amt,
            })

    # Payment status classification
    if payment_total >= total_amount and total_amount > 0:
        settlement_status = "Paid"
    elif payment_total > 0 and balance_due > 0:
        settlement_status = "Partially Paid"
    elif payment_total == 0 and total_amount > 0:
        settlement_status = "Unpaid"
    else:
        settlement_status = "Zero Balance" if balance_due == 0 else "Pending"

    payment_report = raw.get('paymentReport') or {}
    payment_report_url = payment_report.get('url') if isinstance(payment_report, dict) else None

    location_id = clean_str(raw.get('locationId'))
    location_name = clean_str(raw.get('locationName')) or "Unknown"

    proposal_number = clean_str(raw.get('proposalNumber')) or clean_str(columns.get('ID#'))

    return {
        'invoice_id': invoice_id,
        'proposal_number': proposal_number,
        'location_id': location_id,
        'location_name': location_name,
        'customer_name': customer,
        'customer_email': email or None,
        'customer_phone': phone or None,
        'vehicle': vehicle,
        'stock_no': stock_no or None,
        'rego': rego or None,
        'order_no': order_no or None,
        'deal_type': deal_type or None,
        'status': deal_status or "Unknown",
        'settlement_status': settlement_status,
        'sold_date': sold_date or None,
        'exp_delivery': exp_delivery or None,
        'rego_expiry': rego_expiry or None,
        'photo': photo,
        'deal_total': total_amount,
        'balance_due': balance_due,
        'payment_total': payment_total,
        'payment_count': len(payments),
        'payments': payments,
        'refunds': refunds,
        'invoice_lines': invoice_lines,
        'payment_report_url': payment_report_url,
        'source_url': clean_str(raw.get('sourceUrl')),
        'fetched_at': clean_str(raw.get('fetchedAt')),
        'updated_at': datetime.now(timezone.utc),
    }


def run_import(json_path=None):
    path = Path(json_path) if json_path else DEFAULT_JSON_PATH
    if not path.exists():
        print(f"Error: file not found at {path}")
        sys.exit(1)

    print(f"Connecting to MongoDB at {MONGO_URL[:25]}... DB: {DB_NAME}")
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000)
    db = client[DB_NAME]
    coll = db.virtual_yard_deals

    print("Ensuring indexes on virtual_yard_deals...")
    coll.create_index([("invoice_id", ASCENDING)], unique=True)
    coll.create_index([("proposal_number", ASCENDING)])
    coll.create_index([("location_name", ASCENDING)])
    coll.create_index([("customer_name", ASCENDING)])
    coll.create_index([("vehicle", ASCENDING)])
    coll.create_index([("status", ASCENDING)])
    coll.create_index([("settlement_status", ASCENDING)])
    coll.create_index([("sold_date", ASCENDING)])
    coll.create_index([("payment_total", ASCENDING)])
    coll.create_index([("balance_due", ASCENDING)])

    print(f"Reading deals from {path}...")
    batch = []
    total_parsed = 0
    total_upserted = 0

    with open(path, 'r', encoding='utf-8') as f:
        f.readline() # Line 0: manifest
        for line in f:
            doc = parse_deal_line(line)
            if not doc:
                continue
            total_parsed += 1
            batch.append(
                UpdateOne(
                    {"invoice_id": doc["invoice_id"]},
                    {"$set": doc},
                    upsert=True
                )
            )
            if len(batch) >= 500:
                res = coll.bulk_write(batch, ordered=False)
                total_upserted += (res.upserted_count + res.modified_count)
                print(f"  Processed {total_parsed} deals (upserted/updated: {total_upserted})...")
                batch = []

        if batch:
            res = coll.bulk_write(batch, ordered=False)
            total_upserted += (res.upserted_count + res.modified_count)
            print(f"  Processed {total_parsed} deals (upserted/updated: {total_upserted})...")

    print(f"\n[DONE] Successfully loaded {total_parsed} Virtual Yard deals into collection `virtual_yard_deals`!")
    total_in_db = coll.count_documents({})
    print(f"Total documents currently in `virtual_yard_deals`: {total_in_db}")


if __name__ == '__main__':
    run_import()
