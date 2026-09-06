"""
Saleslogs / Harmony BYD data importer for Shaun platform.

Accepts:
- JSON files (with `listCsv` or array of records)
- CSV files (exported from Saleslogs)
- Direct stdin or embedded payload

Usage:
    python scripts/import_saleslogs.py [path-to-json-or-csv]
"""
import sys
import os
import re
import csv
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from pymongo import MongoClient  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / '.env')

MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ.get('DB_NAME', 'test_database')

DEALERSHIP_LOCATIONS = {
    'FAIRFIELD',
    'NUNAWADING',
    'CAROLINE SPRINGS',
    'DERRIMUT HOLDING YARD',
    'DEALERSHIP',
}

TRANSIT_LOCATIONS = {
    'CLAYTON HOLDING YARD',
    'CLAYTON PD',
    'CLAYTON READY',
    'ON WATER',
    'DISPATCH',
    'IN TRANSIT',
    'VELO',
}

DELIVERED_LOCATIONS = {'DELIVERED'}


def normalise_phone(raw):
    if not raw:
        return ''
    digits = re.sub(r'[^\d+]', '', str(raw))
    if digits.startswith('+'):
        return digits
    if digits.startswith('61') and len(digits) == 11:
        return '+' + digits
    if digits.startswith('04') and len(digits) == 10:
        return '+61' + digits[1:]
    if digits.startswith('4') and len(digits) == 9:
        return '+61' + digits
    return digits


def to_iso_date(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()
    if not s or s.upper() in ('TBA', 'PENDING', 'N/A', 'NA', 'CANCELLED', 'NO'):
        return None
    # e.g. 31-Dec-25 or 28-Feb-26
    try:
        dt = datetime.strptime(s, '%d-%b-%y')
        return dt.date().isoformat()
    except Exception:
        pass
    # e.g. 2026-06-20
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # e.g. 20/06/2026 or 20-06-2026
    m = re.match(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})', s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return None


def title_case_name(s):
    if not s:
        return ''
    s = str(s).strip()
    if re.search(r'\b(PTY|LTD|GROUP|HOLDING|TRUST|ENTERPRISE|SERVICES|CHURCH|LLC|INC)\b', s, re.I):
        return s
    return ' '.join(p.capitalize() for p in re.split(r'\s+', s))


def derive_stage_and_flags(row):
    status = (row.get('Status') or '').strip().upper()
    veh_loc = (row.get('VehLocation') or row.get('Vehicle Location') or '').strip().upper()
    pdi_field = str(row.get('ETA') or row.get('ETAComp') or '').strip().upper()
    pdi_done = 'PDI' in pdi_field or 'COMPLETED' in pdi_field or 'DONE' in pdi_field
    actual = bool(to_iso_date(row.get('Actual') or row.get('Actual Delivery')))

    if status == 'CANCELLED' or veh_loc == 'CANCELLED':
        return None, False, veh_loc, 'cancelled'
    
    driver = (row.get('DriverName') or row.get("Driver's Name") or '').strip()
    client = (row.get('Client') or '').strip()
    comp = (row.get('CompanyName') or row.get('Company Name') or '').strip()
    if not driver and not client and not comp:
        return None, False, veh_loc, 'blank'

    # Delivered
    if actual or status == 'DELIVERED' or veh_loc in DELIVERED_LOCATIONS:
        return 'Delivered', True, veh_loc, None

    # Dealership locations
    if any(loc in veh_loc for loc in DEALERSHIP_LOCATIONS):
        return ('Ready for Pickup' if pdi_done else 'Pre-Delivery Inspection'), True, veh_loc, None

    # In Transit / holding yard
    if any(loc in veh_loc for loc in TRANSIT_LOCATIONS) or 'CLAYTON' in veh_loc or 'WATER' in veh_loc:
        return 'In Transit', False, veh_loc, None

    # Unallocated / Scheduled
    if status == 'NOT AVAILABLE' or veh_loc in ('NO ALLOCATION', '', '?') or 'ALLOCATED' in status:
        return 'Scheduled', False, veh_loc, None

    return 'Scheduled', False, veh_loc, None


