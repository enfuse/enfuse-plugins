"""Where this machine keeps its enfuse.ai agent identity.

One JSON file per profile in a private directory (``~/.config/enfuse-agent`` by default, or
``$ENFUSE_AGENT_HOME``). The directory is 0700 and the file 0600, written atomically. On POSIX a
file another user could read is refused, the way ssh refuses a private key with loose permissions.

The registration key lives only here. It is never returned to the model, printed, logged or put
in an environment variable; access tokens are never written at all.
"""
import json
import os
import re
import tempfile

PROFILE = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}\Z')


class StoreError(Exception):
    pass


def default_home():
    if os.environ.get('ENFUSE_AGENT_HOME'):
        return os.environ['ENFUSE_AGENT_HOME']
    config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(config, 'enfuse-agent')


class Store:
    def __init__(self, directory=None, profile='default'):
        if not PROFILE.match(profile or ''):
            raise StoreError('A profile name is 1 to 32 lowercase letters, digits, "_" or "-".')
        self.directory = directory or default_home()
        self.path = os.path.join(self.directory, profile + '.json')

    def _check(self):
        if os.name != 'posix':
            return
        info = os.stat(self.path)
        if info.st_uid != os.getuid():
            raise StoreError('%s belongs to another user; refusing to use it.' % self.path)
        if info.st_mode & 0o077:
            raise StoreError('%s can be read by other users. Run: chmod 600 %s' % (self.path, self.path))

    def load(self):
        if not os.path.exists(self.path):
            return {}
        self._check()
        with open(self.path, encoding='utf-8') as handle:
            value = json.load(handle)
        if type(value) is not dict:
            raise StoreError('%s is not an enfuse-agent profile.' % self.path)
        return value

    def save(self, value):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        if os.name == 'posix':
            os.chmod(self.directory, 0o700)
        handle, temporary = tempfile.mkstemp(prefix='.profile-', dir=self.directory)
        try:
            if os.name == 'posix':
                os.fchmod(handle, 0o600)
            with os.fdopen(handle, 'w', encoding='utf-8') as out:
                json.dump(value, out, indent=1, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    def forget(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
            return True
        return False
