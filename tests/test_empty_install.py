"""Fresh open-source installs have no corpus and never collect by default."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bti.cli import main
from bti.db import connect
from bti.historical import load_historical_manifest
from bti.ingest import load_manifest_document
from bti.public import health
from bti.worker import run_cycle


class EmptyInstallTests(unittest.TestCase):
    def test_empty_configuration_initializes_without_network_or_sample_claims(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / "public_sources.json"
            manifest.write_text('{"sources": []}')
            (root / "historical_seeds.json").write_text('{"policy":{"max_per_cycle":1},"seeds":[]}')
            (root / "content_sources.json").write_text('{"enabled":false}')
            self.assertEqual(load_manifest_document(manifest)["sources"], [])
            self.assertEqual(load_historical_manifest(root / "historical_seeds.json"), ([], 1))
            db = root / "empty.sqlite3"
            with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected external fetch")) as fetch:
                self.assertEqual(main(["--db", str(db), "--manifest", str(manifest), "init"]), 0)
                cycle = run_cycle(db, manifest, backup_dir=None)
                self.assertEqual(cycle["source_counts"]["content_extraction"]["status"], "disabled")
                fetch.assert_not_called()
            result = health(db)
            self.assertEqual(result["counts"]["contents"], 0)
            self.assertEqual(result["counts"]["evidence"], 0)
            self.assertEqual(result["trader_quality"]["awardees_with_platform_performance"], 0)
            with connect(db) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
