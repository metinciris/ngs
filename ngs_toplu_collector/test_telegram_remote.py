from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from remote_control import RemoteAppBridge, queue_remote_command, read_app_status, app_status_fresh
import telegram_remote_agent as agent
from collector import BulkCollector


class BrowserStub:
    def __init__(self): self.stops=[]
    def request_stop(self, immediate=False): self.stops.append(immediate)


class AppStub:
    def __init__(self):
        self.browser_ready=False;self.job_busy=False;self.current_command='';self.runs_scanned=False
        self.browser=BrowserStub();self.calls=[];self.timers=[]
    def after(self, ms, fn, *args): self.timers.append((ms,fn,args));return str(len(self.timers))
    def _submit(self, command, **kwargs):
        if self.job_busy:return False
        self.calls.append((command,kwargs));self.current_command=command;return True


class RemoteBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.app=AppStub()
        self.bridge=RemoteAppBridge(self.app,self.base,{'runs_url':'https://beta.gennext.bio/runs'},'0.4.50')
    def tearDown(self): self.temp.cleanup()
    def test_scan_starts_browser_with_auto_login_then_scan(self):
        status,msg=self.bridge.request_scan()
        self.assertEqual(status,'accepted');self.assertTrue(self.bridge.pending_scan)
        self.assertEqual(self.app.calls,[('open_browser',{'auto_login':True})])
        self.app.browser_ready=True
        self.bridge.on_job_done({'command':'open_browser','status':'completed'})
        # last timer is _start_pending_scan
        _,fn,args=self.app.timers[-1];fn(*args)
        self.assertEqual(self.app.calls[-1],('scan_runs',{}))
        self.assertEqual(self.bridge.last_event['kind'],'scan_started')
    def test_scan_when_browser_ready_still_preflights_login(self):
        self.app.browser_ready=True
        status,_=self.bridge.request_scan()
        self.assertEqual(status,'accepted');self.assertEqual(self.app.calls[-1],('open_browser',{'auto_login':True}))
    def test_stop_is_graceful_and_only_for_scan(self):
        self.app.job_busy=True;self.app.current_command='scan_runs'
        status,_=self.bridge.request_stop()
        self.assertEqual(status,'accepted');self.assertEqual(self.app.browser.stops,[False])
        self.app.current_command='igv_capture'
        status,_=self.bridge.request_stop();self.assertEqual(status,'idle')
    def test_status_has_no_patient_fields(self):
        payload=self.bridge.status_payload()
        text=json.dumps(payload).casefold()
        for word in ('patient','hasta','diagnosis','tani','mp123','pool_name','national_id'):
            self.assertNotIn(word,text)
    def test_command_file_processed_once(self):
        self.app.browser_ready=True
        command_id=queue_remote_command(self.base,'scan_runs',user_id=1196743952)
        path=next((self.base/'DATA'/'REMOTE_CONTROL'/'commands').glob('*.json'))
        self.bridge._process_command_file(path)
        self.assertFalse(path.exists())
        result=json.loads((self.base/'DATA'/'REMOTE_CONTROL'/'results'/(command_id+'.json')).read_text())
        self.assertEqual(result['status'],'accepted')
    def test_completion_event_contains_count_only(self):
        self.bridge.remote_active=True
        self.bridge.on_job_done({'command':'scan_runs','status':'completed','technical_records':12,'pool_name':'SHOULD_NOT_LEAK'})
        status=read_app_status(self.base)
        self.assertEqual(status['last_event']['technical_records'],12)
        self.assertNotIn('pool',json.dumps(status).casefold())


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old_base=agent.BASE;self.old_root=agent.ROOT
        agent.BASE=Path(self.temp.name);agent.ROOT=agent.remote_root(agent.BASE)
    def tearDown(self): agent.BASE=self.old_base;agent.ROOT=self.old_root;self.temp.cleanup()
    def test_runs_command_queues_and_launches(self):
        sent=[];queued=[]
        cfg={'allowed_user_id':1196743952,'allowed_chat_id':1196743952}
        with patch.object(agent,'send_message',side_effect=lambda *a,**k:sent.append(a[2])), \
             patch.object(agent,'queue_remote_command',side_effect=lambda *a,**k:queued.append((a,k)) or 'x'), \
             patch.object(agent,'launch_ngs',return_value=True):
            agent.handle_authorized('TOKEN',cfg,{'chat':{'id':1196743952},'text':'/runs_tara'})
        self.assertEqual(queued[0][0][1],'scan_runs');self.assertIn('NGS aciliyor',sent[0])
    def test_status_text_never_reads_patient_db(self):
        root=agent.remote_root(agent.BASE);root.mkdir(parents=True,exist_ok=True)
        from remote_control import _atomic_json
        _atomic_json(root/'app_status.json',{'app_running':True,'heartbeat_at':__import__('time').time(),'version':'0.4.50','browser_ready':True,'job_busy':True,'current_command':'scan_runs','runs_scanned':False,'last_event':{}})
        text=agent.status_text()
        self.assertIn('RUNS taramasi calisiyor',text)
        self.assertNotIn('MP',text)
    def test_text_buttons_map_to_commands(self):
        self.assertEqual(agent._message_command('▶ RUNS Tara'),'/runs_tara')
        self.assertEqual(agent._message_command('/runs_tara@enlilko_bot'),'/runs_tara')


