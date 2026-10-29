# Contractor app deployment

Deploy the updated `shaun-backend` and `byd-contractor-app` together. Trello migration is unchanged.

## Required secrets and media

Set `CONTRACTOR_ENCRYPTION_KEY` to a Fernet key generated with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep this value in your deployment secret manager and backups. All backend instances must use the same key. Losing it makes encrypted media and integration credentials unreadable. For rotation, `CONTRACTOR_ENCRYPTION_KEYS` accepts comma-separated keys with the new key first; retain old keys while old data remains encrypted with them.

New contractor media is encrypted before writing to local disk or S3. Other Delivery Centre document uploads retain their existing format. Downloads transparently decrypt contractor media after checking job permissions. After deploying, encrypt legacy evidence using:

```powershell
python scripts/encrypt_contractor_media.py
```

This restartable command preserves storage paths and skips already encrypted files. Legacy media remains readable during migration. Take a storage backup first. Configure `CONTRACTOR_MAX_MEDIA_BYTES` to change the default 100 MB server upload limit. The app's camera UI caps uploads at 100 MB.

## Shared Delivery Centre database

The normal deployment shares the Delivery Centre database. Leave the remote API and outbound webhook URLs empty. Job snapshots, including status milestones, time entries, checklist state, evidence links, issues, and GPS history, are stored in each client's `contractor_jobs` field. The API returns these details through the existing authorized client endpoint.

The integration worker checks for pending work every 15 seconds. Its scheduled polling interval is controlled by Settings (5–15 minutes); automatic polling can be disabled. Due-date notifications continue independently.

## Remote integrations

Set `CONTRACTOR_WEBHOOK_SECRET` to a strong random shared secret and `CONTRACTOR_PUBLIC_API_URL` to the public HTTPS backend origin. Alternatively, a Super Admin can set the webhook secret in Settings. Secrets saved in Settings are encrypted and are never returned to the browser. Blank secret fields retain existing values.

The incoming webhook is `POST /api/contractor/integrations/webhook`. Authenticate using either `Authorization: Bearer <shared-secret>` or these headers:

- `X-Contractor-Timestamp`: Unix timestamp in seconds, within five minutes.
- `X-Contractor-Signature`: `sha256=` followed by the hex HMAC-SHA256 of `<timestamp>.<raw-request-body>` using the shared secret.
- `Idempotency-Key` or JSON `event_id`: stable identity for retries.

Supported inbound request events: `client.created`, `job.request`, `delivery.scheduled`, `accessory.added`, `job.updated`, `client.updated`, due-date events, urgent events, and `template.updated`. A job request may contain full `JobCreate` data under `payload`, or a `client_id` that exists in the shared database. Duplicate events do not repeat successful mutations. Invalid or failed requests return errors so the caller can retry.

For a separate Delivery Centre instance, configure its HTTPS API origin, API key, and outbound webhook URL in Settings. The remote instance must expose the same authenticated integration contract. Polling reads `GET /api/contractor/integrations/requests` in pages; outbound full snapshots are delivered to the configured webhook. Do not configure the static frontend address as a webhook destination.

Outbound events remain in `contractor_outbox` until delivery succeeds. Failed deliveries use exponential backoff, retain stable idempotency keys, record errors, and notify administrators. The Sync dashboard supports immediate retry. Web Push requires valid VAPID configuration and the user enabling notifications on an HTTPS installed PWA.

## Access and offline behavior

Contractor admins manage workers through `/api/contractor/users`; only Super Admins can grant Admin rights. The owner-only cross-platform `/api/admin/users` controls are unchanged. Accounts shared with other platforms must still be managed centrally in Sales CRM. Site and assignment checks apply to job records, timers, attachments, history, comments, offline actions, reports, and exports.

Users must sign in and open/load their jobs online once. An unexpired session can then reopen the cached app offline. Jobs, per-task clock actions, and evidence queues persist in IndexedDB, scoped to the account. Offline media uses AES-GCM with a nonextractable device key. Sync preserves action timestamps and stops on failed prerequisites. Settings shows pending work and errors. Signing in as a different user never submits the first user's queue.

New writes are tested locally; production TLS, remote credentials, browser notification permissions, device adoption, and production end-to-end delivery must be checked after deployment.

## Verification

```powershell
python -m pytest tests/test_contractor_app.py tests/test_all_implemented_requirements.py tests/test_contractor_requirements.py tests/test_new_requirements.py -q -p no:cacheprovider
```

Run frontend `npm test`, `npm run build`, and `npm run lint`. Tests use mocked databases and transports; they do not log in to or modify live accounts.
