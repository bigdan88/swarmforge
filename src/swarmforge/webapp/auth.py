"""Local browser session and CSRF protection, independent of Host/Origin checks."""
import hashlib
import hmac
import secrets
import time


class LocalSession:
    lifetime = 8 * 3600
    cookie = "swarmforge_web_session"

    def __init__(self):
        self.secret = secrets.token_bytes(32)

    def _sign(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def valid_cookie(self, cookie):
        try:
            sid, issued, signature = (cookie or "").split(".")
            age = time.time() - int(issued)
            return len(sid) == 48 and 0 <= age < self.lifetime and hmac.compare_digest(signature, self._sign(sid + "." + issued))
        except (ValueError, TypeError):
            return False

    def issue(self, cookie=None):
        if not self.valid_cookie(cookie):
            value = secrets.token_hex(24) + "." + str(int(time.time()))
            cookie = value + "." + self._sign(value)
        return cookie, self._sign("csrf:" + cookie)

    def authorized(self, cookie, token):
        return self.valid_cookie(cookie) and bool(token) and hmac.compare_digest(token, self._sign("csrf:" + cookie))