class FakeLocator:
    def __init__(self,page=None,kind='generic',value='',count=1):
        self.page=page;self.kind=kind;self.value=value;self.n=count;self.clicked=False;self.focused=False
    @property
    def first(self):return self
    def count(self):return self.n
    def input_value(self,timeout=None):return self.value
    def focus(self,timeout=None):self.focused=True
    def click(self,timeout=None,force=False):
        self.clicked=True
        if self.kind=='button' and self.page and self.page.submit_succeeds:self.page.url='https://beta.gennext.bio/runs?page=1'
    def evaluate(self,script):
        if self.kind in ('button','form') and self.page and self.page.submit_succeeds:self.page.url='https://beta.gennext.bio/runs?page=1'

class FakePage:
    def __init__(self,user='metin',password='secret',submit_succeeds=True,button_count=1):
        self.url='https://beta.gennext.bio/auth';self.submit_succeeds=submit_succeeds;self.goto_calls=[]
        self.user=FakeLocator(self,'user',user);self.password=FakeLocator(self,'password',password)
        self.button=FakeLocator(self,'button',count=button_count);self.form=FakeLocator(self,'form')
    def locator(self,selector):
        if selector=='#kt_sign_in_submit':return self.button
        if selector=='#kt_login_signin_form':return self.form
        if 'input[name="password"]' in selector or 'password' in selector:return self.password
        if 'input[name="email"]' in selector or 'email' in selector or 'text' in selector:return self.user
        if 'button[type="submit"]' in selector:return self.button
        return FakeLocator(self,count=0)
    def get_by_role(self,*args,**kwargs):return self.button
    def wait_for_selector(self,*args,**kwargs):
        if not self.button.count():raise RuntimeError('not found')
        return self.button
    def wait_for_timeout(self,ms):pass
    def wait_for_load_state(self,*a,**k):pass
    def goto(self,url,**kwargs):self.goto_calls.append(url);self.url=url

class AutoLoginTests(unittest.TestCase):
    def test_saved_same_origin_login_clicks_exact_sign_in(self):
        c=BulkCollector(Path('.'),{'runs_url':'https://beta.gennext.bio/runs?page=1'},None)
        c.main=FakePage();c._remote_auto_login_if_needed()
        self.assertTrue(c.main.button.clicked)
        self.assertIn('/runs',c.main.url)
    def test_visual_autofill_not_readable_still_submits(self):
        # Regression for live v0.4.50: Chromium can show saved credentials while
        # Playwright input_value is temporarily empty. Submit must not be gated on it.
        c=BulkCollector(Path('.'),{'runs_url':'https://beta.gennext.bio/runs?page=1'},None)
        c.main=FakePage(user='',password='',submit_succeeds=True);c._remote_auto_login_if_needed()
        self.assertTrue(c.main.button.clicked)
        self.assertIn('/runs',c.main.url)
    def test_submit_staying_on_auth_fails_without_exposing_credentials(self):
        c=BulkCollector(Path('.'),{'runs_url':'https://beta.gennext.bio/runs?page=1'},None)
        c.main=FakePage(user='',password='',submit_succeeds=False)
        with self.assertRaisesRegex(RuntimeError,'Sign In tamamlanmadı'):c._remote_auto_login_if_needed()
    def test_foreign_auth_host_refused(self):
        c=BulkCollector(Path('.'),{'runs_url':'https://beta.gennext.bio/runs?page=1'},None)
        c.main=FakePage();c.main.url='https://evil.invalid/auth'
        with self.assertRaisesRegex(RuntimeError,'beklenmeyen'):c._remote_auto_login_if_needed()
        self.assertFalse(c.main.button.clicked)

if __name__=='__main__':unittest.main(verbosity=2)
