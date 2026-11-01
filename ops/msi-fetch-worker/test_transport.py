"""Offline regressions for operator routing configuration; no SSH is executed."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("fixture_transport", Path(__file__).with_name("transport.py"))
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class TransportConfigurationTests(unittest.TestCase):
    def test_unconfigured_executor_fails_before_subprocess(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(transport.subprocess, "run") as run:
            with patch.object(transport, "CONFIG_PATH", Path(folder) / "missing.json"):
                with self.assertRaisesRegex(RuntimeError, "not_configured"):
                    transport.verify_installation("synthetic-hash")
            run.assert_not_called()

    def test_operator_host_and_distribution_preserve_bounded_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "routing.json"
            path.write_text(json.dumps({"ssh_host": "fixture-worker", "wsl_distribution": "FixtureLinux"}))
            ssh, execute, inspect = transport.ssh_commands(path)
            self.assertEqual(ssh[-1], "fixture-worker")
            self.assertIn("BatchMode=yes", ssh)
            self.assertTrue(execute.startswith("wsl.exe -d FixtureLinux "))
            self.assertIn("--kill-after=3s 30s", execute)
            self.assertTrue(inspect.endswith("python3 -"))
            for key, invalid in (("ssh_host", "-oProxyCommand=bad"), ("wsl_distribution", "fixture; command")):
                config = {"ssh_host": "fixture-worker", "wsl_distribution": "FixtureLinux", key: invalid}
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    transport.ssh_commands(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
