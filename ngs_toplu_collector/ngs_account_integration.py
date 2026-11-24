"""GenNext account login: use the selected DPAPI identity, not cached browser login.

This wrapper runs entirely on the existing BrowserWorker Playwright thread.
It never logs passwords or writes credentials to browser state or diagnostics.
"""
from urllib.parse import urlparse
from ngs_accounts import AccountStore

_AUTH = ('/auth', '/login', '/signin', '/sign-in')


def _at_auth(page, runs_url):
    try:
        a, b = urlparse(page.url), urlparse(runs_url)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc) and a.path.rstrip('/').lower() in _AUTH
    except Exception:
        return False


def _login_fields(page):
    for scope in [page] + [f for f in page.frames if f is not page.main_frame]:
        try:
            if urlparse(scope.url).netloc != urlparse(page.url).netloc:
                continue
            email = scope.locator('input[name="email"], input[type="email"], input[autocomplete="username"]').first
            password = scope.locator('input[type="password"]').first
            if email.count() and password.count() and email.is_visible() and password.is_visible():
                return email, password
        except Exception:
            continue
    return None


def install(collector_class):
    original = collector_class.open_browser
    if getattr(original, '_ngs_accounts_v2', False):
        return

    def open_browser(self, auto_login=False, account_id=None):
        store = AccountStore(self.base)
        # Explicit account always wins. Otherwise use active choice, then saved default.
        if account_id is None:
            account_id = getattr(self, '_ngs_active_account', None)
            if account_id not in ('1', '2'):
                account_id = store.read().get('default', '1')
        if account_id not in ('1', '2'):
            raise ValueError('GenNext hesap seçimi geçersiz.')
        account = store.read().get('accounts', {}).get(account_id, {})
        if not account.get('email') or not account.get('password'):
            raise ValueError('Seçili GenNext hesabında kullanıcı adı veya şifre eksik.')
        current = getattr(self, '_ngs_active_account', None)
        if self.context is not None and current != account_id:
            try:
                self.context.close()
            finally:
                self.context = None
                self.main = None
                if self.pw:
                    self.pw.stop()
                    self.pw = None
        self._ngs_active_account = account_id
        previous_profile = self.cfg.get('profile_dir')
        try:
            self.cfg['profile_dir'] = str(store.profile(account_id).relative_to(self.base))
            # Force one fresh credential login per browser context, not on each collector step.
            context_identity = getattr(self, '_ngs_account_context_v2', None)
            fresh = self.context is None or context_identity is not self.context
            if fresh:
                # Prevent V6's automatic wait while the account form is not filled yet.
                previous_skip = getattr(self, '_ngs_login_skip_auth_v6', False)
                self._ngs_login_skip_auth_v6 = True
                try:
                    result = original(self, auto_login=False) or {}
                finally:
                    self._ngs_login_skip_auth_v6 = previous_skip
                if self.context is None or self.main is None:
                    raise RuntimeError('GenNext tarayıcısı açılamadı.')
                # V8: preserve a working session in this account-specific profile.
                # A dashboard or runs landing is not alone proof; require an
                # authenticated route and no visible auth form after settling.
                try:
                    from urllib.parse import urlparse as _urlparse
                    origin = _urlparse(str(self.cfg.get('runs_url', '')))
                    dashboard = f'{origin.scheme}://{origin.netloc}/dashboard'
                    self.main.goto(dashboard, wait_until='domcontentloaded', timeout=20000)
                    self.main.wait_for_timeout(900)
                    path = _urlparse(self.main.url).path.rstrip('/').lower()
                    if (path in ('/dashboard', '/runs') and
                            _urlparse(self.main.url).netloc == origin.netloc and
                            not _login_fields(self.main)):
                        # Ensure the protected RUNS route also remains available.
                        self.main.goto(str(self.cfg.get('runs_url')), wait_until='domcontentloaded', timeout=20000)
                        self.main.wait_for_timeout(650)
                        path = _urlparse(self.main.url).path.rstrip('/').lower()
                        if path == '/runs' and not _login_fields(self.main):
                            self._ngs_account_context_v2 = self.context
                            self.log('NGS hesap ' + account_id + ': mevcut GenNext oturumu korundu, cikis yapilmadi.')
                            return {'browser_ready': True, 'auth_verified': True}
                except Exception as exc:
                    self.log('GenNext aktif oturum denetimi tamamlanamadi; giris ekrani kontrol edilecek.')
                # Do not force logout or destroy browser cookies. Login only
                # when protected routes redirected to the real auth form.
                runs_url = str(self.cfg.get('runs_url', ''))
                base = urlparse(runs_url)
                if (base.scheme, base.netloc) != ('https', 'beta.gennext.bio'):
                    # Respect other configured environments but reject foreign hosts.
                    if base.scheme != 'https' or not base.netloc.endswith('.gennext.bio'):
                        raise RuntimeError('GenNext giriş alan adı doğrulanamadı.')
                auth_url = base.scheme + '://' + base.netloc + '/auth'
                try:
                    self.main.goto(auth_url, wait_until='domcontentloaded', timeout=20000)
                except Exception:
                    # Delayed redirects are possible; inspect the actual DOM below.
                    pass
                if not _at_auth(self.main, runs_url):
                    raise RuntimeError('GenNext korumali sayfasi dogrulanamadi; mevcut oturum silinmedi, manuel kontrol gerekli.')
                fields = None
                try:
                    for _ in range(20):
                        fields = _login_fields(self.main)
                        if fields:
                            break
                        self.main.wait_for_timeout(250)
                except Exception:
                    pass
                if not fields:
                    raise RuntimeError('GenNext giriş formu bulunamadı. Oturum temizliği veya web sayfası kontrol edilmeli.')
                email, password = fields
                email.fill(account['email'])
                password.fill(account['password'])
                self.log('NGS hesap ' + account_id + ': kayıtlı kullanıcı adı ve şifre giriş formuna yazıldı.')
                # V6 guard submits one visible enabled Sign In and checks actual runs.
                verified = bool(self._login_ensure_v6(self.main, runs_url, require_runs=True))
                if not verified:
                    raise RuntimeError('GenNext oturum açma doğrulanamadı.')
                self._ngs_account_context_v2 = self.context
                return {'browser_ready': True, 'auth_verified': True}
            # Existing validated account context: do not sign out on routine calls.
            return original(self, auto_login=auto_login)
        finally:
            if previous_profile is None:
                self.cfg.pop('profile_dir', None)
            else:
                self.cfg['profile_dir'] = previous_profile

    open_browser._ngs_accounts_v2 = True
    collector_class.open_browser = open_browser
