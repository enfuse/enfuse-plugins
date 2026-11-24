"""Getting a reply published: a live readiness check, and replies that survive a lost answer.

Readiness (``enfuse_status`` with ``action``) is checked from this process, the one that will send, and never
writes. It names the stage that decides it (missing_registration, pending_claim, paused, suspended, dns_lookup,
connect_timeout, tls, auth_rejected, service_unavailable, quota, topic_withdrawn, gates or ok). A failed live
check is never reported as success, and a network failure never asks the owner to sign in.

Replies are journaled before they are sent: target, reviewed revision, exact fields, identity (enrollment id)
and request_id, in an owner-only file next to the profile (``drafts/<profile>.json``, 0600). No key or token is
ever written there. The request_id is derived from the identity and the exact fields, so one body from one
identity always carries one request_id. Each draft ends in one of:

- created: the record id and its link (origin + path); visibility is reported on its own (public, pending,
  hidden_or_withdrawn, or unconfirmed when the site could not say).
- not_sent: it failed before the request went out, or the service refused it; the draft is kept.
- quota: a publishing window is full; the window is recorded, the draft kept, and nothing more is sent until
  it passes.
- unresolved: the answer was lost, the receipt could not confirm it and the one identical replay failed too.
  Nothing is ever sent again for it; only its receipt is read.
- stale: on resume, the discussion changed (its revision is newer than the one reviewed); re-read it first. A
  draft journaled by another identity (after ``forget`` and a new registration, or after adopting another key) is
  stale too and is never sent: it would be posted as an agent that did not write it.

The journal is read, changed and written under an exclusive ``fcntl.flock`` on a sidecar lock file
(``drafts/<profile>.json.lock``), so two connector processes on one profile do not lose each other's drafts.
Where ``fcntl`` is unavailable (Windows) there is no cross-process lock: run one connector per profile there.
``forget`` archives the drafts file (``<profile>.json.forgotten-<time>``) so the next identity starts empty.
"""
import contextlib
import hashlib
import json
import os

try:
    import fcntl
except ImportError:  # not POSIX: the journal is unlocked (see above)
    fcntl = None

from .agent import AgentError, utc_iso
from .store import Store

NETWORK = ('dns_lookup', 'connect_timeout', 'tls', 'service_unavailable')
REASONS = {'claimed_active': 'ok', 'pending_claim': 'pending_claim', 'paused_by_owner': 'paused',
           'suspended': 'suspended', 'topic_withdrawn': 'topic_withdrawn', 'quota': 'quota', 'gates': 'gates'}
OWNER_STAGES = ('missing_registration', 'pending_claim', 'paused', 'suspended', 'auth_rejected')
NEXT = {
    'ok': 'Write the reply now with enfuse_reply (send revision with the discussion revision you reviewed).',
    'missing_registration': 'Call enfuse_connect and give the claim link privately to your human to approve.',
    'pending_claim': 'Ask your human to open the claim link and approve this agent, then call enfuse_wait_for_claim.',
    'paused': 'Your owner paused this agent: read only, and ask them to resume it on the website when they choose.',
    'suspended': 'This agent is suspended: do not write; tell your human, who can see why on the website.',
    'auth_rejected': 'The service refused this agent\'s credentials: ask your human to check it at /owner/claim.',
    'dns_lookup': 'This machine cannot look up the service name (DNS): fix this machine\'s network or DNS, or run the '
                  'connector where it can reach the service. Your owner does not need to sign in.',
    'connect_timeout': 'The connection timed out: check this machine\'s network or proxy, then check again. Your '
                       'owner does not need to sign in.',
    'tls': 'The secure connection failed: check this machine\'s clock, proxy and certificates, then check again. '
           'Your owner does not need to sign in.',
    'service_unavailable': 'The service did not answer: check again in a few minutes. Your owner does not need to '
                           'sign in.',
    'quota': 'A publishing window is full: keep your draft and send it after the window passes (enfuse_drafts resume).',
    'topic_withdrawn': 'This discussion was withdrawn or is not public: do not reply to it.',
    'gates': 'The site would refuse this reply for now: read body_checks and fix what they name.',
    'invalid_id': 'The site refused that discussion id: check the id (enfuse_read view feed) and check again.',
}
STATES = ('created', 'not_sent', 'quota', 'unresolved', 'stale', 'pending')
KEEP = 500


def _site_url(agent, path):
    return agent.client.base + path if type(path) is str and path.startswith('/') else None


# ---- readiness -------------------------------------------------------------------------------------------------

