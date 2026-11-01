# Optional on-demand fetch executor

This component executes one explicitly authorized signed task and exits. It is
not needed to run the dashboard. It does not discover sources, own quotas, write
the canonical database, retry blocked requests or choose fallback nodes.

The desktop owns shared policy, pacing, cooldowns and imports. The executor checks
task signatures, short expiry, replay protection, strict URL allowlists and body
bounds. Linux resource controls cap memory, CPU, descriptors and elapsed time.
The wire contract is documented in `CONTRACT.md`.

`transport.py` reads ignored `runtime/remote-executor.json`; copy its example shape
from `config/remote-executor.example.json` only when the owner authorizes a remote
executor. Set the actual SSH alias and WSL distribution locally. An unconfigured
executor fails before SSH. Never embed machine identities or service keys in Git.

The installer is an explicit optional operator action and may create a dedicated
unprivileged identity and a purpose-only signing key. It must not be run as a
dashboard setup step. Review the scoped installer and authorized destination first.
The public repository does not claim a preinstalled worker or current source access.

Offline checks:

```sh
python ops/msi-fetch-worker/test_executor.py
PYTHONPATH=src python ops/msi-fetch-worker/test_bridge.py
python ops/msi-fetch-worker/test_supervisor_interval.py
python ops/msi-fetch-worker/test_transport.py
```
