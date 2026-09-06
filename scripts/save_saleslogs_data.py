"""
Saves the Saleslogs CSV payload to disk then imports it.
Run: python scripts/save_saleslogs_data.py
"""
import sys
import os
import io
import csv
import json
import uuid
import re
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).resolve().parents[1] / '.env')

MONGO_URL = os.environ['MONGO_URL']
DB_NAME   = os.environ.get('DB_NAME', 'test_database')

# ── helpers (same as import_saleslogs.py) ─────────────────────────────────────

DEALERSHIP_LOCATIONS = {'FAIRFIELD','NUNAWADING','CAROLINE SPRINGS','DERRIMUT HOLDING YARD','DEALERSHIP'}
TRANSIT_LOCATIONS    = {'CLAYTON HOLDING YARD','CLAYTON PD','CLAYTON READY','ON WATER','DISPATCH','IN TRANSIT','VELO'}
DELIVERED_LOCATIONS  = {'DELIVERED'}

def normalise_phone(raw):
    if not raw: return ''
    d = re.sub(r'[^\d+]', '', str(raw))
    if d.startswith('+'): return d
    if d.startswith('61') and len(d) == 11: return '+'+d
    if d.startswith('04') and len(d) == 10: return '+61'+d[1:]
    if d.startswith('4')  and len(d) == 9:  return '+61'+d
    return d

def to_iso_date(v):
    if not v: return None
    s = str(v).strip()
    if not s or s.upper() in ('TBA','PENDING','N/A','NA','CANCELLED','NO','?'): return None
    try:
        return datetime.strptime(s, '%d-%b-%y').date().isoformat()
    except: pass
    try:
        return datetime.strptime(s, '%d-%b-%Y').date().isoformat()
    except: pass
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})', s)
    if m: return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return None

def title_case_name(s):
    if not s: return ''
    s = str(s).strip()
    if re.search(r'\b(PTY|LTD|GROUP|HOLDING|TRUST|ENTERPRISE|SERVICES|CHURCH|LLC|INC)\b', s, re.I):
        return s
    return ' '.join(p.capitalize() for p in re.split(r'\s+', s))

def derive_stage_and_flags(row):
    status  = (row.get('Status') or '').strip().upper()
    veh_loc = (row.get('VehLocation') or '').strip().upper()
    eta_str = str(row.get('ETA') or row.get('ETAComp') or '').strip().upper()
    pdi_done = any(x in eta_str for x in ('PDI','COMPLETED','DONE'))
    actual  = bool(to_iso_date(row.get('Actual') or ''))

    if status == 'CANCELLED' or veh_loc == 'CANCELLED':
        return None, False, veh_loc, 'cancelled'
    driver = (row.get('DriverName') or '').strip()
    client = (row.get('Client')     or '').strip()
    comp   = (row.get('CompanyName')or '').strip()
    if not driver and not client and not comp:
        return None, False, veh_loc, 'blank'
    if actual or status == 'DELIVERED' or veh_loc in DELIVERED_LOCATIONS:
        return 'Delivered', True, veh_loc, None
    if any(loc in veh_loc for loc in DEALERSHIP_LOCATIONS):
        return ('Ready for Pickup' if pdi_done else 'Pre-Delivery Inspection'), True, veh_loc, None
    if any(loc in veh_loc for loc in TRANSIT_LOCATIONS) or 'CLAYTON' in veh_loc or 'WATER' in veh_loc:
        return 'In Transit', False, veh_loc, None
    return 'Scheduled', False, veh_loc, None

def derive_contact_status(row, stage):
    if stage == 'Delivered': return 'Contacted'
    if (row.get('FollowCom') or '').strip(): return 'Contacted'
    if (row.get('SalesCom')  or '').strip(): return 'Awaiting Reply'
    return 'Not Contacted'

def build_vehicle(row):
    veh_type = (row.get('VDType')   or '').strip()
    model    = (row.get('VDModel')  or '').strip()
    variant  = (row.get('Variant')  or '').strip()
    colour   = (row.get('VDColour') or '').strip()
    parts = []
    if veh_type: parts.append(f"BYD {veh_type.title()}")
    if model and model.lower() != veh_type.lower(): parts.append(model.title())
    if variant and variant.lower() not in ('nil','n/a'): parts.append(variant.title())
    label = ' '.join(parts).strip()
    if colour and colour.lower() not in ('nil','n/a'):
        label = f"{label} \u00b7 {colour.title()}" if label else colour.title()
    return label or 'BYD Vehicle'

