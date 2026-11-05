# Denza Melbourne Dealer Studio import

`scripts/import_dealerstudio.py` imports a validated, complete export for dealership
`2606` / `denza-melbourne` only. The source checkout is `/var/www/dealer-studio-byd`.

The backend's configured `MONGO_URL` and `DB_NAME` are used. Full source data is
upserted into `dealerstudio_records` with `record_type=lead` or `record_type=car`,
using dealership-scoped stable IDs and source hashes. Sharing one physical
collection keeps the deployment within the Atlas collection limit. Records absent from a later export
are retained; this job does not delete database records.

Existing `clients` with `site_location=Denza Melbourne`,
`imported_from=dealer_studio` are matched by their saved `vy_order_id` (the original
Dealer Studio lead-cluster ID), then retain their Mongo/application IDs. New delivery
clients are created only for Contract Signed, Vehicle Order and Delivered source
records. All other leads are still stored in the raw lead collection. Existing
delivery clients that later become lost/cancelled are retained with their current
source status recorded. Delivered stages are never moved backwards.

Source contact/vehicle/order fields and Dealer Studio metadata are refreshed.
Notes, documents, comments, assignments, consent/opt-out flags, contact status and
staff checklists are preserved. Empty source values do not erase enriched values.
VINs are copied only through a unique source stock/registration/slug match.
Other dealerships and records from other importers are not updated.

Every live import requires a private compressed backup of the Denza Dealer Studio
clients it can edit and the raw source collection. Unrelated client records are
excluded from database reads and cannot match the write guards. Writes have optimistic concurrency guards. Verification checks
the source snapshots, unaffected records and a second client import with zero
remaining changes. Ambiguous existing IDs fail before writes.

Database reads use compressed transport and bounded, ordered `_id` pages. A lost
connection retries the incomplete page without duplicating already-read records.
Writes use batches of at most 50 operations. The runner plans inside the guarded
importer rather than downloading the same collections for a redundant dry run.
Transient database failures retry the importer up to three times, re-reading the
current state and creating a new backup each time. Validation and identity
conflicts are not retried. `database-progress.json` records the current stage and
completed batch counts without customer data.

```sh
venv/bin/python scripts/import_dealerstudio.py --input /var/www/dealer-studio-byd/.runtime/sync/data --require-fresh --dry-run
```

`dealerstudio-sync.service` and `saleslogs-sync.service` share the private
`.runtime/clients-sync.lock` using `flock`. They serialize the scheduled jobs, so
one cannot invalidate the other's cross-site verification. Session renewal has
its own lock and continues while the sync job waits for the database job lock.

The hourly runner publishes its canonical files only after the importer verifies
the write. Status, actual database counts, backups and failures are recorded under
`/var/www/dealer-studio-byd/.runtime/sync/`. Both timers survive reboots; authentication
renewal runs every five minutes independently of the hourly data import.

Verification:

```sh
venv/bin/python -m pytest tests/test_dealerstudio_upsert.py tests/test_saleslogs_department_upsert.py -q
```
