"""Compare client-list encoding without sending requests or changing databases.

Run: venv/Scripts/python.exe scripts/benchmark_clients.py --rows 5000 --repeats 3
Uses synthetic records only. Includes the previous FastAPI serialization path.
"""
import argparse
import asyncio
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.routing import serialize_response
from fastapi.utils import create_response_field
from starlette.responses import JSONResponse
from models import Client
from routers.clients import _client_list_response


async def benchmark(count, repeats):
    timestamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [{
        'id': str(i), 'name': 'Benchmark Client', 'phone': '0400000000',
        'vehicle': 'BYD Seal', 'created_at': timestamp, 'updated_at': timestamp,
        'payment_complete': True, 'pdi_complete': True,
        'registration_docs_complete': True,
    } for i in range(count)]
    field = create_response_field(name='Response', type_=List[Client], mode='serialization')
    previous, optimized = [], []
    for _ in range(repeats):
        started = perf_counter()
        clients = [Client(**row) for row in rows]
        content = await serialize_response(field=field, response_content=clients, is_coroutine=True)
        old_body = JSONResponse(content).body
        previous.append((perf_counter() - started) * 1000)
        started = perf_counter()
        new_body = _client_list_response(rows).body
        optimized.append((perf_counter() - started) * 1000)
        assert json.loads(old_body) == json.loads(new_body)
    compressed = gzip.compress(new_body, compresslevel=5)
    print(json.dumps({
        'records': count, 'repeats': repeats,
        'previous_encoding_median_ms': round(median(previous), 1),
        'optimized_encoding_median_ms': round(median(optimized), 1),
        'encoding_speedup': round(median(previous) / median(optimized), 2),
        'json_bytes': len(new_body), 'gzip_bytes': len(compressed),
        'transfer_reduction_percent': round((1 - len(compressed) / len(new_body)) * 100, 1),
        'responses_equal': True,
    }, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=5000)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.rows <= 5000 or args.repeats < 1:
        parser.error('rows must be 1..5000 and repeats must be positive')
    asyncio.run(benchmark(args.rows, args.repeats))