def derive_contact_status(row, stage):
    if stage == 'Delivered':
        return 'Contacted'
    has_followup = bool((row.get('FollowCom') or row.get('Follow Up Comments') or '').strip())
    has_sales = bool((row.get('SalesCom') or row.get('Sales Comments') or '').strip())
    if has_followup:
        return 'Contacted'
    if has_sales:
        return 'Awaiting Reply'
    return 'Not Contacted'


def build_vehicle(row):
    parts = []
    veh_type = (row.get('VDType') or row.get('VEH. Type') or '').strip()
    model = (row.get('VDModel') or row.get('VEH. Model') or '').strip()
    variant = (row.get('Variant') or row.get('VEH. Variant') or '').strip()
    colour = (row.get('VDColour') or row.get('VEH. Colour') or '').strip()
    
    if veh_type:
        parts.append(f"BYD {veh_type.title()}")
    if model and model.lower() not in (veh_type.lower(),):
        parts.append(model.title())
    if variant and variant.lower() not in ('nil', 'n/a'):
        parts.append(variant.title())
    label = ' '.join(parts).strip()
    if colour and colour.lower() not in ('nil', 'n/a'):
        label = f"{label} \u00b7 {colour.title()}" if label else colour.title()
    return label or 'BYD Vehicle'


def extract_addons_and_accessories(row):
    addons = []
    accessories = []
    
    fields = [
        'Products', 'WindowTint', 'Treatment', 'Electronics',
        'ADWARRANTY', 'ADOther', 'ADOther3', 'ADOther4'
    ]
    for k, v in row.items():
        if k in fields or (k.startswith('N_Detail') and not k.endswith('_Comment')) or (k.startswith('W_Detail') and not k.endswith('_Comment')):
            if v and str(v).strip() and str(v).strip() not in ('0', '0.00', '0.0000', 'N/A', 'false', 'False'):
                val_str = str(v).strip()
                # If looks like a name or comment
                if len(val_str) > 1 and not re.match(r'^\d+(\.\d+)?$', val_str):
                    for piece in re.split(r'[\n;,/]+', val_str):
                        piece = piece.strip(' -*$"\'')
                        if piece and len(piece) > 2 and not re.match(r'^\d+(\.\d+)?$', piece):
                            addons.append(piece[:120])
                            accessories.append({
                                'id': uuid.uuid4().hex,
                                'name': piece[:120],
                                'status': 'Pending Order',
                                'note': None,
                                'updated_at': datetime.now(timezone.utc),
                            })
    seen = set()
    deduped_addons = []
    for a in addons:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            deduped_addons.append(a)
    return deduped_addons, accessories


def build_comments(row, vehicle_at):
    out = []
    sources = [
        ('SalesCom', 'Sales'),
        ('FollowCom', 'Follow-up'),
        ('FincrCom', 'Finance'),
        ('AfterCom', 'Aftercare'),
        ('ETA_Comment', 'ETA Note'),
        ('SalesCom_Comment', 'Sales Comment'),
        ('FollowCom_Comment', 'Follow-up Comment'),
    ]
    for col_name, label in sources:
        body = row.get(col_name)
        if not body:
            continue
        clean = str(body).strip()
        if not clean or clean.upper() in ('PDI COMPLETED', 'PDI DONE', 'N/A', 'NIL'):
            continue
        out.append({
            'id': uuid.uuid4().hex,
            'author_id': None,
            'author_name': f'Harmony Auto \u00b7 {label}',
            'body': clean,
            'created_at': datetime.now(timezone.utc),
        })
    if vehicle_at:
        out.append({
            'id': uuid.uuid4().hex,
            'author_id': None,
            'author_name': 'System',
            'body': f'Vehicle location at import: {vehicle_at}',
            'created_at': datetime.now(timezone.utc),
        })
    return out


def parse_csv_stream(stream):
    reader = csv.DictReader(stream)
    return list(reader)


