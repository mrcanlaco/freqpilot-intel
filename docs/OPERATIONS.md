# Operations

## Local files

Copy or create actual configuration under `config/` using the `*.example.json`
shapes. Actual `config/*.json` files are ignored. Bootstrap only creates missing
files; it never overwrites an existing config. Source registries are empty and
content extraction starts disabled. Review and authorize sources before changing
`enabled` or starting a collector.

Keep SQLite databases, backups, tunnel configuration/credentials, remote executor
routing and diagnostics under ignored local storage. Keep public-release history
separate from any older private operator history. Do not copy old `.git` metadata,
refs, tags, Actions logs, author email addresses or generated reports into a new
public repository.

## Public hosting

The application binds only to loopback. An operator may use the generic
`ops/cloudflared.example.yml` template for an explicitly authorized tunnel. Replace
the placeholder tunnel ID, hostname and local credential path privately. Keep the
final catch-all 404 and do not expose a whole file directory or add HTTP writes.
The supervisor uses the tunnel ID declared in its local configuration; no operator
identity is embedded in the public source.

The Windows supervisor and optional startup registration are opt-in. Review those
scripts before running them. Startup is after user login, not a guaranteed pre-login
or always-on service. Existing runtime processes are not automatically restarted
by a repository push.

## Optional remote executor

Copy `config/remote-executor.example.json` to `runtime/remote-executor.json` and set
the already-authorized SSH alias and WSL distribution locally. Missing or malformed
routing fails closed. No hostname is guessed and no alternate node is selected on
failure. The generic transport preserves batch mode, bounded timeouts, signed
task/URL binding, code-hash checks and resource limits.

Remote setup is a separate explicit operator action. Do not run the installer just
to start the local dashboard. Do not copy an existing account credential or give
the executor access to the canonical SQLite database.

## Releases

Run the offline tests and review the staged-file allowlist. Back up a live SQLite
database with the online backup API and check integrity before maintenance. Reload
only verified owned children when deployment requires it; preserve the collector,
policies, source visibility and cooldowns unless the owner authorizes a change.
Verify both localhost and public HTTPS, asset hashes, report availability, private
paths returning 404 and mutations returning 405.

For an AGPL network deployment, keep a prominent source offer pointing at the exact
version being served. The initial source link targets `v0.1.0`; update it with a
release when deploying a different version. Do not expose credentials, raw captures
or personal notes as part of the source offer. Keep an old origin reachable until
users can explicitly export/import private browser research after a domain change.
