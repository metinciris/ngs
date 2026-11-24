from pathlib import Path
import tempfile
import unittest
from collector import BulkCollector

class DummyDB: pass

class BrowserRecoveryTests(unittest.TestCase):
    def make(self, root):
        c=BulkCollector(Path(root),{'profile_dir':'PROFILE','headless':False,'slow_mo_ms':0},DummyDB())
        c.pw=object()
        return c

    def test_stale_lock_cleanup_preserves_profile(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'PROFILE';p.mkdir()
            (p/'SingletonLock').write_text('stale')
            (p/'SingletonCookie').write_text('stale')
            (p/'Cookies').write_text('keep')
            removed=BulkCollector._cleanup_stale_profile_locks(p)
            self.assertEqual(set(removed),{'SingletonLock','SingletonCookie'})
            self.assertTrue((p/'Cookies').exists())

    def test_launch_retries_after_stale_lock(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.make(td);p=Path(td)/'PROFILE';p.mkdir();(p/'SingletonLock').write_text('x')
            calls=[];sentinel=object()
            c._active_profile_processes=lambda profile: []
            def launch(profile):
                calls.append(1)
                if len(calls)==1: raise RuntimeError('Target page, context or browser has been closed')
                return sentinel
            c._launch_context_once=launch
            self.assertIs(c._launch_context_with_recovery(p),sentinel)
            self.assertEqual(len(calls),2)
            self.assertFalse((p/'SingletonLock').exists())

    def test_live_profile_is_not_cleaned_or_killed(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.make(td);p=Path(td)/'PROFILE';p.mkdir();(p/'SingletonLock').write_text('live')
            c._active_profile_processes=lambda profile: [1234]
            with self.assertRaisesRegex(RuntimeError,'PID: 1234'):
                c._launch_context_with_recovery(p)
            self.assertTrue((p/'SingletonLock').exists())

if __name__=='__main__':unittest.main(verbosity=2)
