"""HTTPS to the enfuse.ai service: JSON in, JSON out, nothing else.

No cookies, no Origin header (the service refuses both on agent routes), a descriptive
User-Agent (the edge network rejects anonymous clients), and errors returned as data so the
caller decides what a status means.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

DEFAULT_BASE = 'https://physicalai.enfuse.ai'
USER_AGENT = 'enfuse-agent/%s (+https://physicalai.enfuse.ai/skill.md)' % __version__


# No rolling window is longer than a day; a Retry-After beyond a week is treated as malformed (None), never capped.
MAX_RETRY_AFTER = 7 * 24 * 3600


class Response:
    def __init__(self, status, data, text, headers):
        self.status, self.data, self.text, self.headers = status, data, text, headers

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def error(self):
        return self.data.get('error') if type(self.data) is dict else None

    def header(self, name):
        """A response header by name, whatever its case (None when absent)."""
        name = name.lower()
        for key, value in (self.headers or {}).items():
            if str(key).lower() == name:
                return value
        return None

    def retry_after(self):
        """The server's Retry-After in whole seconds, exactly as sent, or None when it is missing or malformed.
        No floor and no cap: a value beyond MAX_RETRY_AFTER (7 days) is out of range and treated as malformed,
        so the caller never invents a time. The length is checked before int() (huge digit strings raise there)."""
        value = str(self.header('Retry-After') or '').strip()
        if not (value.isascii() and value.isdigit()) or len(value) > len(str(MAX_RETRY_AFTER)):
            return None
        seconds = int(value)
        return seconds if seconds <= MAX_RETRY_AFTER else None


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """The service never redirects these routes; a redirect would carry the bearer token elsewhere."""

    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base=None, opener=None, timeout=30):
        self.base = (base or DEFAULT_BASE).rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        local = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if not (parsed.scheme == 'https' or local) or not parsed.hostname:
            raise ValueError('The service address must be https (or http to a local test server).')
        self.open = opener or urllib.request.build_opener(urllib.request.HTTPSHandler(), NoRedirects()).open
        self.timeout = timeout

    def url(self, path, query=None):
        if not path.startswith('/'):
            raise ValueError('A service path starts with /')
        query = {k: v for k, v in (query or {}).items() if v not in (None, '')}
        return self.base + path + ('?' + urllib.parse.urlencode(query) if query else '')

    def request(self, method, path, body=None, bearer=None, query=None, accept='application/json'):
        headers = {'User-Agent': USER_AGENT, 'Accept': accept}
        data = None
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body if body is not None else {}, separators=(',', ':')).encode('utf-8')
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        request = urllib.request.Request(self.url(path, query), data=data, headers=headers, method=method)
        try:
            response = self.open(request, timeout=self.timeout)
            status, raw, got = response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            status, raw, got = error.code, error.read(), error.headers
        except urllib.error.URLError as error:
            return Response(0, {'error': 'unreachable', 'detail': str(error.reason)}, '', {})
        text = raw.decode('utf-8', 'replace')
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        return Response(status, parsed, text, dict(got.items()) if got else {})