def import_records(rows, wipe_first=False):
    client = MongoClient(MONGO_URL)
    db = client[DB_NAME]

    if wipe_first:
        wiped = db.clients.delete_many({'imported_from': {'$in': ['saleslogs-csv', 'harmony-xlsx', 'saleslogs-json']}})
        print(f"Wiped previous Saleslogs/Harmony import: {wiped.deleted_count}")

    user_map = {u.get('name', '').lower(): u for u in db.users.find({'active': True})}

    counts = {
        'inserted': 0,
        'updated': 0,
        'skipped_cancelled': 0,
        'skipped_blank': 0,
        'by_stage': {},
        'arrived': 0,
        'unassigned': 0,
    }

    for row in rows:
        name_raw = (row.get('DriverName') or row.get("Driver's Name") or 
                    row.get('Client') or row.get('CompanyName') or row.get('Company Name') or '').strip()
        if not name_raw:
            counts['skipped_blank'] += 1
            continue

        stage, arrived, vehicle_at, skip = derive_stage_and_flags(row)
        if skip:
            counts[f'skipped_{skip}'] = counts.get(f'skipped_{skip}', 0) + 1
            continue

        order_no = (str(row.get('OrderNo') or row.get('Order No.') or row.get('SaleslogsOrder') or '').strip()) or None
        if order_no and '[REMOVED]' in order_no:
            order_no = order_no.replace('[REMOVED]', '')
        phone = normalise_phone(row.get('PhoneNo') or row.get('Phone Number'))
        email = (str(row.get('Email') or '').strip().lower()) or None
        if email == '' or email == 'nil':
            email = None

        delivery_date = (to_iso_date(row.get('ETA')) or 
                         to_iso_date(row.get('Estimated')) or 
                         to_iso_date(row.get('Actual')))
        actual_delivery = to_iso_date(row.get('Actual'))
        order_date = to_iso_date(row.get('OrderDate') or row.get('Order Date'))

        sales_person = (str(row.get('SP_FULL') or row.get('SP') or row.get('Sales Person') or '').strip()) or None
        delivery_consultant = (str(row.get('AM_FULL') or row.get('AM') or row.get('Delivery Consultant') or '').strip()) or None

        assigned_agent_id = None
        if delivery_consultant:
            u = user_map.get(delivery_consultant.lower())
            if u:
                assigned_agent_id = u['id']
        if not assigned_agent_id and sales_person:
            u = user_map.get(sales_person.lower())
            if u:
                assigned_agent_id = u['id']

        addons, accessories = extract_addons_and_accessories(row)
        comments = build_comments(row, vehicle_at if not arrived else None)
        contact_status = derive_contact_status(row, stage)

        suburb = (str(row.get('Suburb') or '').strip().title()) or None
        location = None
        if suburb:
            location = suburb if 'VIC' in suburb.upper() else f"{suburb}, VIC"

        # Trade-in
        trade_in_flag = bool((row.get('TradeInType') or row.get('TradeInStockNo') or row.get('RegoTrade') or '').strip())
        trade_in_status = 'Pending' if trade_in_flag else 'Pending'

        rego = (str(row.get('Rego') or row.get('Rego No.') or '').strip()) or None
        if rego and '[REMOVED]' in rego:
            rego = rego.replace('[REMOVED]', '')
        vin = (str(row.get('Vin') or row.get('Vin No.') or '').strip()) or None
        if vin and '[REMOVED]' in vin:
            vin = vin.replace('[REMOVED]', '')

        stock_no = (str(row.get('StockNo') or row.get('Stock No.') or '').strip()) or None
        if stock_no and '[REMOVED]' in stock_no:
            stock_no = stock_no.replace('[REMOVED]', '')

        pay_type = (row.get('Pay') or row.get('DeptType') or 'Retail').strip()
        sale_type = 'Retail'
        if 'FLEET' in pay_type.upper():
            sale_type = 'Fleet'
        elif 'LEASE' in pay_type.upper() or 'NOVATED' in pay_type.upper():
            sale_type = 'Lease'
        elif 'CASH' in pay_type.upper() or pay_type == 'C':
            sale_type = 'Cash'
        elif 'DEMO' in pay_type.upper():
            sale_type = 'Demo'

        now = datetime.now(timezone.utc)
        last_contacted = now if contact_status in ('Contacted', 'Awaiting Reply') else None
        if stage == 'Delivered' and actual_delivery:
            try:
                last_contacted = datetime.fromisoformat(f'{actual_delivery}T12:00:00+00:00')
            except Exception:
                last_contacted = now

        client_doc = {
            'name': title_case_name(name_raw),
            'phone': phone or '',
            'email': email,
            'vehicle': build_vehicle(row),
            'rego': rego,
            'vin': vin,
            'delivery_date': delivery_date,
            'stage': stage,
            'salesperson': sales_person,
            'notes': None,
            'address': None,
            'location': location,
            'deal_type': pay_type or 'Retail',
            'sale_type': sale_type,
            'trade_in_flag': trade_in_flag,
            'trade_in_status': trade_in_status,
            'vy_order_id': order_no,
            'vy_stock_id': stock_no,
            'stripe_customer_id': None,
            'arrived': arrived,
            'arrived_at': now if arrived else None,
            'contact_status': contact_status,
            'last_contacted_at': last_contacted,
            'assigned_agent_id': assigned_agent_id,
            'accessories': accessories,
            'aftermarket_notes': None,
            'addons': addons,
            'imported_from': 'saleslogs-csv',
            'imported_at': now,
            'comments': comments,
            'created_at': datetime.fromisoformat(f'{order_date}T00:00:00+00:00') if order_date else now,
            'updated_at': now,
        }

        # Check existing record by order_no, vin, rego, or (email + name)
        query = []
        if order_no:
            query.append({'vy_order_id': order_no})
        if vin:
            query.append({'vin': vin})
        if rego:
            query.append({'rego': rego})
        if email and name_raw:
            query.append({'email': email, 'name': client_doc['name']})

        existing = None
        if query:
            existing = db.clients.find_one({'$or': query})

        if existing:
            client_doc['id'] = existing['id']
            db.clients.update_one({'id': existing['id']}, {'$set': client_doc})
            counts['updated'] += 1
        else:
            client_doc['id'] = uuid.uuid4().hex
            db.clients.insert_one(client_doc)
            counts['inserted'] += 1

        counts['by_stage'][stage] = counts['by_stage'].get(stage, 0) + 1
        if arrived:
            counts['arrived'] += 1
        if not assigned_agent_id:
            counts['unassigned'] += 1

    print('\n=== Saleslogs Import Summary ===')
    print(f"Total processed:    {counts['inserted'] + counts['updated']}")
    print(f"Inserted:           {counts['inserted']}")
    print(f"Updated:            {counts['updated']}")
    print(f"Skipped (cancelled):{counts.get('skipped_cancelled', 0)}")
    print(f"Skipped (blank):    {counts.get('skipped_blank', 0)}")
    print('\nBreakdown by stage:')
    for s, n in sorted(counts['by_stage'].items(), key=lambda x: -x[1]):
        print(f"  {s:30s} {n}")
    print(f"\nArrived at dealership:   {counts['arrived']}")
    print(f"Unassigned agent:        {counts['unassigned']}")
    print(f"Total clients in DB now: {db.clients.count_documents({})}")


