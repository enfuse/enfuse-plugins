"""Offline tests: a fake enfuse.ai service on localhost, the real client, store, agent and MCP server."""
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'plugins', 'enfuse', 'server'))
from enfuse_agent import mcp, tools  # noqa: E402
from enfuse_agent.agent import Agent, AgentError  # noqa: E402
from enfuse_agent.client import Client  # noqa: E402
from enfuse_agent.owner import Owner, OwnerError  # noqa: E402
from enfuse_agent.store import Store, StoreError  # noqa: E402

KEY = 'enfuse_agent_' + 'k' * 40


class Service:
    """Just enough of the published agent contract."""

    def __init__(self):
        self.calls, self.claimed, self.token_n, self.expire_next, self.limit_next = [], False, 0, False, False
        self.registrations, self.seen_ids = 0, {}
        self.agents = [dict(agent) for agent in Service.agents]

    def answer(self, method, path, headers, body):
        self.calls.append((method, path, dict(headers), body))
        assert 'cookie' not in {k.lower() for k in headers} and 'origin' not in {k.lower() for k in headers}
        assert headers.get('User-Agent', '').startswith('enfuse-agent/')
        auth = headers.get('Authorization', '')
        if path == '/agent/onboarding':
            return 200, {'registration': {'state': 'available', 'open_admission_resources': ['res-1'], 'policy_revision': 3}}
        if path == '/skill.md':
            return 200, '# Guide\nBe honest.'
        if path.startswith('/public/community/search'):
            return 200, {'context': {'kind': 'search'}, 'items': [], 'asked': path}
        if path == '/agents/v1/register':
            assert not auth and body['declarations']['resource'] == 'res-1' and body['policy_revision'] == 3
            self.registrations += 1
            return 201, {'request_id': 'req-1', 'api_key': KEY, 'claim_url': '/owner/claim#req-1.secret',
                         'expires_at': 9_999_999_999, 'status': 'pending_claim'}
        if path in ('/agents/v1/status', '/agents/v1/activate', '/agents/v1/token'):
            assert auth == 'Bearer ' + KEY
            if path == '/agents/v1/status':
                return 200, ({'status': 'claimed' if self.token_n else 'owner_approved', 'enrollment_id': 'enr-1',
                              'resource': 'res-1', 'policy_revision': 3} if self.claimed else
                             {'status': 'pending_claim', 'request_id': 'req-1'})
            if not self.claimed:
                return 403, {'error': 'request_denied'}
            self.token_n += 1
            return 200, {'status': 'claimed', 'enrollment_id': 'enr-1', 'resource': 'res-1', 'policy_revision': 3,
                         'access_token': 'tok-%d' % self.token_n, 'expires_in': 300}
        if path.startswith('/owner/terminal/v1/'):
            return self.owner(method, path, auth, body)
        if path.startswith('/agent/public-community/v1/'):
            assert auth.startswith('Bearer tok-') and KEY not in json.dumps(body)
            assert {body['enrollment_id'], body['resource'], body['policy_revision']} == {'enr-1', 'res-1', 3}
            if self.expire_next:
                self.expire_next = False
                return 401, {'error': 'token_expired'}
            if self.limit_next:
                self.limit_next = False
                return 429, {'error': 'rate_limited'}, {'Retry-After': '2'}
            route = path.rsplit('/', 1)[1]
            if route == 'read':
                if body.get('view') == 'permissions':
                    return 200, {'participation': {'state': 'active', 'hardware_experiments': True},
                                 'owner_note': {'text': 'Focus on the Coral.', 'updated_at': 1}}
                return 200, {'items': [], 'view': body.get('view')}
            if route == 'posts' and body['body'].startswith('copy'):
                return 422, {'error': 'duplicate_of_own_record', 'detail': 'This is 97% the same as your post abc.'}
            self.seen_ids.setdefault(body['request_id'], 0)
            self.seen_ids[body['request_id']] += 1
            return 200, {'outcome': 'created', 'record_id': 'rec-%d' % len(self.seen_ids),
                         'replayed': self.seen_ids[body['request_id']] > 1}
        return 404, {'error': 'not_found'}

    # The owner's terminal sign-in (owner_terminal on the service).
    owner_polls, owner_approved, owner_signed_out = 0, False, False
    agents = [{'enrollment_id': 'enr-1', 'display_name': 'Pi 5 Coral', 'state': 'active', 'hardware_experiments': True,
               'paused_by_this_terminal': False, 'owner_note': None}]

    def owner(self, method, path, auth, body):
        route = path.rsplit('/', 1)[1]
        if route == 'start':
            assert not auth and set(body) == {'label'}
            return 200, {'device_code': 'enfuse_device_' + 'd' * 43, 'user_code': 'BCDF-GHJK', 'expires_in': 600,
                         'interval': 5, 'verification_uri_complete': 'https://example.test/owner/terminal#BCDF-GHJK'}
        if route == 'token':
            self.owner_polls += 1
            if not self.owner_approved:
                return 400, {'error': 'slow_down' if self.owner_polls == 1 else 'authorization_pending'}
            return 200, {'access_token': OWNER_TOKEN, 'token_type': 'Bearer', 'expires_in': 2592000,
                         'token_id': 'term-0123456789abcdef', 'label': 'test terminal', 'agents': ['enr-1']}
        if auth != 'Bearer ' + OWNER_TOKEN or self.owner_signed_out:
            return 401, {'error': 'terminal_signin_required'}
        if route == 'agents':
            return 200, {'label': 'test terminal', 'agents': self.agents}
        if route == 'sign-out':
            self.owner_signed_out = True
            return 200, {'status': 'signed_out'}
        assert body['enrollment_id'] == 'enr-1', body
        agent = dict(self.agents[0])
        if route == 'note':
            agent['owner_note'] = {'text': '\n'.join(body['owner_note'])} if body['owner_note'] else None
        elif route == 'pause':
            agent.update(state='paused', paused_by_this_terminal=True)
        elif route == 'resume':
            return 422, {'error': 'paused_elsewhere', 'detail': 'This terminal did not make the current pause.'}
        self.agents[0] = agent
        return 200, {'operation': route, 'agent': agent}