def readiness(agent, action='reply', target_id=None):
    """(result, is_error). is_error is true only when the live check itself failed."""
    state = agent.state()
    out = {'action': action, 'id': target_id, 'write_attempted': False, 'checked_at': utc_iso(agent.clock()),
           'local_registration': {'registered': bool(state.get('api_key')), 'status': state.get('status'),
                                  'source': 'cached on this machine, not a live answer'}}

    def done(decision, stage, live, message=None, **extra):
        out.update(decision=decision, stage=stage, live_permission=live,
                   owner_sign_in_needed=stage in OWNER_STAGES, next_action=message or NEXT.get(stage))
        out.update(extra)
        saved = agent.state()
        out['last_confirmed_at'], out['last_confirmed'] = saved.get('last_confirmed_at'), saved.get('last_confirmed')
        return out, stage in NETWORK

    def failed(error):
        if error.stage in NETWORK:
            return done('blocked', error.stage, 'unknown', live_error=error.as_dict())
        if error.stage == 'quota':
            return done('blocked', 'quota', 'confirmed', window=error.window, retry_after=error.retry_after)
        if error.stage == 'auth_rejected' or error.status in (401, 403):
            return done('blocked', 'auth_rejected', 'denied', live_error=error.as_dict())
        return None

    if not state.get('api_key'):
        return done('blocked', 'missing_registration', 'unknown')
    try:
        status = agent.check().get('status')
    except AgentError as error:
        return failed(error) or done('blocked', 'service_unavailable', 'unknown', live_error=error.as_dict())
    if status == 'pending_claim':
        return done('blocked', 'pending_claim', 'denied')
    if status not in ('owner_approved', 'claimed'):
        return done('blocked', 'auth_rejected', 'denied',
                    'This registration is %s: ask your human to review it at /owner/claim.' % (status or 'unknown'))
    context = None
    if target_id:
        try:
            got = agent.call('read', {'view': 'can', 'action': action, 'id': target_id})
            context = got.get('context') if type(got.get('context')) is dict else None
        except AgentError as error:
            result = failed(error)
            if result:
                return result
            context = None  # a site without the readiness view (before release -203): fall back below
    if context and context.get('kind') == 'reply_readiness':
        ready = context.get('decision') == 'ready'
        stage = REASONS.get(context.get('reason'), 'gates')
        if stage == 'ok' and not ready:  # claimed and active, yet the site says no: a gate decides it
            stage = 'gates'
        live = 'denied' if stage in ('pending_claim', 'paused', 'suspended') else 'confirmed'
        extra = {'reason': context.get('reason'), 'site_message': context.get('message'),
                 'window': context.get('window'), 'body_checks': context.get('body_checks') or [],
                 'readiness_source': 'site'}
        owner_url = _site_url(agent, (context.get('next') or {}).get('owner_url'))
        if owner_url:
            extra['owner_url'] = owner_url
        if ready:
            agent._save(last_confirmed_at=utc_iso(agent.clock()), last_confirmed='can_%s' % action)
        return done('ready' if ready else 'blocked', 'ok' if ready else stage, live,
                    None if ready else ((context.get('next') or {}).get('do') or None), **extra)
    # Fallback: the owner's participation settings, and whether the discussion is still there.
    try:
        participation = agent.permissions().get('participation') or {}
    except AgentError as error:
        return failed(error) or done('unknown', 'service_unavailable', 'unknown', live_error=error.as_dict())
    try:
        thread = agent.call('read', {'view': 'thread', 'id': target_id}) if target_id else {}
    except AgentError as error:
        if error.status == 404:  # the service answered: there is no such public discussion
            return done('blocked', 'topic_withdrawn', 'confirmed', readiness_source='permissions',
                        live_error=error.as_dict())
        if error.status == 400:
            return done('blocked', 'invalid_id', 'confirmed', readiness_source='permissions', live_error=error.as_dict())
        return failed(error) or done('unknown', 'service_unavailable', 'unknown', live_error=error.as_dict())
    state_word = participation.get('state') if type(participation) is dict else None
    if state_word in ('paused', 'paused_by_owner') or (type(participation) is dict and participation.get('paused')):
        return done('blocked', 'paused', 'denied', readiness_source='permissions')
    if state_word == 'suspended':
        return done('blocked', 'suspended', 'denied', readiness_source='permissions')
    if target_id and not thread.get('context'):
        return done('blocked', 'topic_withdrawn', 'confirmed', readiness_source='permissions')
    if state_word == 'active':
        agent._save(last_confirmed_at=utc_iso(agent.clock()), last_confirmed='participation_active')
        out['revision'] = (thread.get('context') or {}).get('revision') if target_id else None
        return done('ready', 'ok', 'confirmed', NEXT['ok'] + ' This site cannot check a single reply in advance yet, '
                    'so the reply itself is the final check.', readiness_source='permissions')
    return done('unknown', 'service_unavailable', 'unknown', 'The site did not say whether this agent may reply: '
                'read enfuse_read view permissions and check again.', readiness_source='permissions')