def main():
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        file_path = sys.argv[1]
        print(f"Reading from file: {file_path}")
        if file_path.endswith('.json'):
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and 'listCsv' in data:
                    rows = parse_csv_stream(io.StringIO(data['listCsv']))
                elif isinstance(data, list):
                    rows = data
                else:
                    print("Unsupported JSON structure")
                    return
        elif file_path.endswith('.csv'):
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                rows = parse_csv_stream(f)
        else:
            print("Unsupported file format. Please provide a .csv or .json file.")
            return
        import_records(rows)
    else:
        # Check standard data path
        default_csv = Path(__file__).resolve().parents[1] / 'data' / 'saleslogs_import.csv'
        default_json = Path(__file__).resolve().parents[1] / 'data' / 'saleslogs_import.json'
        if default_csv.exists():
            with open(default_csv, 'r', encoding='utf-8', errors='replace') as f:
                rows = parse_csv_stream(f)
            import_records(rows)
        elif default_json.exists():
            with open(default_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and 'listCsv' in data:
                    rows = parse_csv_stream(io.StringIO(data['listCsv']))
                elif isinstance(data, list):
                    rows = data
                else:
                    print("Unsupported JSON structure")
                    return
            import_records(rows)
        else:
            print("No input file found. Please provide path to CSV/JSON or place in data/saleslogs_import.csv")


if __name__ == '__main__':
    main()
