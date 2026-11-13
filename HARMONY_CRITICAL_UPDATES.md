# Harmony Delivery Centre critical updates

Implements the requirements in `Harmony_Group_Delivery_Centre_Critical_Updates.md`.

| Requirement | Backend behavior |
| --- | --- |
| Specialist / Consultant accounts | `delivery_specialist` has the manager permissions of `admin`; `delivery_consultant` retains operational access. Existing owner-only account mutation rules remain enforced. Managers can read Team & Access. |
| Calendar day view and daily counts | Lightweight, date-filtered client summaries expose booking type and site-local delivery time. The frontend opens the entire selected day. |
| Estimated / confirmed bookings | `booking_type` accepts `estimated` or `confirmed`. Legacy bookings default to estimated. Confirmed bookings require a site, date and time. |
| Specific time slots | `GET /api/clients/available-slots?day=YYYY-MM-DD&site_location=...` returns half-hour slots from 08:00 to 17:30. Estimated bookings do not reserve capacity. One confirmed booking is allowed per site/date/time. |
| Simultaneous bookings | A partial unique index on `delivery_slot_key` enforces slot ownership atomically. Site aliases share the same key. Conflicts return HTTP 409; moving/demoting a booking releases its slot. Concurrent edits to the same booking use compare-and-set. |
| Incoming text responses | Delivery receipts are distinguished from replies even when they contain sender/message fields. Forwarded replies remain idempotent and available when Lead Centre is unconfigured or unavailable. Selected-site access applies to SMS history. |
| Reduced-access loading | Dashboard counts use one aggregation, previews are bounded, and list summaries exclude documents/comments/provider data. Optional shared-message lookups have timeouts. |

The frontend adds searchable Team & Access, booking controls in customer creation/detail, clickable date summaries, day customer lists, available slots, and lazy loading of route pages.

## Deployment

1. Deploy this backend before the matching frontend: the UI uses summary responses, available slots, and the additional dashboard preview fields.
2. Restart the backend normally. Startup creates the required unique booking index. Check startup logs for index errors before accepting bookings.
3. Keep `MOBILEMESSAGE_FROM` / `MOBILEMESSAGE_SENDER_ID` set to Harmony's requested sender, `+61408622037`. Do not replace it with an alphanumeric sender if customer replies are expected.
4. Retain the existing handset forwarding for this own-number sender to `https://byd-panel.omnisuiteai.com/api/messages/inbound`. Forwarded JSON should identify the customer in `sender`, `from`, or `phone`, and include `message`, `body`, or `text`. Include a stable `message_id` or `received_at` for deduplication and `original_message_id` when available for site correlation.
5. Keep the provider inbound/status webhooks pointing to that endpoint. An own-number SMS sender relies on handset forwarding for inbound replies; the provider does not receive those handset replies directly.

No customer records are migrated or automatically elevated to manager access. Existing records remain estimated until staff confirm them.

## Verification

- Isolated backend API regressions cover booking creation/validation, slot conflicts, rescheduling, site locks, summary payloads, role permissions, receipt handling, forwarded replies, and shared-history outages.
- Opt-in MongoDB integration verifies simultaneous confirmation, reservation release, dashboard site isolation and summary payload reduction against a new disposable database. Only its generated fixture collections are removed afterward.
- Frontend interaction tests cover calendar day/detail navigation, summary counts, empty days, available/occupied slots, booking validation, conflict recovery and cross-page team search.

```powershell
# Isolated regressions (exclude historical tests that connect to live application data)
venv/Scripts/python.exe -m pytest tests -q --ignore=tests/test_harmony_imports.py --ignore=tests/test_site_filtering.py --ignore=tests/test_virtualyard.py

# Opt-in disposable MongoDB test
$env:RUN_DELIVERY_MONGO_TESTS='1'
venv/Scripts/python.exe -m pytest tests/test_delivery_critical_mongo.py -q -s

# Inspect the real SMS test without sending another message
venv/Scripts/python.exe scripts/smoke_delivery_sms.py --to +61499918523 --sender +61408622037 --check --reference harmony-critical-updates-own-sender
```

On 11 November 2026, MobileMessage reported `delivered` for the authorized test from `+61408622037` to `+61499918523` (message ID `bb6a4125-3932-4b19-ac8d-de17e0397744`). A real `HARMONY OK` handset reply must be observed to complete live forwarding verification; simulated webhook regressions are separate evidence.

## CRM client-card handoff follow-up

`GET /api/clients/:id/sales-chat?before=<message-id>` opens earlier retained messages in pages of 100. The response includes `has_more`; invalid cursors fail without writes. Handoff notification filters include dealership aliases (for example Melbourne and BYD Melbourne City VIC) within the selected site. Shared structured Sold fields and attributed internal chat remain in the existing customer record.