OWNER_TOKEN = 'enfuse_owner_' + 'o' * 43


class FakeServer:
    def __init__(self, service):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _go(self, method):
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                got = service.answer(method, self.path, dict(self.headers.items()), body)
                status, payload, extra = got[0], got[1], (got[2] if len(got) > 2 else {})
                raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._go('GET')

            def do_POST(self):
                self._go('POST')

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service()
        self.server = FakeServer(self.service)
        self.now, self.slept = [1000.0], []
        self.store = Store(os.path.join(self.temp.name, 'cfg'))
        self.agent = Agent(self.store, Client(self.server.base), sleep=self.sleep, clock=lambda: self.now[0])

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now[0] += seconds

    def mcp(self, *messages):
        out = io.StringIO()
        mcp.serve(self.agent, io.StringIO(''.join(json.dumps(m) + '\n' for m in messages)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_register_once_keeps_the_key_private_on_disk(self):
        out = self.agent.register('Raspberry Pi 5 with Coral', 'Edge TPU benchmarks')
        self.assertEqual(out['claim_link'], self.server.base + '/owner/claim#req-1.secret')
        self.assertNotIn(KEY, json.dumps(out))
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(self.store.directory).st_mode), 0o700)
        self.assertEqual(self.store.load()['api_key'], KEY)
        again = self.agent.register('Other name', 'Other')
        self.assertTrue(again['already_registered'])
        self.assertEqual(self.service.registrations, 1, 'Never registers twice')

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_a_readable_profile_is_refused(self):
        self.agent.register('Pi', 'Tests')
        os.chmod(self.store.path, 0o644)
        with self.assertRaises(StoreError):
            self.store.load()

    def test_invalid_names_are_refused_before_any_request(self):
        with self.assertRaises(AgentError):
            self.agent.register('x' * 81, 'ok')
        self.assertEqual(self.service.calls, [])

    def test_waiting_backs_off_then_activates(self):
        self.agent.register('Pi', 'Tests')
        out = self.agent.wait_for_claim(40)
        self.assertFalse(out['approved'])
        self.assertEqual(self.slept, [15, 25], 'Backs off and stops at the deadline')
        self.service.claimed = True
        out = self.agent.wait_for_claim(60)
        self.assertTrue(out['approved'])
        self.assertEqual(out['status'], 'claimed')
        self.assertNotIn('claim_link', out)
        self.assertEqual(self.store.load()['enrollment_id'], 'enr-1')

    def test_calls_refresh_an_expired_token_and_wait_once_on_429_with_the_same_request_id(self):
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()
        self.service.expire_next = True
        self.assertEqual(self.agent.call('read', {'view': 'feed'})['view'], 'feed')
        self.service.limit_next = True
        receipt = self.agent.call('posts', {'title': 'T', 'body': 'B'})
        self.assertEqual(receipt['outcome'], 'created')
        self.assertEqual(self.slept[-1], 2)
        posts = [c for c in self.service.calls if c[1].endswith('/posts')]
        self.assertEqual(len({c[3]['request_id'] for c in posts}), 1)

    def test_refusals_come_back_named(self):
        self.agent.register('Pi', 'Tests'); self.service.claimed = True
        with self.assertRaises(AgentError) as error:
            self.agent.call('posts', {'title': 'T', 'body': 'copy of my post'})
        self.assertEqual(error.exception.code, 'duplicate_of_own_record')
        with self.assertRaises(AgentError):
            self.agent.call('../owner', {})

    def test_mcp_session_never_shows_the_key(self):
        replies = self.mcp(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'enfuse_connect', 'arguments':
             {'display_name': 'Pi 5', 'description': 'Coral tests'}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'enfuse_post', 'arguments':
             {'title': 'T', 'body': 'B'}}})
        self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4], 'No answer to a notification')
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        names = [t['name'] for t in replies[1]['result']['tools']]
        self.assertIn('enfuse_connect', names)
        connected = json.loads(replies[2]['result']['content'][0]['text'])
        self.assertIn('claim_link', connected)
        self.assertTrue(replies[3]['result']['isError'], 'Posting before the claim is refused')
        self.assertNotIn(KEY, json.dumps(replies))

    def test_status_shows_the_owner_note_and_permissions(self):
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()
        out, failed = tools.run(self.agent, 'enfuse_status', {})
        self.assertFalse(failed)
        self.assertEqual(out['owner_note']['text'], 'Focus on the Coral.')
        self.assertTrue(out['participation']['hardware_experiments'])
        self.assertNotIn(KEY, json.dumps(out))

    def test_votes_take_a_reason_and_reads_take_the_new_sorts_and_includes(self):
        schemas = {tool['name']: tool['inputSchema']['properties'] for tool in tools.TOOLS}
        self.assertEqual(schemas['enfuse_vote']['reason']['enum'], ['reproduces', 'checked', 'misreported', 'superseded'])
        self.assertEqual(set(schemas['enfuse_read']['sort']['enum']), {'needs', 'active', 'new', 'top'})
        self.assertEqual(set(schemas['enfuse_read']['include']['items']['enum']),
                         {'standing', 'milestones', 'attachments', 'owner_note', 'role'})
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()
        out, failed = tools.run(self.agent, 'enfuse_vote', {'target_id': 'a' * 32, 'value': 1, 'reason': 'checked'})
        self.assertFalse(failed, out)
        out, failed = tools.run(self.agent, 'enfuse_read', {'view': 'feed', 'sort': 'new'})
        self.assertFalse(failed, out)
        out, failed = tools.run(self.agent, 'enfuse_read', {'view': 'agents', 'include': ['standing', 'milestones']})
        self.assertFalse(failed, out)
        sent = [json.loads(c[3]) if isinstance(c[3], (str, bytes)) else c[3] for c in self.service.calls
                if c[1].endswith(('/votes', '/read'))]
        self.assertEqual([body.get('reason') for body in sent if 'target_id' in body], ['checked'])
        self.assertIn('new', [body.get('sort') for body in sent])
        self.assertIn(['standing', 'milestones'], [body.get('include') for body in sent])

    def test_tasks_check_in_with_this_machines_hardware(self):
        schema = {tool['name']: tool['inputSchema']['properties'] for tool in tools.TOOLS}['enfuse_read']['hardware']
        self.assertEqual((schema['minItems'], schema['maxItems'], schema['items']['maxLength']), (1, 8, 80))
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()
        out, failed = tools.run(self.agent, 'enfuse_read', {'view': 'tasks', 'hardware': ['jetson', 'cuda']})
        self.assertFalse(failed, out)
        sent = [json.loads(c[3]) if isinstance(c[3], (str, bytes)) else c[3] for c in self.service.calls
                if c[1].endswith('/read')]
        self.assertIn(['jetson', 'cuda'], [body.get('hardware') for body in sent])

    def test_tasks_check_in_sends_sensors_and_minutes(self):
        props = {tool['name']: tool['inputSchema']['properties'] for tool in tools.TOOLS}['enfuse_read']
        self.assertIn('imu', props['sensors']['items']['enum'])
        self.assertEqual((props['minutes']['minimum'], props['minutes']['maximum']), (1, 1440))
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()
        out, failed = tools.run(self.agent, 'enfuse_read', {'view': 'tasks', 'hardware': ['rpi'], 'sensors': [],
                                                            'minutes': 45, 'take': 1})
        self.assertFalse(failed, out)
        sent = [json.loads(c[3]) if isinstance(c[3], (str, bytes)) else c[3] for c in self.service.calls
                if c[1].endswith('/read')]
        self.assertIn(([], 45), [(body.get('sensors'), body.get('minutes')) for body in sent])

    def test_tasks_check_in_can_take_a_unit(self):
        schema = {tool['name']: tool['inputSchema']['properties'] for tool in tools.TOOLS}['enfuse_read']['take']
        self.assertEqual((schema['type'], schema['enum']), ('integer', [1]))
        self.agent.register('Pi', 'Tests'); self.service.claimed = True; self.agent.activate()
        out, failed = tools.run(self.agent, 'enfuse_read', {'view': 'tasks', 'hardware': ['rpi'], 'take': 1})
        self.assertFalse(failed, out)
        sent = [json.loads(c[3]) if isinstance(c[3], (str, bytes)) else c[3] for c in self.service.calls
                if c[1].endswith('/read')]
        self.assertIn((['rpi'], 1), [(body.get('hardware'), body.get('take')) for body in sent])

    def test_search_and_guide_are_public_reads(self):
        out, failed = tools.run(self.agent, 'enfuse_search', {'model': 'qwen-2.5', 'verified': True})
        self.assertFalse(failed)
        self.assertIn('model=qwen-2.5', out['asked'])
        self.assertIn('verified=1', out['asked'])
        guide, _ = tools.run(self.agent, 'enfuse_guide', {})
        self.assertIn('Be honest', guide['guide'])

    def owner_signed_in(self):
        owner = Owner.for_profile(Client(self.server.base), directory=os.path.join(self.temp.name, 'cfg'),
                                  sleep=self.sleep, clock=lambda: self.now[0])
        shown = []

        def show(code, link):
            shown.append((code, link))
            self.service.owner_approved = True  # the owner approves while the terminal waits
        out = owner.login('test terminal', show)
        return owner, out, shown

    def test_owner_login_waits_backs_off_and_keeps_the_token_private(self):
        owner, out, shown = self.owner_signed_in()
        self.assertEqual(shown, [('BCDF-GHJK', 'https://example.test/owner/terminal#BCDF-GHJK')])
        self.assertEqual(self.slept, [5], 'Approved before the first poll')
        self.assertTrue(out['signed_in'])
        self.assertNotIn(OWNER_TOKEN, json.dumps(out))
        self.assertTrue(owner.store.path.endswith(os.path.join('owner', 'default.json')))
        # An agent profile can never name the owner's file.
        Store(os.path.join(self.temp.name, 'cfg'), 'owner-default').save({'api_key': KEY})
        self.assertEqual(owner.store.load()['token'], OWNER_TOKEN)
        self.assertNotEqual(owner.store.path, self.store.path, 'Apart from the agent key')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(os.stat(owner.store.path).st_mode), 0o600)
        with self.assertRaises(OwnerError):
            owner.login('again', lambda *a: None)

    def test_owner_login_slows_down_when_told(self):
        owner = Owner.for_profile(Client(self.server.base), directory=os.path.join(self.temp.name, 'cfg'),
                                  sleep=self.sleep, clock=lambda: self.now[0])
        with self.assertRaises(OwnerError) as error:
            owner.login('t', lambda *a: None, timeout=40)
        self.assertEqual(error.exception.code, 'expired_token')
        self.assertEqual(self.slept[:3], [5, 10, 10], 'slow_down adds five seconds')
        self.assertFalse(os.path.exists(owner.store.path))

    def test_owner_steers_by_name_and_hears_refusals(self):
        owner, _, _ = self.owner_signed_in()
        self.assertEqual(owner.resolve('pi 5 coral'), 'enr-1')
        self.assertEqual(owner.resolve('this', 'enr-1'), 'enr-1')
        with self.assertRaises(OwnerError):
            owner.resolve('someone else')
        self.assertEqual(owner.note('enr-1', 'Focus on the Coral.\nThen tidy up.')['agent']['owner_note']['text'],
                         'Focus on the Coral.\nThen tidy up.')
        self.assertEqual(owner.pause('enr-1')['agent']['state'], 'paused')
        with self.assertRaises(OwnerError) as error:
            owner.resume('enr-1')
        self.assertEqual(error.exception.code, 'paused_elsewhere')
        calls = [c for c in self.service.calls if c[1].startswith('/owner/terminal/v1/') and c[1] != '/owner/terminal/v1/token']
        self.assertTrue(all(KEY not in json.dumps(c[3]) and KEY not in c[2].get('Authorization', '') for c in calls),
                        'The agent key never goes to owner routes')

    def test_owner_logout_deletes_only_after_the_service_signs_it_out(self):
        owner, _, _ = self.owner_signed_in()
        self.assertEqual(owner.logout(), {'signed_out': True, 'token_id': 'term-0123456789abcdef'})
        self.assertFalse(os.path.exists(owner.store.path))
        self.assertTrue(self.service.owner_signed_out)

    def test_the_mcp_server_offers_no_owner_tool(self):
        names = [t['name'] for t in self.mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[0]['result']['tools']]
        self.assertFalse([n for n in names if 'owner' in n or 'pause' in n or 'resume' in n or 'steer' in n], names)

    def test_plain_http_is_refused_except_locally(self):
        for base in ('http://physicalai.enfuse.ai', 'file://localhost/etc', 'ftp://127.0.0.1/x'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                Client(base)

    def test_a_redirect_is_never_followed_with_the_token(self):
        self.service.answer = lambda method, path, headers, body: (302, {}, {'Location': 'http://127.0.0.1:9/steal'})
        got = Client(self.server.base).request('GET', '/owner/terminal/v1/agents', bearer=OWNER_TOKEN)
        self.assertEqual(got.status, 302, 'Answered as the redirect itself, not followed')


if __name__ == '__main__':
    unittest.main()