def build_accessories(row):
    addons, accessories = [], []
    comment_keys = {k for k in row if k and k.endswith('_Comment')}
    for k, v in row.items():
        if k in comment_keys: continue
        if not k or not (k.startswith('N_Detail') or k.startswith('W_Detail') or k in ('Products','WindowTint','Treatment','Electronics','ADWARRANTY','ADOther','ADOther3','ADOther4')):
            continue
        if not v: continue
        val = str(v).strip()
        if not val or val in ('0','0.00','0.0000','N/A','false','False',''): continue
        if re.match(r'^\d+(\.\d+)?$', val): continue
        for piece in re.split(r'[\n;,/]+', val):
            piece = piece.strip(' -*$"\'')
            if piece and len(piece) > 2 and not re.match(r'^\d+(\.\d+)?$', piece):
                addons.append(piece[:120])
                accessories.append({'id': uuid.uuid4().hex, 'name': piece[:120],
                                    'status': 'Pending Order', 'note': None,
                                    'updated_at': datetime.now(timezone.utc)})
    seen, deduped = set(), []
    for a in addons:
        if a.lower() not in seen:
            seen.add(a.lower()); deduped.append(a)
    return deduped, accessories

def build_comments(row, vehicle_at):
    out = []
    for col, label in [('SalesCom','Sales'),('FollowCom','Follow-up'),('FincrCom','Finance'),('AfterCom','Aftercare'),('ETA_Comment','ETA Note')]:
        body = (row.get(col) or '').strip()
        if not body or body.upper() in ('PDI COMPLETED','PDI DONE','N/A','NIL'): continue
        out.append({'id': uuid.uuid4().hex, 'author_id': None,
                    'author_name': f'Harmony Auto \u00b7 {label}', 'body': body,
                    'created_at': datetime.now(timezone.utc)})
    if vehicle_at:
        out.append({'id': uuid.uuid4().hex, 'author_id': None, 'author_name': 'System',
                    'body': f'Vehicle location at import: {vehicle_at}',
                    'created_at': datetime.now(timezone.utc)})
    return out

# ── main import ───────────────────────────────────────────────────────────────

