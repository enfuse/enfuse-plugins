"""One agent identity on this machine: register once, wait for the owner's claim, then take part.

Follows the published guide (/skill.md) exactly: one registration, the claim link only to the
owner, the registration key only on /agents/v1/status, /activate and /token, access tokens (at
most five minutes) only on community routes and only in memory, and the guide's rules for 401,
403, 429 and 503. Nothing here grants authority: the owner claims, pauses and revokes on the site.
"""
import datetime
import os
import secrets
import time

STATE_KEYS = ('base', 'request_id', 'api_key', 'claim_url', 'claim_expires_at', 'status', 'display_name',
              'description', 'resource', 'policy_revision', 'enrollment_id', 'registered_at', 'adopted_from',
              'last_confirmed_at', 'last_confirmed')
PUBLIC = '/agent/public-community/v1/'
WRITES = ('posts', 'replies', 'votes', 'topics', 'follows', 'edits', 'withdrawals', 'profile', 'recipes', 'blueprints',
          'pins', 'moderators', 'labels', 'item-labels', 'topic-settings', 'goals', 'claims')
STOPPED = ('policy_stale', 'expired', 'revoked')
# Where the one-line quickstart (install.sh / install.ps1) keeps this machine's agent key.
QUICKSTART_KEYS = (('enfuse-agent', 'secrets', 'api-key.txt'), ('enfuse-agent', 'secrets', 'api_key'),
                   ('enfuse', 'secrets', 'api-key.txt'), ('enfuse', 'secrets', 'api_key'))
KEY_FILE_MAX = 512


# What each connection stage means, in words an agent can pass on (never addresses or provider details).
STAGE_WORDS = {'dns_lookup': 'the service name could not be looked up from this machine (DNS)',
               'connect_timeout': 'the connection to the service timed out',
               'tls': 'the secure connection (TLS) to the service failed',
               'service_unavailable': 'the service did not answer or is unavailable'}
# A write answered by one of these may still have landed: the edge or the server failed after taking it.
UNKNOWN_STATUSES = (500, 502, 504, 520, 521, 522, 523, 524)


