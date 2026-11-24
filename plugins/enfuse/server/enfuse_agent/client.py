"""HTTPS to the enfuse.ai service: JSON in, JSON out, nothing else.

No cookies, no Origin header (the service refuses both on agent routes), a descriptive
User-Agent (the edge network rejects anonymous clients), and errors returned as data so the
caller decides what a status means.

A request that never got an HTTP answer comes back as status 0 with ``data`` naming the ``stage``
it failed at (dns_lookup, connect_timeout, tls, service_unavailable) and ``sent``: False when it
failed before the request was on the wire (nothing can have been written), True when the request
went out and the answer was lost (a write may have landed). Only the exception's class name is
kept, never addresses, resolver or certificate details.
"""
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import __version__


def network_stage(reason):
    """Where a connection failed, from the underlying exception (or URLError.reason)."""
    if isinstance(reason, socket.gaierror):
        return 'dns_lookup'
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 'connect_timeout'
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return 'tls'
    return 'service_unavailable'


def unreachable(reason, sent):
    return {'error': 'unreachable' if not sent else 'no_response', 'stage': network_stage(reason), 'sent': sent,
            'detail': type(reason).__name__ if not isinstance(reason, str) else 'connection failed'}

DEFAULT_BASE = 'https://physicalai.enfuse.ai'
USER_AGENT = 'enfuse-agent/%s (+https://physicalai.enfuse.ai/skill.md)' % __version__


class Response:
    def __init__(self, status, data, text, headers):
        self.status, self.data, self.text, self.headers = status, data, text, headers

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def error(self):
        return self.data.get('error') if type(self.data) is dict else None

    def retry_after(self, ceiling=120):
        try:
            return max(1, min(ceiling, int(self.headers.get('Retry-After', ''))))
        except (TypeError, ValueError):
            return None


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
            try:
                status, raw, got = error.code, error.read(), error.headers
            except (OSError, http.client.HTTPException) as lost:
                # The service answered with an error status, then its body was cut off: delivery unknown.
                return Response(0, unreachable(lost, sent=True), '', {})
        except urllib.error.URLError as error:
            # Raised while resolving, connecting, shaking hands or sending: the request was not taken.
            return Response(0, unreachable(error.reason, sent=False), '', {})
        except (OSError, http.client.HTTPException) as error:
            # Raised after the request went out (waiting for or reading the answer): delivery unknown.
            return Response(0, unreachable(error, sent=True), '', {})
        text = raw.decode('utf-8', 'replace')
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        return Response(status, parsed, text, dict(got.items()) if got else {})
