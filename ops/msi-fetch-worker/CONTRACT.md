# Signed fetch protocol v2

One JSON object and newline enter stdin; one bounded JSON response leaves stdout.
The caller owns authorization, scheduling, aggregate quotas and database writes.

Required task fields: `task_id`, timezone-aware `not_before` and `expires_at`, one
allowlisted `url`, `lease` and lowercase HMAC-SHA256 `signature`. The lease is
`{"issuer":"desktop","policy":"archive-static-v2","quota":1}`. Sign canonical
UTF-8 JSON of task fields other than signature, with sorted keys, compact
separators and `ensure_ascii=True`. A task expires within 120 seconds and cannot
be replayed. Do not place the key itself in a task or log.

The response binds the exact task ID and URL to observed HTTP status, bounded
body bytes/base64 and SHA-256, timestamps, TLS verification and measured runtime
constraints. Hash/length, key permissions, executor code identity and resource
bounds must all be checked before an import. No arbitrary request headers,
cookies, redirects or live-origin fallback are permitted.

HTTP access-control/rate-limit outcomes persist the same host hold across all
nodes. A failed or uncertain execution is not permission to choose another route.
Only a completed, classified I/O timeout may enter the caller's durable same-node
candidate backoff. Successful network transport does not establish source rights,
article quality or profitability. An article counts only after canonical storage
and permission-checked readback by the desktop.

Use synthetic offline tests to validate this protocol. The repository does not
assert that any particular machine has been installed or has current access.
