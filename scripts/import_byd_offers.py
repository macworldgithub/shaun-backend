"""Import the current BYD Fairfield offers-page catalogue into MongoDB.

The source page contains multiple promotions for the same vehicle variant, so each
promotion is kept as a separate offer record. The stable source_key makes this
script safe to rerun: existing imported offers are updated in place.

Run from shaun-backend:
    python scripts/import_byd_offers.py
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / '.env')

MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ.get('DB_NAME', 'test_database')
SOURCE_URL = os.environ.get('OFFERS_PAGE_URL', 'https://bydfairfield.com.au/offers')


def offer(
    source_key,
    name,
    model,
    variant,
    promotion,
    value,
    unit,
    order_from,
    order_to,
    deliver_by,
    image_url,
    spec,
    *,
    body='SUV',
    powertrain='Electric',
    cash_or_product='cash',
    exclusions=None,
    claim_docs=None,
):
    return {
        'source_key': source_key,
        'name': name,
        'model_variant': variant,
        'body_style': body,
        'powertrain': powertrain,
        'display_value': value,
        'display_unit': unit,
        'image_url': image_url,
        'configurator_url': f'https://bydfairfield.com.au/configurator/{model.lower().replace(" ", "-")}?spec={spec}',
        'public_url': SOURCE_URL,
        'source_url': SOURCE_URL,
        'eligible_models': [model],
        'order_from': order_from,
        'order_to': order_to,
        'deliver_by': deliver_by,
        'honour_if_delayed': True,
        'sale_type_exclusions': exclusions or [],
        'combinable': False,
        'claim_doc_templates': claim_docs or [],
        'cash_or_product': cash_or_product,
        'internal_notes': promotion,
        'active': True,
    }


OFFERS = [
    offer('sealion-8-dynamic-fwd-driveaway-2026-08', 'BYD SEALION 8 Dynamic FWD - Driveaway Campaign', 'SEALION 8', 'Dynamic FWD', 'Driveaway Campaign', '$56,990', 'driveaway', '2026-08-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py_4z_dx-bRdMJa6wrONK1LKVUHUYzfHPWukebdKiwH5vA&m=3053-3055-3057-3114&t=colour&o=1', '3053-3055-3057-3114', powertrain='Hybrid', exclusions=['Demo'], claim_docs=[]),
    offer('sealion-8-dynamic-awd-cashback-2026-08', 'BYD SEALION 8 Dynamic AWD - $2,000 Cashback', 'SEALION 8', 'Dynamic AWD', '$2,000 Cashback Offer', '$2,000', 'cashback', '2026-08-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py_4z_dx-bRdMJa6wrONK1LKVUHUYzfHPWukebdKiwH5vA&m=3054-3055-3113-3114&t=colour&o=1', '3054-3055-3113-3114', powertrain='Hybrid', exclusions=['Fleet', 'Demo'], claim_docs=['EFT form']),
    offer('sealion-8-premium-awd-cashback-2026-08', 'BYD SEALION 8 Premium AWD - $2,000 Cashback', 'SEALION 8', 'Premium AWD', '$2,000 Cashback Offer', '$2,000', 'cashback', '2026-08-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py_4z_dx-bRdMJa6wrONK1LKVUHUYzfHPWukebdKiwH5vA&m=3126-3055-3113-3114&t=colour&o=1', '3126-3055-3113-3114', powertrain='Hybrid', exclusions=['Fleet', 'Demo'], claim_docs=['EFT form']),
    offer('sealion-7-premium-driveaway-2026-08', 'BYD SEALION 7 Premium - Driveaway Campaign', 'SEALION 7', 'Premium', 'Driveaway Campaign', '$54,990', 'driveaway', '2026-08-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py91Yu62ToCHM4BCUs4pWikXy2RkCoc7XsKE_4aozjk_vQ&m=1773-1775-1764-1763&t=colour&o=1', '1773-1775-1764-1763', exclusions=['Demo']),
    offer('sealion-7-premium-novated-2026-09', 'BYD SEALION 7 Premium - 5% Novated Lease', 'SEALION 7', 'Premium', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py91Yu62ToCHM4BCUs4pWikXy2RkCoc7XsKE_4aozjk_vQ&m=1773-1775-1764-1763&t=colour&o=1', '1773-1775-1764-1763', exclusions=['Fleet', 'Demo']),
    offer('sealion-7-performance-novated-2026-09', 'BYD SEALION 7 Performance - 5% Novated Lease', 'SEALION 7', 'Performance', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py91Yu62ToCHM4BCUs4pWikXy2RkCoc7XsKE_4aozjk_vQ&m=1765-1775-1777-1763&t=colour&o=1', '1765-1775-1777-1763', exclusions=['Fleet', 'Demo']),
    offer('atto-2-premium-your-way-2026-08', 'BYD ATTO 2 Premium - $2,000 Your Way', 'ATTO 2', 'Premium', '$2,000 Your Way', '$2,000', 'your way', '2026-08-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py_5sQw3jmGLs9mbUNdigkbk28RCq_kfuSo8Oe5VAYYF1w&m=2786-2784-2725-2724&t=colour&o=1', '2786-2784-2725-2724', cash_or_product='both', exclusions=['Demo', 'Fleet', 'Government', 'Rental'], claim_docs=['EFT form']),
    offer('atto-2-dynamic-novated-2026-09', 'BYD ATTO 2 Dynamic - 5% Novated Lease', 'ATTO 2', 'Dynamic', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py_5sQw3jmGLs9mbUNdigkbk28RCq_kfuSo8Oe5VAYYF1w&m=2785-2784-2782-2724&t=colour&o=1', '2785-2784-2782-2724', exclusions=['Fleet', 'Demo']),
    offer('atto-2-premium-novated-2026-09', 'BYD ATTO 2 Premium - 5% Novated Lease', 'ATTO 2', 'Premium', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py_5sQw3jmGLs9mbUNdigkbk28RCq_kfuSo8Oe5VAYYF1w&m=2786-2784-2725-2724&t=colour&o=1', '2786-2784-2725-2724', exclusions=['Fleet', 'Demo']),
    offer('shark-6-dynamic-cab-chassis-your-way-2026-08', 'BYD SHARK 6 Dynamic Cab Chassis - $3,000 Your Way', 'SHARK 6', 'Dynamic Cab Chassis', '$3,000 Your Way', '$3,000', 'your way', '2026-08-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py-IScQRmfchZwZyvZJqoS80bDL_U_FXiUrF15krMx1qFA&m=4368-1094-1022-6199-6200&t=colour&o=1', '4368-1094-1022-6199-6200', body='Utility', powertrain='Hybrid', cash_or_product='both', exclusions=['Demo', 'Fleet', 'Government', 'Rental'], claim_docs=['EFT form']),
    offer('shark-6-dynamic-cab-chassis-driveaway-2026-09', 'BYD SHARK 6 Dynamic Cab Chassis - $57,900 Driveaway', 'SHARK 6', 'Dynamic Cab Chassis', 'Driveaway Campaign', '$57,900', 'driveaway', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py-IScQRmfchZwZyvZJqoS80bDL_U_FXiUrF15krMx1qFA&m=4368-1094-1022-6199-6200&t=colour&o=1', '4368-1094-1022-6199-6200', body='Utility', powertrain='Hybrid', exclusions=['Demo']),
    offer('shark-6-premium-driveaway-2026-09', 'BYD SHARK 6 Premium - $57,900 Driveaway', 'SHARK 6', 'Premium', 'Driveaway Campaign', '$57,900', 'driveaway', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py-IScQRmfchZwZyvZJqoS80bDL_U_FXiUrF15krMx1qFA&m=1030-1094-1035-1021-6201&t=colour&o=1', '1030-1094-1035-1021-6201', body='Utility', powertrain='Hybrid', exclusions=['Demo']),
    offer('atto-1-essential-novated-2026-09', 'BYD ATTO 1 Essential - 5% Novated Lease', 'ATTO 1', 'Essential', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py-bIT9WHGLX5piNaoZXVbd9MEwW_oqKxdF7XTgTr3uB-w&m=2826-2828-2835-2832&t=colour&o=1', '2826-2828-2835-2832', body='Hatchback', exclusions=['Fleet', 'Demo']),
    offer('atto-1-premium-novated-2026-09', 'BYD ATTO 1 Premium - 5% Novated Lease', 'ATTO 1', 'Premium', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py-bIT9WHGLX5piNaoZXVbd9MEwW_oqKxdF7XTgTr3uB-w&m=2827-2828-3675-2832&t=colour&o=1', '2827-2828-3675-2832', body='Hatchback', exclusions=['Fleet', 'Demo']),
    offer('dolphin-essential-novated-2026-09', 'BYD DOLPHIN Essential - 5% Novated Lease', 'DOLPHIN', 'Essential', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py8aZSn57yQKVWg4rXRg6DPvp2kPAHNu3I6xyEqIl92h7w&m=1377-1380-1383-1450&t=colour&o=1', '1377-1380-1383-1450', exclusions=['Fleet', 'Demo']),
    offer('dolphin-premium-novated-2026-09', 'BYD DOLPHIN Premium - 5% Novated Lease', 'DOLPHIN', 'Premium', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py8aZSn57yQKVWg4rXRg6DPvp2kPAHNu3I6xyEqIl92h7w&m=1376-1380-1443-1450&t=colour&o=1', '1376-1380-1443-1450', exclusions=['Fleet', 'Demo']),
    offer('seal-premium-novated-2026-09', 'BYD SEAL Premium - 5% Novated Lease', 'SEAL', 'Premium', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py9nqtQGg7vpZKxjfPDvpvixmiRl-eQBsCTCcvEHWZ8SFQ&m=1635-1633-1630-1645&t=colour&o=1', '1635-1633-1630-1645', body='Sedan', exclusions=['Fleet', 'Demo']),
    offer('seal-performance-novated-2026-09', 'BYD SEAL Performance - 5% Novated Lease', 'SEAL', 'Performance', '5% Novated Lease Offer', '5%', 'discount', '2026-09-01', '2026-09-30', '2026-10-31', 'https://dealers.virtualyard.com.au/api/design/composit.php?key=U4_4GsGXOaVPZ7k3Y9PP2_mB048u-PkleWzR5wt2py9nqtQGg7vpZKxjfPDvpvixmiRl-eQBsCTCcvEHWZ8SFQ&m=1643-1633-1862-1645&t=colour&o=1', '1643-1633-1862-1645', body='Sedan', exclusions=['Fleet', 'Demo']),
]


def main():
    client = MongoClient(MONGO_URL)
    collection = client[DB_NAME].offers
    now = datetime.now(timezone.utc)
    for item in OFFERS:
        item['updated_at'] = now
        result = collection.update_one(
            {'source_key': item['source_key']},
            {'$set': item, '$setOnInsert': {'id': uuid.uuid4().hex, 'created_at': now}},
            upsert=True,
        )
        action = 'inserted' if result.upserted_id else 'updated'
        print(f'{action}: {item["name"]}')
    collection.create_index('source_key', unique=True, sparse=True)
    print(f'Imported {len(OFFERS)} BYD Fairfield offers into {DB_NAME}.offers')
    client.close()


if __name__ == '__main__':
    main()
