# Client API performance

`GET /api/clients` keeps its existing JSON array, fields, computed readiness rules,
site restrictions, and delivery-date/created-date ordering.

- MongoDB returns only fields declared by `Client`, excluding `_id` and unused
  import/provider payloads. Client detail uses the same projection.
- List retrieval requests batches of up to 1,000 records instead of relying on
  small default batches and repeated network round trips.
- Validation, direct JSON encoding, and optional gzip compression run in a worker
  thread. The large response does not perform this work on the async event loop.
- Clients advertising gzip receive compressed lists of at least 1,000 bytes.
  Encoding preferences and `Vary: Accept-Encoding` are respected; uncompressed
  clients receive the same JSON. Browsers handle decompression automatically.
- Startup creates non-unique `clients.id` and `users.id` indexes for client detail
  and authentication lookups without requiring existing records to be unique.
- Combining search with `unassigned=true` now applies both conditions.

## Pagination

The existing default remains 5,000 records for current frontends. Callers can
request smaller pages without changing the array response:

```
GET /api/clients?limit=100&offset=0
GET /api/clients?limit=100&offset=100
```

`limit` accepts 1–5,000; `offset` must be non-negative. Existing filters apply
before sorting and pagination. A short or empty page marks the end. There is no
total-count query. Offset pagination is not a snapshot: concurrently edited
delivery dates can move records between pages.

## Measurements

Local synthetic benchmark, 5,000 records, median of three runs:

- Previous validation and JSON encoding: 235.5 ms.
- Updated validation and JSON encoding: 115.2 ms (2.04× faster).
- Synthetic JSON: 8,558,891 bytes; gzip: 56,235 bytes.
- Parsed JSON responses compared equal.

A read-only sample of 200 actual database records produced 508,519 bytes of JSON
and 52,223 bytes after gzip (89.7% smaller). Validation and encoding took 18.7 ms
previously; updated encoding including compression took 14.5 ms. Generated IDs
for legacy comments missing an ID were stabilized in memory for the comparison;
no database records were modified.

These measurements are not production endpoint latency guarantees. The sampled
database transfer took 4.8 seconds from the development machine, including
connection setup. MongoDB explain reported 0–8 ms execution for 100-record list
queries. Large lists still require fetching all selected records; smaller pages
reduce that work further.

Run the synthetic benchmark without contacting a database:

```
venv/Scripts/python.exe scripts/benchmark_clients.py --rows 5000 --repeats 3
```

Deploy/restart the backend to activate the code and startup index creation. No
new environment variables or frontend deployment are required. Live records and
live indexes were not changed during local verification.