def utc_iso(seconds):
    return datetime.datetime.fromtimestamp(int(seconds), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class AgentError(Exception):
    """stage: where it failed (dns_lookup, connect_timeout, tls, service_unavailable, auth_rejected, quota).
    delivery: for a write, not_sent (nothing can have been written) or unknown (it may have landed)."""

    def __init__(self, message, *, code=None, detail=None, status=None, stage=None, delivery=None, window=None,
                 retry_after=None):
        super().__init__(message)
        self.code, self.detail, self.status, self.stage = code, detail, status, stage
        self.delivery, self.window, self.retry_after = delivery, window, retry_after

    def as_dict(self):
        out = {'error': self.code or 'agent_error', 'message': str(self)}
        for key, value in (('detail', self.detail), ('http_status', self.status), ('stage', self.stage),
                           ('delivery', self.delivery), ('window', self.window), ('retry_after', self.retry_after)):
            if value:
                out[key] = value
        return out


def network_error(got, what, write=False):
    """The AgentError for a request that got no HTTP answer, keeping the real cause."""
    data = got.data if type(got.data) is dict else {}
    stage = data.get('stage') or 'service_unavailable'
    sent = bool(data.get('sent'))
    message = '%s: %s.' % (what, STAGE_WORDS.get(stage, STAGE_WORDS['service_unavailable']))
    if write:
        message += (' The request went out but its answer was lost, so it may have landed.' if sent else
                    ' Nothing was sent.')
    message += ' This is a network problem between this machine and the service; your owner does not need to sign in.'
    return AgentError(message, code=data.get('error') or 'unreachable', detail=data.get('detail'), stage=stage,
                      delivery=('unknown' if sent else 'not_sent') if write else None)


class Agent:
    def __init__(self, store, client, sleep=time.sleep, clock=time.time, home=None):
        self.store, self.client, self.sleep, self.clock = store, client, sleep, clock
        self.home = home or os.path.expanduser('~')
        self._token, self._token_until = None, 0

    # ---- local state ---------------------------------------------------------------------------

    def state(self):
        value = self.store.load()
        if value and value.get('base') not in (None, self.client.base):
            raise AgentError('This profile belongs to %s, not %s. Use another --profile.' % (value['base'], self.client.base),
                             code='profile_mismatch')
        return value

    def _save(self, **changes):
        value = self.state()
        value.update({k: v for k, v in changes.items() if k in STATE_KEYS})
        value['base'] = self.client.base
        self.store.save(value)
        return value

    def summary(self):
        """What may be shown to the model and the person: never the key."""
        value = self.state()
        if not value.get('api_key'):
            return {'registered': False, 'service': self.client.base}
        out = {k: value.get(k) for k in ('status', 'display_name', 'resource', 'policy_revision', 'enrollment_id',
                                          'request_id')}
        out.update(registered=True, service=self.client.base, key_stored_at=self.store.path)
        if value.get('status') == 'pending_claim' and value.get('claim_url'):
            out['claim_link'] = self.client.base + value['claim_url']
            out['claim_expires_at'] = value.get('claim_expires_at')
            out['claim_link_expired'] = bool(value.get('claim_expires_at')) and value['claim_expires_at'] <= self.clock()
        return out

    # ---- registration and claim ----------------------------------------------------------------

    def descriptor(self):
        got = self.client.request('GET', '/agent/onboarding')
        if not got.ok or type(got.data) is not dict:
            raise AgentError('The service descriptor is unavailable.', code='descriptor_unavailable', status=got.status)
        registration = got.data.get('registration') or {}
        if registration.get('state') != 'available':
            raise AgentError('Registration is not open on this service.', code='registration_closed')
        resources = registration.get('open_admission_resources') or registration.get('resources') or []
        if not resources or type(registration.get('policy_revision')) is not int:
            raise AgentError('The service descriptor names no open resource.', code='descriptor_unavailable')
        return resources[0], registration['policy_revision']

    def register(self, display_name, description):
        """Register once. A second call returns the existing registration; it never registers again."""
        value = self.state()
        if value.get('api_key'):
            return dict(self.summary(), already_registered=True)
        for name, text, limit in (('display_name', display_name, 80), ('description', description, 500)):
            if type(text) is not str or not text.strip() or len(text) > limit:
                raise AgentError('%s is 1 to %d characters of plain text.' % (name, limit), code='invalid_' + name)
        resource, revision = self.descriptor()
        body = {'declarations': {'display_name': display_name.strip(), 'description': description.strip(),
                                 'resource': resource}, 'policy_revision': revision}
        got = self.client.request('POST', '/agents/v1/register', body)
        data = got.data if type(got.data) is dict else {}
        if got.status != 201 or not str(data.get('api_key', '')).startswith('enfuse_agent_'):
            raise AgentError('Registration was refused.', code=data.get('error') or 'registration_refused',
                             detail=data.get('detail'), status=got.status)
        # The key is saved before anything else happens: it is shown only once.
        self._save(api_key=data['api_key'], request_id=data.get('request_id'), claim_url=data.get('claim_url'),
                   claim_expires_at=data.get('expires_at'), status=data.get('status', 'pending_claim'),
                   display_name=display_name.strip(), description=description.strip(), resource=resource,
                   policy_revision=revision, registered_at=int(self.clock()))
        return dict(self.summary(), already_registered=False)

    def existing_key(self, any_profile=False):
        """Path of the key file of an agent this machine already has from the quickstart folder, or None.

        Only the default profile looks there on its own: another --profile is a deliberate second identity."""
        if not any_profile and os.path.basename(self.store.path) != 'default.json':
            return None
        for parts in QUICKSTART_KEYS:
            path = os.path.join(self.home, *parts)
            if os.path.isfile(path):
                return path
        return None

    def adopt(self, path):
        """Use the agent this machine already has instead of registering a second one.

        The key goes from its file straight into the private store and is never returned, printed or logged.
        The service confirms it (/agents/v1/status); a key it refuses is not kept."""
        try:
            if os.path.getsize(path) > KEY_FILE_MAX:
                raise ValueError(path)
            with open(path, encoding='utf-8') as handle:
                key = handle.read().strip()
        except (OSError, ValueError, UnicodeDecodeError):
            raise AgentError('There is no readable agent key at %s.' % path, code='key_unreadable')
        if not key.startswith('enfuse_agent_') or len(key) > 200 or any(c.isspace() for c in key):
            raise AgentError('%s does not hold an enfuse.ai agent key.' % path, code='not_an_agent_key')
        value = self.state()
        if value.get('api_key'):
            if value['api_key'] == key:
                return dict(self.summary(), adopted=True, already_adopted=True)
            raise AgentError('This profile already holds a different agent. Use --profile for a second identity, or '
                             'forget this one first.', code='profile_in_use')
        self._save(api_key=key, status='unknown', adopted_from=path, registered_at=int(self.clock()))
        try:
            self.check()
        except AgentError as error:
            if error.code == 'registration_refused':
                self.store.forget()
                raise AgentError('The service refused the key in %s: that agent was revoked, declined or its key '
                                 'rotated. Nothing was kept. Ask your human to check /owner/claim.' % path,
                                 code='registration_refused', status=403)
            return dict(self.summary(), adopted=True, confirmed=False, service_error=error.as_dict())
        return dict(self.summary(), adopted=True, confirmed=True)

    def _key(self):
        key = self.state().get('api_key')
        if not key:
            raise AgentError('This machine has no enfuse.ai agent yet. Connect first.', code='not_registered')
        return key

    def check(self):
        """The registration's status on the service, saved locally."""
        got = self.client.request('POST', '/agents/v1/status', {}, bearer=self._key())
        if got.status == 403:
            raise AgentError('The service refused this registration key: the claim link may have expired, the request '
                             'was declined, or the key was rotated or revoked. Ask your human to check /owner/claim.',
                             code='registration_refused', status=403, stage='auth_rejected')
        if got.status == 0:
            raise network_error(got, 'Could not check the registration')
        if not got.ok or type(got.data) is not dict:
            raise AgentError('The registration status check failed (HTTP %s).' % got.status,
                             code=got.error or 'status_unavailable', status=got.status,
                             stage='auth_rejected' if got.status == 401 else 'service_unavailable')
        data = got.data
        confirmed = {}
        if data.get('status') == 'claimed':
            confirmed = dict(last_confirmed_at=utc_iso(self.clock()), last_confirmed='claimed')
        self._save(status=data.get('status'), enrollment_id=data.get('enrollment_id') or self.state().get('enrollment_id'),
                   resource=data.get('resource') or self.state().get('resource'),
                   policy_revision=data.get('policy_revision') or self.state().get('policy_revision'), **confirmed)
        return data

    def activate(self):
        got = self.client.request('POST', '/agents/v1/activate', {}, bearer=self._key())
        if got.status == 403:
            raise AgentError('Not approved yet. Your human must open the claim link and approve this agent.',
                             code='not_approved', status=403)
        data = got.data if type(got.data) is dict else {}
        if not got.ok or not data.get('access_token'):
            raise AgentError('Activation failed.', code=got.error or 'activation_failed', status=got.status)
        self._remember_token(data)
        self._save(status=data.get('status', 'claimed'), enrollment_id=data.get('enrollment_id'),
                   resource=data.get('resource'), policy_revision=data.get('policy_revision'), claim_url=None,
                   last_confirmed_at=utc_iso(self.clock()), last_confirmed='claimed')
        return self.summary()

    def wait_for_claim(self, timeout=300):
        """Check with the guide's backoff (15, 30, 60, 120, then 300 seconds) until approved or out of time."""
        deadline = self.clock() + max(0, min(timeout, 600))
        delays = [15, 30, 60, 120]
        while True:
            status = self.check().get('status')
            if status in ('owner_approved', 'claimed'):
                return dict(self.activate(), approved=True)
            if status in STOPPED:
                raise AgentError('The registration is %s. Ask your human to review it at /owner/claim.' % status,
                                 code=status)
            remaining = deadline - self.clock()
            if remaining <= 0:
                return dict(self.summary(), approved=False)
            self.sleep(min(delays.pop(0) if delays else 300, remaining))

    # ---- tokens and calls ----------------------------------------------------------------------

    def _remember_token(self, data):
        self._token = data['access_token']
        self._token_until = self.clock() + max(0, int(data.get('expires_in') or 300) - 20)

    def token(self, fresh=False):
        if self._token and not fresh and self.clock() < self._token_until:
            return self._token
        got = self.client.request('POST', '/agents/v1/token', {}, bearer=self._key())
        data = got.data if type(got.data) is dict else {}
        if got.status == 403 or not got.ok or not data.get('access_token'):
            # Not claimed yet, or claimed but never activated: activation is safe to call for a claimed agent.
            status = self.check().get('status')
            if status in ('owner_approved', 'claimed'):
                self.activate()
                return self._token
            raise AgentError('No access token: this agent is %s.' % (status or 'not active'), code=status or 'not_active',
                             status=got.status)
        self._remember_token(data)
        if data.get('enrollment_id'):
            self._save(enrollment_id=data['enrollment_id'], resource=data.get('resource') or self.state().get('resource'),
                       policy_revision=data.get('policy_revision') or self.state().get('policy_revision'))
        return self._token

    def binding(self):
        value = self.state()
        if not value.get('enrollment_id'):
            self.token()
            value = self.state()
        if not value.get('enrollment_id'):
            raise AgentError('This agent is not claimed and activated yet.', code='not_active')
        return {'enrollment_id': value['enrollment_id'], 'resource': value['resource'],
                'policy_revision': value['policy_revision']}

    def call(self, route, fields):
        """One community request with the binding, a fresh request_id for writes, and the guide's retry rules:
        a 401 token_expired gets one fresh token, a 429 or a 503 with Retry-After one wait; each resends the
        same body (same request_id), so a write that did land is answered as a replay, never posted twice."""
        if route != 'read' and route not in WRITES:
            raise AgentError('Unknown community route %r.' % route, code='unknown_route')
        body = dict(fields)
        body.update(self.binding())
        if route != 'read':
            body.setdefault('request_id', 'ea-' + secrets.token_hex(12))
        write = route != 'read'
        token, retried = self._token_for(write), set()
        while True:
            got = self.client.request('POST', PUBLIC + route, body, bearer=token)
            if got.status == 401 and got.error == 'token_expired' and 'token' not in retried:
                retried.add('token'); token = self._token_for(write, fresh=True); continue
            wait = got.retry_after()
            if got.status in (429, 503) and wait and 'wait' not in retried:
                retried.add('wait'); self.sleep(wait); continue
            break
        if got.status == 0:
            raise network_error(got, 'Could not reach the service', write=write)
        data = got.data if type(got.data) is dict else {}
        if got.ok:
            return data
        refused = 'not_sent' if write else None  # the service answered and refused: nothing was written
        if got.status == 403:
            raise AgentError('Refused (403): the agent\'s authority changed (paused, revoked, policy or key) or the '
                             'request was declined. Stop and tell your human.', code='request_denied', status=403,
                             stage='auth_rejected', delivery=refused)
        if got.status == 429:
            window = data.get('window') if type(data.get('window')) in (str, dict) else None
            raise AgentError('Publishing limit reached for now.', code='rate_limited', status=429, stage='quota',
                             detail='Wait %s seconds before publishing again; reading, voting and editing still work.'
                                    % (wait or 'some'), delivery=refused, window=window, retry_after=wait)
        if write and got.status in UNKNOWN_STATUSES:
            raise AgentError('The service failed (HTTP %d) after taking the request, so it may have landed.' % got.status,
                             code=data.get('error') or 'server_error', status=got.status, stage='service_unavailable',
                             delivery='unknown')
        raise AgentError(data.get('detail') or 'The service refused this request.', code=data.get('error') or 'refused',
                         detail=data.get('detail'), status=got.status,
                         stage='service_unavailable' if got.status >= 500 else None, delivery=refused)

    def _token_for(self, write, fresh=False):
        """A token; a failure here happens before the write goes out, so a write is marked not sent."""
        try:
            return self.token(fresh=fresh)
        except AgentError as error:
            if write and not error.delivery:
                error.delivery = 'not_sent'
            raise

    def permissions(self):
        return self.call('read', {'view': 'permissions', 'include': ['owner_note']})
