"""로그인(계정·잠금·세션)과 웹 서버(접근 통제, CSRF 방지, 보안 헤더, 채팅 흐름, 대화 분리)."""
import json
import unittest

from starlette.testclient import TestClient

from chat import agent, auth, web
from chat.llm import LlmError
from chat.settings import Agent, Llm, Settings, Web

ORIGIN = {'origin': 'http://localhost'}
API = {**ORIGIN, 'x-requested-with': 'rnd-chat'}
PASSWORD = 'correct horse battery'
HASH = auth.hash_password(PASSWORD)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class AuthTests(unittest.TestCase):
    def test_passwords(self):
        users = auth.Users([{'name': 'kim', 'password_hash': HASH}, {'name': 'lee', 'password': 'plain-password'}])
        self.assertTrue(users.verify('kim', PASSWORD))
        self.assertFalse(users.verify('kim', PASSWORD + 'x'))
        self.assertTrue(users.verify('lee', 'plain-password'))
        self.assertFalse(users.verify('lee', ''))
        self.assertFalse(users.verify('nobody', PASSWORD))
        self.assertEqual((len(users), users.plaintext), (2, ['lee']))
        self.assertNotEqual(HASH, auth.hash_password(PASSWORD))               # 소금이 매번 달라 같은 비밀번호도 해시가 다르다
        self.assertNotIn(PASSWORD, HASH)
        for bad in ([{'name': 'a'}], [{'password': 'x'}], [{'name': 'a', 'password_hash': 'md5$abc'}],
                    [{'name': 'a', 'password': 'x'}, {'name': 'a', 'password': 'y'}]):
            with self.assertRaises(ValueError):
                auth.Users(bad)
        self.assertFalse(auth._check_hash('x', 'scrypt$broken'))

    def test_throttle_locks_and_backs_off(self):
        clock = Clock()
        throttle = auth.Throttle(limit=3, lock=60, max_lock=200, clock=clock)
        for _ in range(2):
            throttle.failure('user:kim')
        self.assertEqual(throttle.locked_for('user:kim'), 0)
        throttle.failure('user:kim')
        self.assertEqual(throttle.locked_for('user:kim'), 60)
        self.assertEqual(throttle.locked_for('user:lee'), 0)
        clock.now += 61
        self.assertEqual(throttle.locked_for('user:kim'), 0)
        throttle.failure('user:kim')                                          # 풀린 뒤 또 틀리면 두 배
        self.assertEqual(throttle.locked_for('user:kim'), 120)
        for _ in range(5):
            throttle.failure('user:kim')
        self.assertEqual(throttle.locked_for('user:kim'), 200)                # 상한
        throttle.success('user:kim')
        self.assertEqual(throttle.locked_for('user:kim'), 0)

    def test_sessions_expire_and_are_capped_per_user(self):
        clock = Clock()
        sessions = auth.Sessions(idle=100, lifetime=250, per_user=2, clock=clock)
        first = sessions.create('kim')
        self.assertEqual(sessions.get(first).user, 'kim')
        self.assertIsNone(sessions.get('guess'))
        self.assertIsNone(sessions.get(None))
        clock.now += 90
        self.assertIsNotNone(sessions.get(first))                             # 쓰는 동안은 유지
        clock.now += 90
        self.assertIsNotNone(sessions.get(first))
        clock.now += 90
        self.assertIsNone(sessions.get(first))                                # 쓰고 있어도 총 수명을 넘으면 끝
        second = sessions.create('kim')
        clock.now += 101
        self.assertIsNone(sessions.get(second))                               # 쉬는 시간 초과
        tokens = [sessions.create('kim') for _ in range(3)]
        self.assertEqual([sessions.get(token) is not None for token in tokens], [False, True, True])
        sessions.delete(tokens[1])
        self.assertIsNone(sessions.get(tokens[1]))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.error = None

        async def runner(messages, listener):
            self.calls.append(messages)
            if self.error:
                raise self.error
            await listener({'type': 'tool_call', 'name': 'count_projects', 'arguments': {'group_by': 'year'}})
            await listener({'type': 'tool_result', 'name': 'count_projects', 'is_error': False, 'chars': 12, 'images': 0})
            answer = f'답 {len(self.calls)}: <img src=x onerror=alert(1)>'
            await listener({'type': 'answer', 'text': answer, 'stopped': ''})
            return agent.Outcome(answer, messages, 2)
        self.settings = Settings(llm=Llm('http://llm.test/v1', 'hai'), agent=Agent(), servers=(), web=Web(max_question_chars=200))
        self.users = auth.Users([{'name': 'kim', 'password_hash': HASH}, {'name': 'lee', 'password': 'plain-password'}])
        self.throttle = auth.Throttle(limit=3)
        self.app = web.create_app(self.settings, self.users, runner=runner, throttle=self.throttle)

    def client(self, user=None, password=PASSWORD, **options):
        client = TestClient(self.app, base_url='http://localhost', follow_redirects=False, **options)
        if user:
            response = client.post('/login', data={'username': user, 'password': password}, headers=ORIGIN)
            self.assertEqual((response.status_code, response.headers['location']), (303, '/'))
        return client

    def ask(self, client, message, conversation_id=None, headers=API):
        response = client.post('/api/chat', json={'message': message, 'conversation_id': conversation_id}, headers=headers)
        events = [json.loads(line[6:]) for line in response.text.split('\n') if line.startswith('data: ')] if response.status_code == 200 else []
        return response, events

    def test_everything_but_login_needs_a_session(self):
        client = self.client()
        response = client.get('/')
        self.assertEqual((response.status_code, response.headers['location']), (303, '/login'))
        for path in ('/api/me', '/api/conversations', '/api/conversations/abc'):
            self.assertEqual(client.get(path).status_code, 401, path)
        self.assertEqual(self.ask(client, '질문')[0].status_code, 401)
        self.assertEqual(client.delete('/api/conversations/abc', headers=API).status_code, 401)
        self.assertEqual(client.get('/login').status_code, 200)
        self.assertEqual(client.get('/static/style.css').status_code, 200)
        self.assertEqual(client.get('/static/app.js').headers['content-type'], 'text/javascript; charset=utf-8')
        for name in ('index.html', '..%2Fweb.py', 'nothing.js'):
            self.assertEqual(client.get('/static/' + name).status_code, 404, name)
        self.assertEqual(self.calls, [])

    def test_security_headers_and_host_check(self):
        client = self.client()
        for path in ('/login', '/static/app.js', '/api/me', '/'):
            headers = client.get(path).headers
            self.assertIn("script-src 'self'", headers['content-security-policy'], path)
            self.assertNotIn('unsafe-inline', headers['content-security-policy'])
            self.assertEqual((headers['x-content-type-options'], headers['x-frame-options'], headers['referrer-policy']),
                             ('nosniff', 'DENY', 'same-origin'), path)
            self.assertNotIn('strict-transport-security', headers)
        self.assertEqual(client.get('/login').headers['cache-control'], 'no-store')
        self.assertIn('max-age', client.get('/login', headers={'x-forwarded-proto': 'https'}).headers['strict-transport-security'])
        self.assertEqual(client.get('/login', headers={'host': 'evil.example'}).status_code, 400)

    def test_login_failures_are_uniform_and_lock(self):
        client = self.client()
        page = client.get('/login?error=1').text
        self.assertIn('이름 또는 비밀번호가 맞지 않습니다', page)
        self.assertNotIn('<script', page)
        self.assertNotIn('error', client.get('/login?error=<script>alert(1)</script>').text.split('<form')[1].split('<label')[0])
        post = lambda name, password, headers=ORIGIN: client.post('/login', data={'username': name, 'password': password}, headers=headers)
        self.assertEqual(post('kim', PASSWORD, {}).headers['location'], '/login?error=origin')            # Origin 없는 요청
        self.assertEqual(post('kim', PASSWORD, {'origin': 'http://evil.example'}).headers['location'], '/login?error=origin')
        self.assertEqual(post('kim', 'wrong').headers['location'], '/login?error=1')
        self.assertEqual(post('nobody', 'wrong').headers['location'], '/login?error=1')                   # 없는 계정도 같은 답
        self.assertNotIn('set-cookie', post('kim', 'wrong').headers)
        self.assertEqual(post('kim', 'wrong').headers['location'], '/login?error=1')                      # 3번째 실패 → 잠김
        self.assertEqual(post('kim', PASSWORD).headers['location'], '/login?error=locked')                # 맞는 비밀번호도 잠금 중에는 거절
        self.assertEqual(client.get('/api/me').status_code, 401)
        other = self.client('lee', 'plain-password')                          # 한 계정이 잠겨도 다른 계정은 로그인된다
        self.assertEqual(other.get('/api/me').json()['name'], 'lee')
        for _ in range(12):                                                   # 같은 IP 에서 여러 이름으로 계속 틀리면 IP 도 잠긴다
            post(f'guess{_}', 'wrong')
        self.assertEqual(client.post('/login', data={'username': 'lee', 'password': 'plain-password'}, headers=ORIGIN).headers['location'],
                         '/login?error=locked')

    def test_session_cookie_flags_and_logout(self):
        client = self.client()
        response = client.post('/login', data={'username': 'lee', 'password': 'plain-password'}, headers=ORIGIN)
        cookie = response.headers['set-cookie'].lower()
        self.assertTrue(all(flag in cookie for flag in ('httponly', 'samesite=strict', 'path=/')), cookie)
        self.assertNotIn('secure', cookie)                                    # http 로 온 요청
        self.assertNotIn('lee', cookie)                                       # 쿠키에는 무작위 토큰만
        secure = self.client().post('/login', data={'username': 'kim', 'password': PASSWORD}, headers={**ORIGIN, 'x-forwarded-proto': 'https'})
        self.assertIn('secure', secure.headers['set-cookie'].lower())
        self.assertEqual(client.get('/api/me').json()['name'], 'lee')
        self.assertEqual(client.get('/').status_code, 200)
        self.assertEqual(client.get('/login').headers['location'], '/')
        self.assertEqual(client.post('/logout').headers['location'], '/')     # Origin 없는 로그아웃은 무시
        self.assertEqual(client.get('/api/me').status_code, 200)
        self.assertEqual(client.post('/logout', headers=ORIGIN).headers['location'], '/login')
        self.assertEqual(client.get('/api/me').status_code, 401)

    def test_writes_need_same_origin_and_the_api_header(self):
        client = self.client('kim')
        for headers in ({}, ORIGIN, {'x-requested-with': 'rnd-chat'}, {'origin': 'http://evil.example', 'x-requested-with': 'rnd-chat'}):
            self.assertEqual(self.ask(client, '질문', headers=headers)[0].status_code, 403, headers)
            self.assertEqual(client.delete('/api/conversations/x', headers=headers).status_code, 403, headers)
        self.assertEqual(self.calls, [])

    def test_chat_streams_progress_and_keeps_conversations_per_user(self):
        kim = self.client('kim')
        response, events = self.ask(kim, '인공지능 과제가 몇 건이야?')
        self.assertEqual(response.headers['content-type'].split(';')[0], 'text/event-stream')
        self.assertEqual([event['type'] for event in events], ['tool_call', 'tool_result', 'answer', 'done'])
        self.assertEqual(events[0]['arguments'], {'group_by': 'year'})
        conversation_id = events[-1]['conversation_id']
        self.assertEqual(self.calls[0], [{'role': 'user', 'content': '인공지능 과제가 몇 건이야?'}])
        _, events = self.ask(kim, '부처별로는?', conversation_id)
        self.assertEqual(events[-1]['conversation_id'], conversation_id)
        self.assertEqual([m['role'] for m in self.calls[1]], ['user', 'assistant', 'user'])              # 앞선 문답이 함께 간다
        listing = kim.get('/api/conversations').json()['conversations']
        self.assertEqual([(item['id'], item['title']) for item in listing], [(conversation_id, '인공지능 과제가 몇 건이야?')])
        stored = kim.get('/api/conversations/' + conversation_id)
        self.assertEqual(len(stored.json()['messages']), 4)
        self.assertEqual(stored.headers['content-type'], 'application/json')                              # 답 속의 HTML 은 글자로만 오간다
        lee = self.client('lee', 'plain-password')
        self.assertEqual(lee.get('/api/conversations').json()['conversations'], [])
        self.assertEqual(lee.get('/api/conversations/' + conversation_id).status_code, 404)              # 남의 대화는 id 를 알아도 못 본다
        self.assertEqual(self.ask(lee, '이어서', conversation_id)[0].status_code, 404)
        self.assertFalse(lee.delete('/api/conversations/' + conversation_id, headers=API).json()['deleted'])
        self.assertTrue(kim.delete('/api/conversations/' + conversation_id, headers=API).json()['deleted'])
        self.assertEqual(kim.get('/api/conversations').json()['conversations'], [])

    def test_bad_requests_and_model_errors(self):
        client = self.client('kim')
        self.assertEqual(self.ask(client, '   ')[0].status_code, 400)
        self.assertEqual(self.ask(client, '가' * 201)[0].status_code, 400)
        self.assertEqual(self.ask(client, '질문', 'no-such-conversation')[0].status_code, 404)
        self.assertEqual(client.post('/api/chat', content=b'[1, 2', headers={**API, 'content-type': 'application/json'}).status_code, 400)
        self.assertEqual(self.calls, [])
        self.error = LlmError('모델 서버에 연결하지 못했습니다: ConnectError')
        _, events = self.ask(client, '질문')
        self.assertEqual(events, [{'type': 'error', 'message': '모델 서버에 연결하지 못했습니다: ConnectError'}])
        self.error = RuntimeError('secret internal detail')
        _, events = self.ask(client, '질문')
        self.assertEqual(events, [{'type': 'error', 'message': '답을 만드는 중 오류가 났습니다.'}])              # 내부 오류 내용은 내보내지 않는다
        self.assertEqual(client.get('/api/conversations').json()['conversations'], [])
        self.error = None
        self.assertEqual(self.ask(client, '다시')[1][-1]['type'], 'done')                                 # 실패 뒤에도 다음 질문을 받는다


if __name__ == '__main__':
    unittest.main()
