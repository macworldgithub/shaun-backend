# Hourly SalesLogs sync

`data-saleslog/index.js` fetches **BYD Caroline Springs - New** and **BYD
Nunawading - New**. It uses the current month in Australia/Melbourne rather than
a fixed month. The separate Denza Melbourne department is never requested.
`scripts/import_response_all_departments.py` also excludes rows marked
`Harmony DENZA - VIC`, including any returned inside either BYD department.
Other sites and existing DENZA/dealer-studio records remain untouched.

`data-saleslog/run-hourly.js` renews the session, fetches both departments,
validates a fresh complete snapshot, runs a database dry run, creates a private
compressed backup, applies the upsert, and verifies that a second import has no
remaining changes. It publishes the canonical snapshot only after verification.
Failed or partial fetches never replace the last successful snapshot. Imports
retain record IDs, notes, documents, consent, communications and assignments;
updates use optimistic concurrency guards. Missing source records are not deleted.

## Private runtime files

Default server directory: `/var/www/shaun-backend/.runtime/saleslogs/`.

- `cookie.txt`: initial authenticated cookie, supplied privately by an operator.
- `session.json`: current server-issued token and rotated cookies, mode 0600.
- `session-status.json`: most recent authentication check, without credentials.
- `response_all_departments.json`: latest verified snapshot used for the import.
- `response_all_departments.previous.json`: previous successful snapshot.
- `last-run.json`: sync status, timestamps, counts and backup location.
- `backups/saleslogs-before-*.ejson.gz`: complete pre-import client backups.

The old tracked files under `data-saleslog/response_*` are historical files and
are not read by the scheduled job. Runtime data and credentials are ignored by
Git. Backups are retained; include this private directory in disk monitoring and
your backup retention process.

## Authentication

Configure `SALESLOGS_COOKIE_FILE=.runtime/saleslogs/cookie.txt` in the backend
`.env`, or set `SALESLOGS_COOKIE`. The environment may also override
`SALESLOGS_SESSION_FILE`, `SALESLOGS_DATA_DIR` and `SALESLOGS_PYTHON`.
Node 20.12 or later is required; no additional Node dependencies are needed.

The renewal service calls the same `/au/v1/api/GetUser` endpoint as the SalesLogs
application and persists the returned token and `Set-Cookie` changes. It can also
recover a supported HTTP-only session through SalesLogs' session-token endpoint.
It never signs a token, changes token expiry claims or bypasses MFA. A fingerprint
of the initial cookie ensures that an old seed does not overwrite a renewed
session after a restart. Replacing the seed with a fresh cookie reconnects it.

The renewal timer runs every five minutes, independently of the hourly import.
If the account is logged out/revoked or SalesLogs requires login/MFA again,
`session-status.json`/`last-run.json` report authentication required and the job
fails without importing stale data. A fresh login cookie is needed then.

## Server installation and checks

The backend lives at `/var/www/shaun-backend`; its existing Python virtualenv is
used by the importer. These units are independent of the FastAPI web service.

```sh
cd /var/www/shaun-backend
install -d -m 700 .runtime .runtime/saleslogs
# Configure the private cookie file and .env first.
install -m 644 deploy/saleslogs-*.service deploy/saleslogs-*.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/saleslogs-sync.service /etc/systemd/system/saleslogs-session.service
systemctl daemon-reload
systemctl start saleslogs-session.service
systemctl start saleslogs-sync.service
systemctl enable --now saleslogs-session.timer saleslogs-sync.timer
systemctl list-timers 'saleslogs-*'
journalctl -u saleslogs-sync.service -u saleslogs-session.service
```

Both jobs use `flock` to avoid overlapping or racing over session state. Use
`systemctl start saleslogs-sync.service` for manual production imports too.
The hourly timer survives reboots and catches up a missed run. The renewal timer
starts after boot and keeps renewing even while the web service is restarted.

Local verification (mocked transports/databases; no customer messaging):

```sh
node --test tests/saleslogs-*.test.cjs
./venv/bin/python -m pytest tests/test_saleslogs_department_upsert.py -q
```

For a fetch without a database write, run `node data-saleslog/index.js`. For a
specific historical month use `node data-saleslog/index.js 202610 --output=.runtime/saleslogs/historical.json`.
