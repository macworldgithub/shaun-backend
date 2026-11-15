# Harmony Delivery Centre further updates

Implements `Harmony_CRM_Delivery_Centre_Further_Updates.md` together with the matching Sales CRM and shared Node API updates.

- A newly recorded Delivered transition stores an immutable collection timestamp and one enrollment ID. Existing Delivered records are not backfilled. A separate worker runs each minute and repairs interrupted enrollment; the delivery request also starts due processing after responding.
- Private customers receive the supplied delivery-day thank-you, day-23 check-in and day-31 survey drafts. Day 3 creates an assigned sales CRM call reminder, not a customer SMS. Due times follow Australia/Melbourne calendar days, including daylight saving changes.
- Business/company, fleet, demo, government and rental sales are excluded. Known legacy retail/cash records can qualify when newly delivered; explicit customer type is available for new and edited clients. Existing customer/CRM opt-outs are rechecked before sending.
- MongoDB jobs have deterministic identities and atomic leases. SMS retries preserve the exact payload, sender, recipient and provider idempotency key. Expired or uncertain retries require review, and stale SMS windows are skipped rather than sending a backlog. Outgoing texts remain in the existing message history.
- Site-specific settings persist the enabled state and all four templates. Delivery managers can edit them; other staff can view them. Customer profiles show the actual job states, exclusions and SMS opt-out control.
- Day-3 reminders persist in the shared `post_delivery_followups` collection, with an assigned salesperson, customer/client links and a CRM timeline event. The Node API scopes them to the salesperson/site and saves completed call notes and text actions in customer history.

API:

```
GET /api/post-delivery/settings
PUT /api/post-delivery/settings
GET /api/post-delivery/clients/:id
```

No new credentials are required. Existing MobileMessage credentials and sender configuration are preserved. Template placeholders are `[First Name]`, `[Customer Name]`, `[Make]` and `[Model]`; vehicle make/model come from the inventory handoff or the client vehicle record.

Deploy this backend and the shared CRM API before the frontends. Preserve `.env`, uploads and rollback builds. The rollout does not enroll historical deliveries or issue a test SMS to a customer. Isolated tests and disposable MongoDB concurrency tests use a fake SMS provider; production acceptance uses opted-out synthetic records.

Provider retry contract: [MobileMessage SMS API documentation](https://mobilemessage.com.au/api-documentation). Keys are retained for 24 hours; automated retries stop before that window expires.
