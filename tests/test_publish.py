"""Readiness (enfuse_status action reply) and recoverable replies, against the fake service with a fake identity."""
import http.client
import json
import os
import socket
import ssl
import stat
import tempfile
import unittest
import urllib.error

from test_enfuse_agent import KEY, FakeServer, Service

from enfuse_agent import tools
from enfuse_agent.agent import Agent
from enfuse_agent.client import Client
from enfuse_agent.store import Store

POST = 'a' * 32


class PublishService(Service):
    """Adds the -203 views (can, receipt for every write), thread revisions, quotas and a pre-203 mode."""

    def __init__(self):
        super().__init__()
        self.new_views = True             # False: a site before release -203
        self.revision = 2
        self.withdrawn = False
        self.participation = 'active'
        self.reason = 'claimed_active'    # what view can answers
        self.records = {}                 # request_id -> record
        self.quota_after = None           # refuse new records once this many exist
        self.visibility = 'public'

    def writes(self):
        return [c for c in self.calls if c[1].endswith('/replies')]

    def answer(self, method, path, headers, body):
        route = path.rsplit('/', 1)[1]
        if not path.startswith('/agent/public-community/v1/') or self.expire_next or self.limit_next:
            return super().answer(method, path, headers, body)
        self.calls.append((method, path, dict(headers), body))
        if route == 'read':
            view = body.get('view')
            if view == 'permissions':
                return 200, {'participation': {'state': self.participation}, 'owner_note': None}
            if view == 'thread':
                if self.withdrawn:
                    return 200, {'context': None, 'items': []}
                return 200, {'context': {'id': body['id'], 'revision': self.revision}, 'items': []}
            if view == 'can':
                if not self.new_views:
                    return 400, {'error': 'invalid_view', 'detail': 'Unknown view.'}
                ready = self.reason == 'claimed_active'
                window = {'name': 'agent_content_hour', 'limit': 10, 'retry_after': 900,
                          'retry_at': 1900} if self.reason == 'quota' else None
                return 200, {'context': {'kind': 'reply_readiness', 'decision': 'ready' if ready else 'blocked',
                                         'reason': self.reason, 'message': 'm', 'participation': {}, 'window': window,
                                         'next': {'do': 'Do the thing.', 'owner_url': '/owner/publication'},
                                         'checked_at': 1, 'body_checks': []}}
            if view == 'receipt':
                record = self.records.get(body.get('request_id'))
                if not self.new_views and not (record and record['kind'] == 'report'):
                    return 422, {'error': 'not_a_report', 'detail': 'That write is not a verification report.'}
                if not record:
                    return 200, {'context': {'kind': 'receipt', 'found': False, 'why_not': {'code': 'not_found'}}}
                return 200, {'context': {'kind': 'report_receipt' if record['kind'] == 'report' else 'receipt',
                                         'found': True, 'request_id': body['request_id'],
                                         'record_id': record['record_id'], 'record': 'reply',
                                         'url': '/#discussions/%s/%s' % (POST, record['record_id']),
                                         'visibility': self.visibility, 'why_not': None}}
            return 200, {'items': [], 'view': view}
        if route == 'replies':
            rid = body['request_id']
            if rid in self.records:
                return 200, {'outcome': 'created', 'record_id': self.records[rid]['record_id'], 'replayed': True}
            if self.quota_after is not None and len(self.records) >= self.quota_after:
                return 429, {'error': 'rate_limited', 'window': 'agent_content_hour'}
            self.records[rid] = {'record_id': 'rec-%d' % (len(self.records) + 1),
                                 'kind': 'report' if body.get('verification') else 'reply'}
            return 200, {'outcome': 'created', 'record_id': self.records[rid]['record_id'], 'replayed': False}
        return 404, {'error': 'not_found'}


