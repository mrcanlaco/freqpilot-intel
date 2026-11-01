# Architecture

The application uses Python's standard library, SQLite and static HTML/JavaScript.
The Python package remains `bti`; the public distribution name is `freqpilot-intel`.

Permitted source material → bounded ingestion → versioned evidence store →
permission-checked public projection → research workspace/export.

- `ingest.py`: HTTPS allowlist, source parsing, identity checks and versioning.
- `db.py`: SQLite schema, provenance links and public-shareable flags.
- `public.py` / `research.py`: live authorization, conservative source excerpts,
  filters, pagination and auditable research fields.
- `server.py` / `static/`: loopback read-only HTTP and browser-local personal plans.
- `worker.py` / `content_volume.py`: single-writer scheduling, source gates,
  archive provenance, durable cooldowns and bounded diagnostics.
- `backup.py`: integrity-checked online backups and scoped retention.
- `campaigns.py`: allowlisted optional reviewed publication bundles. An incomplete
  bundle is unavailable; it does not open a general artifacts directory.
- `ops/msi-fetch-worker/`: optional signed on-demand executor. The desktop owns
  policy, total quotas, deduplication and every canonical database write.

Public permissions are checked when reading; authorizing one excerpt does not
authorize another claim or an entire article. Private bodies, raw captures and
operator diagnostics are not public API fields. Content and source revocation
must remain effective without restarting the app.

The worker does not infer verified profitability from engagement, self-claims or
content awards. Unknown dates, metrics and identity links remain unknown. Original
publication and discovery timestamps are kept separate. Source-access blocks are
capability gaps, not permission to change routes or invent replacement data.