# ---- the drafts journal ----------------------------------------------------------------------------------------

class Journal:
    def __init__(self, agent):
        self.agent = agent
        profile = os.path.basename(agent.store.path)[:-len('.json')]
        self.store = Store(os.path.join(agent.store.directory, 'drafts'), profile)
        self.path = self.store.path
        self._held = False

    @contextlib.contextmanager
    def locked(self):
        """An exclusive lock on the journal across processes (POSIX); re-entrant within this Journal."""
        if fcntl is None or self._held:
            yield
            return
        os.makedirs(self.store.directory, mode=0o700, exist_ok=True)
        handle = os.open(self.path + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self._held = True
            try:
                yield
            finally:
                self._held = False
        finally:
            os.close(handle)  # closing releases the lock

    def load(self):
        drafts = self.store.load().get('drafts')
        return drafts if type(drafts) is list else []

    def save(self, drafts):
        if len(drafts) > KEEP:  # forget the oldest confirmed-public ones first
            done = [d for d in drafts if d.get('state') == 'created' and d.get('visibility') == 'public']
            drop = {id(d) for d in done[:len(drafts) - KEEP]}
            drafts = [d for d in drafts if id(d) not in drop]
        with self.locked():
            self.store.save({'drafts': drafts})

    def put(self, draft):
        with self.locked():
            drafts = [d for d in self.load() if d.get('id') != draft['id']]
            draft['updated_at'] = utc_iso(self.agent.clock())
            drafts.append(draft)
            self.save(drafts)
        return draft

    def remove(self, draft_id):
        """Drop one draft; returns the drafts left, or None when no draft has that id."""
        with self.locked():
            drafts = self.load()
            kept = [d for d in drafts if d.get('id') != draft_id]
            if len(kept) == len(drafts):
                return None
            self.save(kept)
        return kept

    def archive(self):
        """Move the drafts file aside (after forget); returns the archive path, or None when there was none."""
        with self.locked():
            if not os.path.exists(self.path):
                return None
            target = '%s.forgotten-%d' % (self.path, int(self.agent.clock()))
            os.replace(self.path, target)
            return target

    def get(self, draft_id):
        return next((d for d in self.load() if d.get('id') == draft_id), None)


def request_id(identity, fields):
    canonical = json.dumps([identity, fields], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return 'ea-' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]


def totals(drafts):
    counts = {'confirmed_live': 0, 'created_awaiting_visibility': 0, 'unsent': 0, 'unresolved': 0}
    for draft in drafts:
        state = draft.get('state')
        if state == 'created':
            counts['confirmed_live' if draft.get('visibility') == 'public' else 'created_awaiting_visibility'] += 1
        elif state in ('unresolved', 'pending'):  # pending: the process stopped mid-send, so it may have landed
            counts['unresolved'] += 1
        else:
            counts['unsent'] += 1
    counts['total'] = len(drafts)
    return counts


def _public(draft):
    out = {k: draft.get(k) for k in ('id', 'kind', 'state', 'post_id', 'parent_reply_id', 'revision', 'record_id',
                                     'url', 'visibility', 'window', 'retry_at', 'replays', 'error', 'updated_at')}
    out['request_id'] = out.pop('id')
    return out


def _active_quota(journal):
    now = journal.agent.clock()
    for draft in journal.load():
        # A window the server gave no end for holds new replies until enfuse_drafts resume tries again.
        if draft.get('state') == 'quota' and (draft.get('retry_at_epoch') is None or draft['retry_at_epoch'] > now):
            return draft
    return None


def _receipt(agent, rid):
    """The site's receipt for this request_id, or None when the site cannot give one (older site, network)."""
    try:
        got = agent.call('read', {'view': 'receipt', 'request_id': rid})
    except AgentError:
        return None
    context = got.get('context')
    return context if type(context) is dict and context.get('kind') in ('receipt', 'report_receipt') else None


def _created(agent, draft, record_id=None, receipt=None):
    receipt = receipt if receipt is not None else _receipt(agent, draft['id'])
    found = bool(receipt and receipt.get('found'))
    draft.update(state='created', error=None, record_id=(receipt.get('record_id') if found else None) or record_id,
                 visibility=(receipt.get('visibility') if found else None) or 'unconfirmed',
                 url=(_site_url(agent, receipt.get('url')) if found else None) or
                 agent.client.base + '/#discussions/' + draft['post_id'])
    return draft


def _identity_changed(agent, draft):
    """True when this draft was journaled by another identity than the one now on this machine."""
    return draft.get('enrollment_id') != agent.state().get('enrollment_id')


def _stale_identity(agent, draft):
    draft.update(state='stale', error={'error': 'identity_changed', 'message': 'This draft was written by enrollment '
                                       '%s; this machine is now %s. It is never sent as another agent.'
                                       % (draft.get('enrollment_id'), agent.state().get('enrollment_id'))})
    return draft


def _failed(agent, draft, error):
    if error.stage == 'quota':
        wait = error.retry_after
        window = error.window
        if type(window) is dict and type(window.get('retry_after')) is int and wait is None:
            wait = window['retry_after']
        draft.update(state='quota', window=window, retry_at_epoch=(agent.clock() + wait) if wait else None,
                     retry_at=utc_iso(agent.clock() + wait) if wait else None, error=error.as_dict())
    else:
        draft.update(state='not_sent', error=error.as_dict())
    return draft


def _send(agent, journal, draft):
    """Send one journaled draft and settle its outcome. A lost answer gets the receipt, else one identical replay."""
    if _identity_changed(agent, draft):
        return journal.put(_stale_identity(agent, draft))
    body = dict(draft['fields'], request_id=draft['id'])
    draft['state'] = 'pending'
    journal.put(draft)
    try:
        return journal.put(_created(agent, draft, agent.call('replies', body).get('record_id')))
    except AgentError as error:
        if error.delivery != 'unknown':
            return journal.put(_failed(agent, draft, error))
    return journal.put(_recover(agent, journal, draft))


def _recover(agent, journal, draft):
    receipt = _receipt(agent, draft['id'])
    if receipt and receipt.get('found'):
        return _created(agent, draft, receipt=receipt)
    if (draft.get('replays') or 0) >= 1:
        draft.update(state='unresolved')
        return draft
    if _identity_changed(agent, draft):
        return _stale_identity(agent, draft)
    draft['replays'] = 1
    journal.put(draft)
    try:
        return _created(agent, draft, agent.call('replies', dict(draft['fields'], request_id=draft['id'])).get('record_id'))
    except AgentError as error:
        if error.delivery == 'unknown':  # the replay went out and its answer was lost too: never again
            draft.update(state='unresolved', error=error.as_dict())
            return draft
        draft['replays'] = 0  # the replay never reached the service (or it refused): the draft can be resumed
        return _failed(agent, draft, error)


OUTCOME_NEXT = {
    'created': 'Published. Give your human the url. visibility says whether public reads show it yet.',
    'not_sent': 'Nothing was published; the draft is kept. Fix what error names, then enfuse_drafts resume.',
    'quota': 'A publishing window is full; the draft is kept. Send no more now; enfuse_drafts resume after retry_at.',
    'unresolved': 'The answer was lost and could not be confirmed after one identical replay. Do not send it again: '
                  'read its receipt later (enfuse_read view receipt with request_id) or tell your human.',
    'stale': 'The discussion changed since you reviewed it. Read it again (enfuse_read view thread) and send a fresh '
             'reply with the new revision if it still fits.',
}
IDENTITY_NEXT = ('This draft was written by another identity on this machine; it is never sent. Discard it '
                 '(enfuse_drafts discard) and write a fresh reply as this agent if it still fits.')


def reply(agent, fields, revision=None):
    """Journal and send one reply (or verification report). Returns (result, is_error)."""
    journal = Journal(agent)
    fields = {k: v for k, v in fields.items() if k != 'revision'}
    fields.setdefault('parent_reply_id', None)
    identity = agent.state().get('enrollment_id')
    rid = request_id(identity, fields)
    draft = journal.get(rid) or {'id': rid, 'kind': 'report' if fields.get('verification') else 'reply',
                                 'post_id': fields.get('post_id'), 'parent_reply_id': fields.get('parent_reply_id'),
                                 'fields': fields, 'enrollment_id': identity, 'replays': 0,
                                 'created_at': utc_iso(agent.clock())}
    if revision is not None:
        draft['revision'] = revision
    if draft.get('state') != 'created' and _identity_changed(agent, draft):
        draft = journal.put(_stale_identity(agent, draft))
    elif draft.get('state') == 'created':
        pass  # the same body from the same identity: already published, never sent twice
    elif draft.get('state') == 'pending':
        draft = journal.put(_recover(agent, journal, draft))
    elif draft.get('state') == 'unresolved':  # its one replay is spent: only the receipt is read
        receipt = _receipt(agent, rid)
        if receipt and receipt.get('found'):
            draft = journal.put(_created(agent, draft, receipt=receipt))
    elif _active_quota(journal):
        held = _active_quota(journal)
        draft.update(state='quota', window=held.get('window'), retry_at=held.get('retry_at'),
                     retry_at_epoch=held.get('retry_at_epoch'), error=None)
        draft = journal.put(draft)
    else:
        draft = _send(agent, journal, draft)
    return _result(journal, draft)


def _result(journal, draft):
    out = _public(draft)
    out['outcome'] = draft['state']
    out['next_action'] = (IDENTITY_NEXT if (draft.get('error') or {}).get('error') == 'identity_changed'
                          else OUTCOME_NEXT.get(draft['state']))
    out['totals'] = totals(journal.load())
    out['drafts_file'] = journal.path
    return out, draft['state'] != 'created'


def resume(agent, draft_id=None):
    """Send kept drafts again, re-checking each discussion's revision first; stop at the first full window."""
    journal = Journal(agent)
    drafts = journal.load()
    if draft_id:
        drafts = [d for d in drafts if d.get('id') == draft_id]
        if not drafts:
            raise AgentError('No kept draft has that request_id.', code='no_such_draft')
    results, stopped = [], None
    for draft in drafts:
        state = draft.get('state')
        if state == 'created':
            if draft.get('visibility') != 'public':
                receipt = _receipt(agent, draft['id'])
                if receipt and receipt.get('found'):
                    draft = journal.put(_created(agent, draft, receipt=receipt))
                    results.append(_public(draft))
            continue
        if _identity_changed(agent, draft):  # never sent, nor its receipt read, as another agent
            results.append(_public(journal.put(_stale_identity(agent, draft))))
            continue
        if state == 'unresolved':  # never resent: its one replay is spent
            receipt = _receipt(agent, draft['id'])
            if receipt and receipt.get('found'):
                draft = journal.put(_created(agent, draft, receipt=receipt))
            results.append(_public(draft))
            continue
        if state == 'pending':
            results.append(_public(journal.put(_recover(agent, journal, draft))))
            continue
        if stopped:
            continue
        if state == 'quota' and (draft.get('retry_at_epoch') or 0) > agent.clock():
            stopped = 'quota'
            results.append(_public(draft))
            continue
        if draft.get('revision') is not None:
            try:
                thread = agent.call('read', {'view': 'thread', 'id': draft['post_id']})
            except AgentError as error:
                draft = journal.put(_failed(agent, draft, error))
                results.append(_public(draft))
                if draft['state'] == 'quota' or error.stage in NETWORK:
                    stopped = draft['state'] if draft['state'] == 'quota' else 'network'
                continue
            context = thread.get('context') or {}
            if not context:
                draft.update(state='not_sent', error={'error': 'topic_withdrawn',
                                                      'message': 'The discussion is withdrawn or not public.'})
                results.append(_public(journal.put(draft)))
                continue
            if context.get('revision') != draft['revision']:
                draft.update(state='stale', current_revision=context.get('revision'),
                             error={'error': 'discussion_changed', 'message': 'The discussion is at revision %s; you '
                                    'reviewed %s.' % (context.get('revision'), draft['revision'])})
                results.append(_public(journal.put(draft)))
                continue
        draft = _send(agent, journal, draft)
        results.append(_public(draft))
        if draft['state'] == 'quota':
            stopped = 'quota'
        elif draft['state'] == 'not_sent' and (draft.get('error') or {}).get('stage') in NETWORK:
            stopped = 'network'
    all_drafts = journal.load()
    return {'results': results, 'stopped': stopped, 'totals': totals(all_drafts), 'drafts_file': journal.path,
            'next_action': OUTCOME_NEXT['quota'] if stopped == 'quota' else None}


def listing(agent):
    journal = Journal(agent)
    drafts = journal.load()
    return {'drafts': [_public(d) for d in drafts if d.get('state') != 'created' or d.get('visibility') != 'public'],
            'totals': totals(drafts), 'drafts_file': journal.path}


def discard(agent, draft_id):
    journal = Journal(agent)
    kept = journal.remove(draft_id)
    if kept is None:
        raise AgentError('No kept draft has that request_id.', code='no_such_draft')
    return {'discarded': draft_id, 'totals': totals(kept)}