def run_import(rows, wipe=True):
    mongo = MongoClient(MONGO_URL)
    db    = mongo[DB_NAME]

    if wipe:
        n = db.clients.delete_many({'imported_from': {'$in': ['saleslogs-csv','harmony-xlsx','saleslogs-json']}}).deleted_count
        print(f"Wiped {n} previous Harmony/Saleslogs records")

    # Pre-populate / find users and create missing sales agents
    user_map = {u.get('name','').strip().lower(): u for u in db.users.find({'active': True})}
    
    # Auto-provision any active sales agents from CSV if missing
    for row in rows:
        sp_full = (row.get('SP_FULL') or '').strip()
        if sp_full and sp_full.lower() not in ('no opportunity', 'house', 'fleet', 'n/a', 'none', '') and sp_full.lower() not in user_map:
            email_slug = re.sub(r'[^a-zA-Z0-9]', '.', sp_full.lower().strip())
            new_user_id = str(uuid.uuid4())
            new_user_doc = {
                'id': new_user_id,
                'name': sp_full,
                'email': f"{email_slug}@harmonyauto.com.au",
                'role': 'agent',
                'active': True,
                'created_at': datetime.now(timezone.utc),
                'must_change_password': True,
                'password_hash': '$2b$12$e8Y6bFj63Q3q4lQfE9xHPe5lY2x1Z2z1a2b3c4d5e6f7g8h9i0j.' # placeholder hash
            }
            db.users.insert_one(new_user_doc)
            user_map[sp_full.lower()] = new_user_doc
            print(f"Created agent user: {sp_full} ({new_user_doc['email']})")

    counts = dict(inserted=0, updated=0, skipped_cancelled=0, skipped_blank=0,
                  by_stage={}, arrived=0, unassigned=0)
    now = datetime.now(timezone.utc)

    for row in rows:
        name_raw = (row.get('DriverName') or row.get('Client') or row.get('CompanyName') or '').strip()
        if not name_raw:
            counts['skipped_blank'] += 1; continue

        stage, arrived, vehicle_at, skip = derive_stage_and_flags(row)
        if skip:
            counts[f'skipped_{skip}'] = counts.get(f'skipped_{skip}', 0) + 1; continue

        def clean(v):
            s = str(v or '').strip()
            return None if not s or '[REMOVED]' in s else s.replace('[REMOVED]','')

        order_no  = clean(row.get('OrderNo') or row.get('SaleslogsOrder'))
        phone     = normalise_phone(row.get('PhoneNo'))
        email     = (str(row.get('Email') or '').strip().lower()) or None
        if email in ('', 'nil'): email = None

        delivery_date  = to_iso_date(row.get('ETA')) or to_iso_date(row.get('Estimated')) or to_iso_date(row.get('Actual'))
        actual_delivery= to_iso_date(row.get('Actual'))
        order_date     = to_iso_date(row.get('OrderDate'))

        sp_full  = (row.get('SP_FULL') or row.get('SP') or '').strip() or None
        am_full  = (row.get('AM_FULL') or row.get('AM') or '').strip() or None

        assigned_agent_id = None
        for name in filter(None, [sp_full, am_full]):
            u = user_map.get(name.lower())
            if u: assigned_agent_id = u['id']; break

        addons, accessories = build_accessories(row)
        comments            = build_comments(row, vehicle_at if not arrived else None)
        contact_status      = derive_contact_status(row, stage)

        suburb   = str(row.get('Suburb') or '').strip().title() or None
        location = f"{suburb}, VIC" if suburb else None

        pay = (row.get('Pay') or 'Retail').strip()
        sale_type = ('Fleet' if 'FLEET' in pay.upper() else
                     'Lease' if 'LEASE' in pay.upper() or 'NOVATED' in pay.upper() else
                     'Cash'  if 'CASH'  in pay.upper() or pay == 'C' else
                     'Demo'  if 'DEMO'  in pay.upper() else 'Retail')

        trade_in_flag = bool((row.get('TradeInType') or row.get('RegoTrade') or '').strip())

        last_contacted = now if contact_status in ('Contacted','Awaiting Reply') else None
        if stage == 'Delivered' and actual_delivery:
            try: last_contacted = datetime.fromisoformat(f'{actual_delivery}T12:00:00+00:00')
            except: last_contacted = now

        doc = {
            'name': title_case_name(name_raw),
            'phone': phone or '',
            'email': email,
            'vehicle': build_vehicle(row),
            'rego': clean(row.get('Rego')),
            'vin':  clean(row.get('Vin')),
            'delivery_date': delivery_date,
            'stage': stage,
            'salesperson': sp_full,
            'notes': None, 'address': None, 'location': location,
            'deal_type': pay or 'Retail',
            'sale_type': sale_type,
            'trade_in_flag': trade_in_flag,
            'trade_in_status': 'Pending',
            'vy_order_id': order_no,
            'vy_stock_id': clean(row.get('StockNo')),
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
            'registration_status': 'Awaiting registration documents',
            'registration_docs_complete': False,
            'handover_checklist_status': 'Not issued',
            'activation_ready': False,
            'activation_status': 'Blocked',
            'offer_status': 'Eligible',
            'document_completeness': 'Requested',
            'documents': [],
            'linked_offer_ids': [],
            'created_at': datetime.fromisoformat(f'{order_date}T00:00:00+00:00') if order_date else now,
            'updated_at': now,
        }

        # Deduplicate by vin / rego / order_no / (email+name)
        query = []
        if doc['vin']:      query.append({'vin': doc['vin']})
        if doc['rego']:     query.append({'rego': doc['rego']})
        if order_no:        query.append({'vy_order_id': order_no})
        if email and doc['name']: query.append({'email': email, 'name': doc['name']})

        existing = db.clients.find_one({'$or': query}) if query else None
        if existing:
            doc['id'] = existing['id']
            db.clients.update_one({'id': existing['id']}, {'$set': doc})
            counts['updated'] += 1
        else:
            doc['id'] = uuid.uuid4().hex
            db.clients.insert_one(doc)
            counts['inserted'] += 1

        counts['by_stage'][stage] = counts['by_stage'].get(stage, 0) + 1
        if arrived:   counts['arrived']   += 1
        if not assigned_agent_id: counts['unassigned'] += 1

    print('\n=== Saleslogs Import Complete ===')
    print(f"Inserted : {counts['inserted']}")
    print(f"Updated  : {counts['updated']}")
    print(f"Cancelled: {counts.get('skipped_cancelled',0)}")
    print(f"Blank    : {counts.get('skipped_blank',0)}")
    print('\nBy stage:')
    for s, n in sorted(counts['by_stage'].items(), key=lambda x: -x[1]):
        print(f"  {s:<32} {n}")
    print(f"\nArrived at dealership : {counts['arrived']}")
    print(f"Unassigned agent      : {counts['unassigned']}")
    print(f"Total clients in DB   : {db.clients.count_documents({})}")
    mongo.close()


if __name__ == '__main__':
    data_path = Path(__file__).resolve().parents[1] / 'data' / 'saleslogs_import.csv'
    if not data_path.exists():
        print(f"ERROR: data file not found at {data_path}")
        sys.exit(1)
    print(f"Reading {data_path} ...")
    with open(data_path, 'r', encoding='utf-8', errors='replace') as f:
        rows = list(csv.DictReader(f))
    print(f"Parsed {len(rows)} rows from CSV")
    run_import(rows, wipe=True)
