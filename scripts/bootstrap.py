"""Create empty local configuration without fetching data or overwriting files."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    for name in ("public_sources", "content_sources", "historical_seeds"):
        target = ROOT / "config" / (name + ".json")
        if not target.exists():
            target.write_bytes((target.with_name(name + ".example.json")).read_bytes())
            print("Created", target.relative_to(ROOT))
    (ROOT / "runtime").mkdir(exist_ok=True)
    if not args.config_only:
        sys.path.insert(0, str(ROOT / "src"))
        from bti.cli import main as cli_main
        raise SystemExit(cli_main(["init"]))


if __name__ == "__main__":
    main()
