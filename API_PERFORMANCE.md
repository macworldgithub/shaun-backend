# API response-time improvements

Read-only profiling on the application server identified three expensive paths:

- The Clients screen downloaded thousands of records before displaying 20. A 2,747-record site summary took approximately 19 seconds; MongoDB's query execution was only 26 ms. Most time was spent receiving the result, rather than serializing JSON.
- Alerts performed a separate offer lookup for each customer/offer pair and exceeded the 20-second benchmark timeout.
- Inbox polling downloaded every contact and lead and repeatedly scanned those arrays and conversation messages to infer ownership.

## Changes

`GET /api/clients/page` returns `items`, `total`, `ready_count`, `page`, `limit`, and `pages`. The default page size is 20, with a maximum of 200. Site restrictions, escaped search, stage/contact/assignment filters, and operational exception filters apply before pagination. Readiness counts include all customers in the permitted site, independent of the current search. Sorting by delivery date, creation date and ID keeps equal-date pages deterministic. Non-unique compound indexes support this order.

The existing array-based `GET /api/clients` contract remains available for integrations and bulk operations. Large bulk downloads remain proportional to the requested data size. The Clients and My Clients screens now use server pagination so they do not need those downloads. Search applies to the whole permitted dataset, including customers outside the current page. Calendar requests set `calendar=true` to fetch only scheduling and readiness fields.

Alerts read only alert-related customer fields and batch linked offers into one query. The output order and expiry rules remain unchanged.

Message history fetches owner metadata only for phones and linked customer IDs actually present in the inbox, retains legacy phone-change/correlation behavior, and projects the conversation fields it uses. Database-independent ownership joins use maps and run in a worker thread. The inbox no longer waits for the bulk-recipient list. Authentication, selected-site restrictions and ambiguity checks still run for each request.

Deal pagination reads the items and aggregate statistics concurrently and reuses the aggregate count instead of issuing a duplicate count query. Staff membership queries fetch email addresses only. Bcrypt verification and password hashing run outside the async request loop.

## Candidate measurements

Same application server and database, authenticated site-scoped reads:

| User operation | Previous | Isolated candidate |
| --- | ---: | ---: |
| Clients initial load | ~19,205 ms (2,747 records) | ~372 ms (20 records plus full counts) |
| Alerts | >20,000 ms, timed out | ~1,016 ms |
| Message history | ~15,590 ms | ~2,430 ms |
| Deal list | ~1,389 ms | ~1,125 ms |

The client comparison intentionally measures the new bounded UI request against the former whole-list request. These are observed medians, not latency guarantees. Network conditions and database load affect timings. A frozen-data comparison returned the same seven site-visible messages and fields for the original and optimized ownership join.

## Validation and deployment

- Isolated API tests cover page counts, pagination bounds, compressed responses, search/assignment/site combinations, batched alerts, ownership correlation, legacy messages and linear join work.
- The opt-in disposable MongoDB test verifies readiness filters against serialized client readiness, empty searches, site isolation and booking concurrency.
- Frontend interaction tests cover next-page navigation, search across the whole dataset, personal queues and lazy bulk-recipient loading.
- Deploy this backend before the matching frontend. Restart `byd-panel.service` to load the changes and create the non-unique pagination indexes. No dependency upgrades or record migrations are required.

Run a read-only benchmark on the server (prints timings and sizes, not tokens/customer content):

```bash
venv/bin/python scripts/benchmark_api.py --samples 3 --scope site \
  --only auth,dashboard,clients_page,calendar,alerts,staff,deals,messages \
  --output /var/tmp/shaun-api-after.json
```

To include the legacy bulk response in profiling, add `clients_summary` to `--only`. Historical integration tests that mutate live application data should remain excluded from routine regression runs.
