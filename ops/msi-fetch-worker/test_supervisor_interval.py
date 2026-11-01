import importlib.util,json
from pathlib import Path
import tempfile
import unittest
p=Path(__file__).resolve().parents[1]/'windows_supervisor.py'
spec=importlib.util.spec_from_file_location('bti_supervisor_interval_test',p);s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
class SupervisorIntervalTests(unittest.TestCase):
 def test_original_default_stays3600(self):
  c=s.build_commands('python','fixture.db','manifest.json','cloudflared','tunnel.yml',enable_worker=True)
  self.assertEqual(c['worker'][c['worker'].index('--interval-seconds')+1],'3600')
 def test_explicit600_overrides_only_worker(self):
  default=s.build_commands('python','fixture.db','manifest.json','cloudflared','tunnel.yml',enable_worker=True)
  faster=s.build_commands('python','fixture.db','manifest.json','cloudflared','tunnel.yml',enable_worker=True,worker_interval_seconds=600)
  self.assertEqual(faster['worker'][faster['worker'].index('--interval-seconds')+1],'600')
  self.assertEqual(default['app'],faster['app']);self.assertEqual(default['tunnel'],faster['tunnel'])
 def test_runtime_override_validation(self):
  with tempfile.TemporaryDirectory() as d:
   flag=Path(d)/'worker-enabled.json'
   self.assertEqual(s.worker_interval(flag),3600)
   flag.write_text(json.dumps({'enabled':True,'interval_seconds':600}),encoding='utf-8')
   self.assertEqual(s.worker_interval(flag),600)
   for value in (True,5,86401,'600'):
    flag.write_text(json.dumps({'enabled':True,'interval_seconds':value}),encoding='utf-8')
    with self.assertRaises(ValueError):s.worker_interval(flag)
if __name__=='__main__':unittest.main(verbosity=2)
