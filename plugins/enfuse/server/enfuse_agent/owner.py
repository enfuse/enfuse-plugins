"""The owner's side, from a terminal: sign in once on the website, then check on, note, pause and resume agents.

`owner login` starts a device-code sign-in: it prints a short code and a link, you approve it on
/owner/terminal while signed in and choose which agents this terminal may steer, and the terminal
receives a token for 30 days. The token is kept in its own 0600 profile (``owner/<profile>.json``),
apart from the agent's registration key, and is never shown, logged or offered to the model as a tool.

What the token can do is limited on the server: read state, permissions and notes; replace a note; pause;
resume only a pause this terminal made. Permissions, names, consent and revocation stay on the website.
Anything that can read the profile file can do the same, which is why the website shows each signed-in
terminal with a Sign out button.
"""
import os
import time

from .store import Store, default_home

V1 = '/owner/terminal/v1'
STATE_KEYS = ('base', 'token', 'token_id', 'label', 'agents', 'expires_at')


class OwnerError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Owner:
    def __init__(self, store, client, sleep=time.sleep, clock=time.time):
        self.store, self.client, self.sleep, self.clock = store, client, sleep, clock

    @classmethod
    def for_profile(cls, client, profile='default', directory=None, **options):
        """The owner sign-in for one profile, in its own ``owner/`` folder: never an agent profile's file."""
        return cls(Store(os.path.join(directory or default_home(), 'owner'), profile), client, **options)

    def _state(self):
        value = self.store.load()
        if value and value.get('base') != self.client.base:
            raise OwnerError('This owner sign-in belongs to %s, not %s.' % (value.get('base'), self.client.base))
        return value

    def signed_in(self):
        value = self._state()
        return bool(value.get('token')) and value.get('expires_at', 0) > self.clock()

    def summary(self):
        """What may be shown: never the token."""
        value = self._state()
        if not value.get('token'):
            return {'signed_in': False, 'service': self.client.base}
        return {'signed_in': value.get('expires_at', 0) > self.clock(), 'service': self.client.base,
                'label': value.get('label'), 'token_id': value.get('token_id'), 'agents': value.get('agents'),
                'expires_at': value.get('expires_at'), 'stored_at': self.store.path}

    # ---- sign-in -------------------------------------------------------------------------------

    def login(self, label, show, timeout=600):
        """Start a sign-in, call ``show(code, link)`` once, then poll until approved, declined or expired."""
        if self.signed_in():
            raise OwnerError('Already signed in as "%s". Run owner logout first.' % self._state().get('label'),
                             'already_signed_in')
        got = self.client.request('POST', V1 + '/start', {'label': label})
        data = got.data if type(got.data) is dict else {}
        if not got.ok or not data.get('device_code'):
            raise OwnerError(data.get('detail') or 'The service did not start a sign-in.', data.get('error'))
        show(data['user_code'], data['verification_uri_complete'])
        device, interval = data['device_code'], max(5, int(data.get('interval') or 5))
        deadline = self.clock() + min(timeout, int(data.get('expires_in') or 600))
        while self.clock() < deadline:
            self.sleep(interval)
            got = self.client.request('POST', V1 + '/token', {'device_code': device})
            data = got.data if type(got.data) is dict else {}
            if got.ok and data.get('access_token'):
                self.store.save({'base': self.client.base, 'token': data['access_token'], 'token_id': data['token_id'],
                                 'label': data['label'], 'agents': data['agents'],
                                 'expires_at': int(self.clock()) + int(data['expires_in'])})
                return self.summary()
            error = data.get('error')
            if error == 'slow_down':
                interval += 5
            elif error != 'authorization_pending':
                raise OwnerError({'access_denied': 'The sign-in was declined on the website.',
                                  'expired_token': 'The code expired. Run owner login again.'}.get(
                                      error, 'The sign-in failed (%s).' % (error or got.status)), error)
        raise OwnerError('Not approved in time. Run owner login again.', 'expired_token')

    def logout(self):
        """Sign this terminal out on the service, then delete the local token. A local delete alone
        would leave the token valid, so it happens only after the service confirms (or no longer knows it)."""
        value = self._state()
        if not value.get('token'):
            return {'signed_out': False, 'reason': 'not signed in'}
        got = self.client.request('POST', V1 + '/sign-out', {}, bearer=value['token'])
        if not (got.ok or got.status == 401):
            raise OwnerError('The service did not confirm the sign-out (%s); nothing was deleted.' % got.status)
        self.store.forget()
        return {'signed_out': True, 'token_id': value.get('token_id')}

    # ---- steering ------------------------------------------------------------------------------

    def _call(self, method, route, body=None):
        value = self._state()
        if not value.get('token'):
            raise OwnerError('Not signed in. Run owner login first.', 'not_signed_in')
        got = self.client.request(method, V1 + route, body, bearer=value['token'])
        data = got.data if type(got.data) is dict else {}
        if got.ok:
            return data
        if got.status == 401:
            raise OwnerError('This terminal is signed out (expired, signed out on the website, or revoked). '
                             'Run owner login again.', 'terminal_signin_required')
        raise OwnerError(data.get('detail') or 'Refused (%s).' % (data.get('error') or got.status), data.get('error'))

    def status(self):
        return self._call('GET', '/agents')

    def resolve(self, name, local_enrollment=None):
        """An agent by enrollment ID, exact display name (any case), or ``this`` for this machine's agent."""
        agents = self.status().get('agents') or []
        if name == 'this':
            if not local_enrollment:
                raise OwnerError('This machine has no connected agent; name one instead.', 'unknown_agent')
            name = local_enrollment
        found = [a for a in agents if a['enrollment_id'] == name] or [
            a for a in agents if (a.get('display_name') or '').casefold() == name.casefold()]
        if len(found) != 1:
            names = ', '.join(a.get('display_name') or a['enrollment_id'] for a in agents) or 'none'
            raise OwnerError('No single agent named %r on this terminal. Agents: %s.' % (name, names), 'unknown_agent')
        return found[0]['enrollment_id']

    def note(self, enrollment_id, text):
        lines = text.split('\n') if text else []
        return self._call('POST', '/note', {'enrollment_id': enrollment_id, 'owner_note': lines})

    def pause(self, enrollment_id):
        return self._call('POST', '/pause', {'enrollment_id': enrollment_id})

    def resume(self, enrollment_id):
        return self._call('POST', '/resume', {'enrollment_id': enrollment_id})