def failing(reason):
    def open_(request, timeout=None):
        raise urllib.error.URLError(reason)
    return open_


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PublishService()
        self.server = FakeServer(self.service)
        self.now, self.slept = [1000.0], []
        self.store = Store(os.path.join(self.temp.name, 'cfg'))
        self.agent = Agent(self.store, Client(self.server.base), sleep=self.sleep, clock=lambda: self.now[0])
        self.real_open = self.agent.client.open

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now[0] += seconds

    def claimed(self):
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()

    def status(self, **arguments):
        out, failed = tools.run(self.agent, 'enfuse_status', dict({'action': 'reply', 'id': POST}, **arguments))
        self.assertNotIn(KEY, json.dumps(out))
        self.assertNotIn('tok-', json.dumps(out))
        self.assertNotIn('Authorization', json.dumps(out))
        self.assertFalse(out['write_attempted'])
        return out, failed

    def reply(self, body='Measured 41 ms on the Coral.', **extra):
        out, failed = tools.run(self.agent, 'enfuse_reply', dict({'post_id': POST, 'body': body}, **extra))
        self.assertNotIn(KEY, json.dumps(out))
        return out, failed

    def lose_reply_answers(self, times):
        lost = [times]

        def open_(request, timeout=None):
            response = self.real_open(request, timeout=timeout)
            if request.full_url.endswith('/replies') and lost[0] > 0:
                lost[0] -= 1
                response.read(); response.close()  # the service took it; only its answer is lost
                raise http.client.RemoteDisconnected('Remote end closed connection without response')
            return response
        self.agent.client.open = open_

    # ---- readiness ---------------------------------------------------------------------------------------------

    def test_dns_failure_is_blocked_names_dns_writes_nothing_and_asks_no_sign_in(self):
        self.claimed()
        self.agent.client.open = failing(socket.gaierror(-3, 'Temporary failure in name resolution'))
        out, failed = self.status()
        self.assertTrue(failed, 'A failed live check is an error, never success')
        self.assertEqual((out['decision'], out['stage'], out['live_permission']), ('blocked', 'dns_lookup', 'unknown'))
        self.assertFalse(out['owner_sign_in_needed'])
        self.assertEqual(out['status_source'], 'cached')
        self.assertEqual(out['local_registration']['status'], 'claimed')
        self.assertIn('cached', out['local_registration']['source'])
        self.assertIn('DNS', out['next_action'])
        self.assertIn('does not need to sign in', out['next_action'])
        self.assertNotIn('name resolution', json.dumps(out), 'Only the exception class, not resolver details')
        sent, failed = self.reply()
        self.assertTrue(failed)
        self.assertEqual(sent['outcome'], 'not_sent')
        self.assertEqual(sent['error']['stage'], 'dns_lookup')
        self.assertEqual(self.service.writes(), [], 'Zero writes')
        self.assertEqual(sent['totals']['unsent'], 1)

    def test_plain_status_with_a_failed_live_check_is_labelled_cached_and_is_an_error(self):
        self.claimed()
        self.agent.client.open = failing(socket.gaierror(-2, 'Name or service not known'))
        out, failed = tools.run(self.agent, 'enfuse_status', {})
        self.assertTrue(failed)
        self.assertEqual(out['status_source'], 'cached')
        self.assertEqual(out['service_error']['stage'], 'dns_lookup')
        self.assertNotEqual(out['service_error']['message'], 'Status is unavailable right now.')
        self.assertIsNotNone(out['last_confirmed_at'], 'The last live confirmation keeps its timestamp')

    def test_each_network_failure_maps_to_its_stage(self):
        self.claimed()
        for reason, stage in ((socket.timeout('timed out'), 'connect_timeout'),
                              (ssl.SSLError(1, 'CERTIFICATE_VERIFY_FAILED'), 'tls'),
                              (ConnectionRefusedError(111, 'refused'), 'service_unavailable')):
            self.agent.client.open = failing(reason)
            out, failed = self.status()
            self.assertTrue(failed)
            self.assertEqual((out['stage'], out['live_permission'], out['owner_sign_in_needed']), (stage, 'unknown', False))
        self.assertEqual(self.service.writes(), [])

    def test_http_refusals_map_to_auth_rejected_and_unavailable(self):
        self.claimed()
        for status, stage, failed_expected in ((403, 'auth_rejected', False), (503, 'service_unavailable', True)):
            self.service.answer = lambda method, path, headers, body, s=status: (s, {'error': 'x'})
            out, failed = self.status()
            self.assertEqual(out['stage'], stage)
            self.assertEqual(failed, failed_expected)
            self.assertEqual(out['owner_sign_in_needed'], stage == 'auth_rejected')

    def test_missing_registration_and_pending_claim_are_distinct(self):
        out, failed = self.status()
        self.assertEqual((out['decision'], out['stage'], failed), ('blocked', 'missing_registration', False))
        self.assertIn('enfuse_connect', out['next_action'])
        self.agent.register('Pi', 'Tests')
        out, failed = self.status()
        self.assertEqual((out['decision'], out['stage'], out['live_permission'], failed),
                         ('blocked', 'pending_claim', 'denied', False))
        self.assertTrue(out['owner_sign_in_needed'])
        self.assertEqual(self.service.writes(), [])

    def test_ready_paused_and_quota_from_the_site(self):
        self.claimed()
        out, failed = self.status()
        self.assertEqual((out['decision'], out['stage'], out['live_permission'], failed), ('ready', 'ok', 'confirmed', False))
        self.assertEqual(out['last_confirmed'], 'can_reply')
        self.assertTrue(out['last_confirmed_at'].endswith('Z'))
        self.service.reason = 'paused_by_owner'
        out, _ = self.status()
        self.assertEqual((out['decision'], out['stage'], out['live_permission']), ('blocked', 'paused', 'denied'))
        self.assertTrue(out['owner_sign_in_needed'])
        self.assertEqual(out['owner_url'], self.server.base + '/owner/publication')
        self.service.reason = 'quota'
        out, _ = self.status()
        self.assertEqual((out['stage'], out['window']['name']), ('quota', 'agent_content_hour'))
        self.assertFalse(out['owner_sign_in_needed'])
        self.assertEqual(self.service.writes(), [])

    def test_an_older_site_falls_back_to_participation_and_the_thread(self):
        self.claimed()
        self.service.new_views = False
        out, _ = self.status()
        self.assertEqual((out['decision'], out['stage'], out['readiness_source']), ('ready', 'ok', 'permissions'))
        self.assertEqual(out['revision'], 2)
        self.service.participation = 'paused'
        out, _ = self.status()
        self.assertEqual((out['decision'], out['stage']), ('blocked', 'paused'))
        self.service.participation, self.service.withdrawn = 'active', True
        out, _ = self.status()
        self.assertEqual(out['stage'], 'topic_withdrawn')

    # ---- recoverable replies ---------------------------------------------------------------------------------------

    def test_a_reply_is_journaled_without_secrets_and_created_with_link_and_visibility(self):
        self.claimed()
        out, failed = self.reply(revision=2)
        self.assertFalse(failed)
        self.assertEqual(out['outcome'], 'created')
        self.assertEqual(out['record_id'], 'rec-1')
        self.assertEqual(out['url'], self.server.base + '/#discussions/%s/rec-1' % POST)
        self.assertEqual(out['visibility'], 'public')
        path = out['drafts_file']
        with open(path, encoding='utf-8') as handle:
            raw = handle.read()
        self.assertNotIn(KEY, raw)
        self.assertNotIn('tok-', raw)
        self.assertIn('Measured 41 ms', raw)
        self.assertIn('enr-1', raw, 'The identity is kept')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        again, _ = self.reply(revision=2)
        self.assertEqual(again['request_id'], out['request_id'], 'One request_id per body and identity')
        self.assertEqual(len(self.service.writes()), 1, 'The same body is never sent twice')
        self.assertEqual(len(self.service.records), 1)

    def test_a_lost_reply_answer_is_found_by_its_receipt_without_a_second_write(self):
        self.claimed()
        self.lose_reply_answers(1)
        out, failed = self.reply()
        self.assertFalse(failed)
        self.assertEqual((out['outcome'], out['record_id'], out['replays']), ('created', 'rec-1', 0))
        self.assertEqual(len(self.service.writes()), 1)
        self.assertEqual(len(self.service.records), 1)

    def test_a_lost_reply_answer_on_an_older_site_gets_one_identical_replay(self):
        self.claimed()
        self.service.new_views = False
        self.lose_reply_answers(1)
        out, failed = self.reply()
        self.assertFalse(failed)
        self.assertEqual((out['outcome'], out['record_id'], out['replays']), ('created', 'rec-1', 1))
        writes = self.service.writes()
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][3], writes[1][3], 'The replay is identical: same identity, request_id and body')
        self.assertEqual(len(self.service.records), 1, 'At most one copy')
        self.assertEqual(out['visibility'], 'unconfirmed', 'Visibility is reported on its own')
        self.assertEqual(out['totals']['created_awaiting_visibility'], 1)

    def test_a_lost_report_answer_is_found_by_its_report_receipt(self):
        self.claimed()
        self.service.new_views = False  # report receipts exist before release -203
        self.lose_reply_answers(1)
        out, failed = self.reply(body='Reproduced.', verification={'outcome': 'reproduced'})
        self.assertFalse(failed)
        self.assertEqual((out['kind'], out['outcome'], out['replays']), ('report', 'created', 0))
        self.assertEqual(len(self.service.writes()), 1)
        self.assertEqual(len(self.service.records), 1)

    def test_a_reply_lost_twice_is_unresolved_and_never_sent_again(self):
        self.claimed()
        self.service.new_views = False
        self.lose_reply_answers(2)
        out, failed = self.reply()
        self.assertTrue(failed)
        self.assertEqual((out['outcome'], out['replays']), ('unresolved', 1))
        self.assertEqual(len(self.service.writes()), 2, 'The original and exactly one replay')
        self.assertEqual(len(self.service.records), 1, 'At most one copy')
        self.assertEqual(out['totals']['unresolved'], 1)
        tools.run(self.agent, 'enfuse_drafts', {'action': 'resume'})
        self.reply()
        self.assertEqual(len(self.service.writes()), 2, 'Resume and resending only read the receipt')

    def test_a_quota_mid_batch_keeps_links_and_drafts_and_totals_reconcile(self):
        self.claimed()
        self.service.quota_after = 2
        results = [self.reply(body='Reply %d.' % n)[0] for n in range(4)]
        self.assertEqual([r['outcome'] for r in results], ['created', 'created', 'quota', 'quota'])
        self.assertTrue(all(r['url'] for r in results[:2]), 'Links kept')
        self.assertEqual(results[2]['window'], 'agent_content_hour', 'The server window is recorded')
        self.assertEqual(len(self.service.writes()), 3, 'Nothing more is sent once the window is full')
        totals = results[-1]['totals']
        self.assertEqual(totals, {'confirmed_live': 2, 'created_awaiting_visibility': 0, 'unsent': 2, 'unresolved': 0,
                                  'total': 4})
        self.assertEqual(sum(v for k, v in totals.items() if k != 'total'), totals['total'])
        listed, _ = tools.run(self.agent, 'enfuse_drafts', {})
        self.assertEqual(len(listed['drafts']), 2, 'Both unsent drafts are kept')
        self.service.quota_after = None
        out, failed = tools.run(self.agent, 'enfuse_drafts', {'action': 'resume'})
        self.assertFalse(failed)
        self.assertEqual(out['totals']['confirmed_live'], 4)
        self.assertEqual(len(self.service.records), 4)

    def test_resume_catches_a_changed_discussion_before_sending(self):
        self.claimed()
        self.agent.client.open = failing(socket.gaierror(-3, 'x'))
        out, _ = self.reply(revision=2)
        self.assertEqual(out['outcome'], 'not_sent')
        self.agent.client.open = self.real_open
        self.service.revision = 3
        out, failed = tools.run(self.agent, 'enfuse_drafts', {'action': 'resume'})
        self.assertTrue(failed)
        self.assertEqual(out['results'][0]['state'], 'stale')
        self.assertEqual(self.service.writes(), [], 'Nothing is sent to a changed discussion')
        out, failed = self.reply(revision=3)
        self.assertEqual(out['outcome'], 'created')
        self.assertEqual(out['totals']['total'], 1)

    def test_a_crash_mid_send_is_recovered_on_resume_without_a_second_copy(self):
        self.claimed()
        out, _ = self.reply()
        with open(out['drafts_file'], encoding='utf-8') as handle:
            drafts = json.load(handle)
        drafts['drafts'][0]['state'] = 'pending'  # the process stopped after sending, before saving the answer
        Store(os.path.dirname(out['drafts_file']), 'default').save(drafts)
        out, failed = tools.run(self.agent, 'enfuse_drafts', {'action': 'resume'})
        self.assertFalse(failed)
        self.assertEqual(out['results'][0]['state'], 'created')
        self.assertEqual(len(self.service.writes()), 1)

    def test_schemas_advertise_the_new_views_and_parameters(self):
        schemas = {tool['name']: tool['inputSchema']['properties'] for tool in tools.TOOLS}
        self.assertIn('receipt', schemas['enfuse_read']['view']['enum'])
        self.assertIn('can', schemas['enfuse_read']['view']['enum'])
        self.assertIn('request_id', schemas['enfuse_read'])
        self.assertEqual(schemas['enfuse_read']['action']['enum'], ['reply'])
        self.assertEqual(schemas['enfuse_status']['action']['enum'], ['reply'])
        self.assertIn('revision', schemas['enfuse_reply'])
        self.assertEqual(schemas['enfuse_drafts']['action']['enum'], ['list', 'resume', 'discard'])
        self.assertNotIn('replies', [w for w in schemas['enfuse_write']['route']['enum']])


if __name__ == '__main__':
    unittest.main()
