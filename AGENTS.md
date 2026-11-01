# FreqPilot Intel

This is the primary public development repository. Keep application code, tests,
generic examples and public documentation here. Operator-specific configuration,
databases, source captures, research snapshots, credentials and personal notes are
not part of the repository.

## Work safely

- Use Python 3.11+ and the standard library. Keep the web server loopback-bound and
  the HTTP surface read-only; no trading, withdrawals or automatic publishing.
- Preserve source URLs, timestamps, versions and permission checks. Missing metrics
  remain unknown. Engagement and content awards do not prove trading performance.
- Stop on authentication, CAPTCHA, policy and rate-limit barriers. Preserve shared
  cooldowns across nodes; never switch routes to evade a block.
- Use explicitly synthetic fixtures for tests. Do not add real captures, private
  notes, operator logs, account identifiers or deployment credentials.
- Local production deployments may have ignored files beside this source tree.
  Do not erase or overwrite them, run bootstrap destructively, stop unknown
  processes, reset a database or clear a source hold.
- Changes to DNS, persistent runtime, remote machines or external accounts require
  the owner's task authorization. Ordinary code work does not authorize them.

## Validation

Run `python scripts/bootstrap.py --config-only`, then the Python test suite with
`PYTHONPATH=src`, both JavaScript syntax checks and `node --test tests/test_workspace.js`.
The optional executor/bridge tests are in `ops/msi-fetch-worker/` and run in CI.
Tests must not fetch external source content or start production collectors.

Before committing, inspect `git diff --cached --name-only` and scan added files for
secrets. Respect the allowlist in `.gitignore`; a force-add is not permission to
publish operator files. Keep the license and the deployed source link accurate.
