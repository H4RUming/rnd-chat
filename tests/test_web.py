"""로그인(계정·잠금·세션)과 웹 서버(접근 통제, CSRF 방지, 보안 헤더, 채팅 흐름, 대화 분리)."""
import asyncio
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from docx import Document
from openpyxl import load_workbook

from starlette.testclient import TestClient

from chat import agent, auth, docs, files, storage, tlscert, web
from chat.llm import LlmError
from chat.settings import Agent, Llm, Settings, Web

ORIGIN = {'origin': 'http://localhost'}
API = {**ORIGIN, 'x-requested-with': 'rnd-chat'}
PASSWORD = 'correct horse battery'
HASH = auth.hash_password(PASSWORD)


def tiny_pdf(text='Application Form'):
    """글 한 줄이 든 한 쪽짜리 PDF(오프셋까지 맞춘 최소한의 파일)."""
    stream = f'BT /F1 18 Tf 72 720 Td ({text}) Tj ET'.encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
               b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offsets = b'%PDF-1.4\n', []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 6\n0000000000 65535 f \n' + b''.join(b'%010d 00000 n \n' % offset for offset in offsets)
    return out + b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % xref


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

    def test_sessions_kept_in_the_database_survive_a_restart_but_not_a_password_change(self):
        folder = tempfile.mkdtemp(prefix='rndchat-test-')
        self.addCleanup(shutil.rmtree, folder, True)
        database, clock = storage.Database(Path(folder) / 'chat.sqlite3'), Clock()
        users = auth.Users([{'name': 'kim', 'password_hash': HASH}, {'name': 'lee', 'password': 'plain-password'}])
        valid = lambda user, stamp: bool(stamp) and stamp == users.stamp(user)
        sessions = auth.Sessions(idle=100, lifetime=1000, per_user=2, clock=clock, store=database, valid=valid)
        kim, lee, gone = sessions.create('kim', users.stamp('kim')), sessions.create('lee', users.stamp('lee')), sessions.create('kim', users.stamp('kim'))
        sessions.delete(gone)
        kept = database.sessions()
        self.assertEqual(sorted(row[1] for row in kept), ['kim', 'lee'])
        self.assertFalse(any(token in str(kept) for token in (kim, lee)) or 'plain-password' in str(kept) or HASH in str(kept))   # 토큰도 비밀번호도 적지 않는다
        self.assertEqual((users.stamp('nobody'), users.stamp('kim') != users.stamp('lee'), len(users.stamp('lee'))), ('', True, 32))
        clock.now += 50
        again = auth.Sessions(idle=100, lifetime=1000, per_user=2, clock=clock, store=database, valid=valid)        # 서버를 다시 띄움
        self.assertEqual((again.get(kim).user, again.get(lee).user, again.get(gone)), ('kim', 'lee', None))
        clock.now += auth.Sessions.TOUCH + 1 - 50                                # 쓰는 동안에는 마지막으로 쓴 때를 가끔 고쳐 적는다
        again = auth.Sessions(idle=400, lifetime=1000, per_user=2, clock=clock, store=database, valid=valid)
        self.assertIsNotNone(again.get(kim))
        self.assertEqual(max(row[3] for row in database.sessions()), clock.now)
        changed = auth.Users([{'name': 'kim', 'password_hash': auth.hash_password('new-password')}])                # kim 은 비밀번호를 바꾸고, lee 는 없어짐
        later = auth.Sessions(idle=400, lifetime=1000, clock=clock, store=database, valid=lambda user, stamp: bool(stamp) and stamp == changed.stamp(user))
        self.assertEqual((later.get(kim), later.get(lee), database.sessions()), (None, None, []))
        fresh = later.create('kim', changed.stamp('kim'))
        clock.now += 401
        self.assertEqual(auth.Sessions(idle=400, lifetime=1000, clock=clock, store=database).get(fresh), None)      # 오래 쉰 세션은 다시 띄울 때 버린다
        self.assertEqual(database.sessions(), [])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.error = None
        self.make = None
        self.found = None
        self.answer = None
        self.data = tempfile.mkdtemp(prefix='rndchat-test-')
        self.addCleanup(shutil.rmtree, self.data, True)

        async def runner(messages, listener, local=None):
            self.calls.append(messages)
            self.local = local
            if self.error:
                raise self.error
            if self.make and local:                                           # 모델이 파일 만들기·고치기 도구를 부른 것처럼
                result = await local.call(*self.make)
                self.result = result
                for created in result.files:
                    await listener({'type': 'file', 'file': created})
            await listener({'type': 'tool_call', 'name': 'count_projects', 'arguments': {'group_by': 'year'}})
            await listener({'type': 'tool_result', 'name': 'count_projects', 'is_error': False, 'chars': 12, 'images': 0})
            if self.found and local:                                          # 자료 서버가 공고·과제를 돌려준 것처럼
                self.seen, summary = local.observe('search_projects', {}, json.dumps(self.found, ensure_ascii=False), False)
                await listener({'type': 'sources', 'sources': local.sources.take_fresh()})
                await listener({'type': 'tool_call', 'name': 'search_projects', 'arguments': {'query_text': '양자', 'edits': [1, 2]}})
                await listener({'type': 'tool_result', 'name': 'search_projects', 'is_error': False, 'chars': 9, 'images': 0, 'summary': summary})
            answer = self.answer or f'답 {len(self.calls)}: <img src=x onerror=alert(1)>'
            await listener({'type': 'answer', 'text': answer, 'stopped': ''})
            return agent.Outcome(answer, messages, 2)
        self.runner = runner
        self.settings = Settings(llm=Llm('http://llm.test/v1', 'hai'), agent=Agent(), servers=(), web=Web(max_question_chars=200), data_dir=self.data)
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
        for name in ('base.css', 'app.css', 'login.css', 'login.js'):
            self.assertEqual(client.get('/static/' + name).status_code, 200, name)
        font = client.get('/static/d2coding-400.woff2')                     # 글꼴은 이 서버에서 싣고(바깥 CDN 없음) 오래 둔다
        self.assertEqual((font.headers['content-type'], font.headers['cache-control'], font.content[:4]), ('font/woff2', 'public, max-age=2592000', b'wOF2'))
        script = client.get('/static/app.js')
        again = client.get('/static/app.js', headers={'if-none-match': script.headers['etag']})
        self.assertEqual((again.status_code, again.content), (304, b''))      # 바뀌지 않았으면 본문을 다시 보내지 않는다
        login = client.get('/login?error=1').text
        self.assertIn('아이디 또는 비밀번호가 맞지 않습니다.', login)
        self.assertNotIn('<!--error-->', login)
        self.assertEqual(client.get('/login?error=%3Cscript%3E').text.count('class="alert"'), 0)   # 모르는 오류 코드는 아무것도 내지 않는다
        self.assertEqual(client.get('/static/app.js').headers['content-type'], 'text/javascript; charset=utf-8')
        self.assertEqual(client.get('/static/theme.js').status_code, 200)
        self.assertNotIn('<script>', client.get('/login').text)               # 인라인 스크립트 없이 파일로만 싣는다
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
        self.assertEqual(client.get('/login').headers['cache-control'], 'no-store, no-transform')
        self.assertIn('max-age', client.get('/login', headers={'x-forwarded-proto': 'https'}).headers['strict-transport-security'])
        self.assertEqual(client.get('/login', headers={'host': 'evil.example'}).status_code, 400)

    def test_direct_https_sets_secure_cookie_but_no_hsts(self):
        client = TestClient(self.app, base_url='https://localhost', follow_redirects=False)
        headers = client.get('/login').headers
        self.assertEqual(headers['cross-origin-opener-policy'], 'same-origin')
        self.assertNotIn('strict-transport-security', headers)                # 내부망 이름에는 HSTS 를 붙이지 않는다
        response = client.post('/login', data={'username': 'kim', 'password': PASSWORD}, headers={'origin': 'https://localhost'})
        self.assertEqual(response.headers['location'], '/')
        self.assertIn('secure', response.headers['set-cookie'].lower())
        self.assertEqual(client.get('/api/me').json()['name'], 'kim')
        mixed = client.post('/logout', headers={'origin': 'http://localhost'})  # 스킴이 달라도 호스트가 같으면 같은 출처로 본다
        self.assertEqual(mixed.headers['location'], '/login')

    def test_tls_settings_and_certificate_names(self):
        base = '[llm]\nbase_url = "http://x/v1"\nmodel = "m"\n[web]\n'
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            (Path(folder) / 'certs').mkdir()
            (Path(folder) / 'certs' / 'cert.pem').write_text('cert')
            (Path(folder) / 'certs' / 'key.pem').write_text('key')
            config.write_text(base + 'tls_cert = "certs/cert.pem"\ntls_key = "certs/key.pem"\n', encoding='utf-8')
            loaded = Settings.load(config).web
            self.assertEqual((loaded.tls_cert, loaded.tls_key), (str(Path(folder) / 'certs' / 'cert.pem'), str(Path(folder) / 'certs' / 'key.pem')))
            for text, expected in (('tls_cert = "certs/cert.pem"\n', '둘 다'), ('tls_cert = "certs/none.pem"\ntls_key = "certs/key.pem"\n', '파일이 없습니다')):
                config.write_text(base + text, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, expected):
                    Settings.load(config)
            config.write_text(base, encoding='utf-8')
            self.assertEqual(Settings.load(config).web.tls_cert, '')
        self.assertEqual(tlscert.subject_alt_names(['orin', 'orin'], ['10.0.0.5']), 'DNS:localhost,DNS:orin,IP:127.0.0.1,IP:10.0.0.5')
        for names, ips in ((['bad name'], []), ([], ['not-an-ip']), (['a,DNS:evil'], [])):
            with self.assertRaises(ValueError):
                tlscert.subject_alt_names(names, ips)

    def test_login_failures_are_uniform_and_lock(self):
        client = self.client()
        page = client.get('/login?error=1').text
        self.assertIn('아이디 또는 비밀번호가 맞지 않습니다', page)
        self.assertNotIn('<script>', page)
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
        self.assertEqual((client.get('/api/me').json()['name'], client.get('/api/me').json()['model']), ('lee', 'hai'))
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
        self.assertEqual(events[-1]['index'], 1)
        self.assertEqual(events[0]['arguments'], {'group_by': 'year'})
        conversation_id = events[-1]['conversation_id']
        self.assertEqual(self.calls[0], [{'role': 'user', 'content': '인공지능 과제가 몇 건이야?'}])
        _, events = self.ask(kim, '부처별로는?', conversation_id)
        self.assertEqual(events[-1]['conversation_id'], conversation_id)
        self.assertEqual([m['role'] for m in self.calls[1]], ['user', 'assistant', 'user'])              # 앞선 문답이 함께 간다
        listing = kim.get('/api/conversations').json()['conversations']
        self.assertEqual([(item['id'], item['title']) for item in listing], [(conversation_id, '인공지능 과제가 몇 건이야?')])
        self.assertGreater(listing[0]['updated'], 0)
        stored = kim.get('/api/conversations/' + conversation_id)
        self.assertEqual(len(stored.json()['messages']), 4)
        self.assertEqual(stored.headers['content-type'], 'application/json')                              # 답 속의 HTML 은 글자로만 오간다
        # 다시 생성: 마지막 질문을 다시 묻고 답만 바꾼다
        response = kim.post('/api/chat', json={'conversation_id': conversation_id, 'regenerate': True}, headers=API)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[2], self.calls[1])                        # 같은 대화 내용으로 다시 물음
        messages = kim.get('/api/conversations/' + conversation_id).json()['messages']
        self.assertEqual((len(messages), messages[-2]['content'], messages[-1]['content'][:4]), (4, '부처별로는?', '답 3:'))
        self.assertEqual(kim.post('/api/chat', json={'regenerate': True}, headers=API).status_code, 400)
        # 제목 바꾸기
        renamed = kim.patch('/api/conversations/' + conversation_id, json={'title': '  AI   과제 현황 '}, headers=API)
        self.assertEqual(renamed.json(), {'id': conversation_id, 'title': 'AI 과제 현황'})
        self.assertEqual(kim.get('/api/conversations').json()['conversations'][0]['title'], 'AI 과제 현황')
        self.assertEqual(kim.patch('/api/conversations/' + conversation_id, json={'title': ' '}, headers=API).status_code, 400)
        self.assertEqual(kim.patch('/api/conversations/' + conversation_id, json={'title': 'x'}).status_code, 403)
        lee = self.client('lee', 'plain-password')
        self.assertEqual(lee.patch('/api/conversations/' + conversation_id, json={'title': '남의 것'}, headers=API).status_code, 404)
        self.assertEqual(lee.get('/api/conversations').json()['conversations'], [])
        self.assertEqual(lee.get('/api/conversations/' + conversation_id).status_code, 404)              # 남의 대화는 id 를 알아도 못 본다
        self.assertEqual(self.ask(lee, '이어서', conversation_id)[0].status_code, 404)
        self.assertFalse(lee.delete('/api/conversations/' + conversation_id, headers=API).json()['deleted'])
        self.assertTrue(kim.delete('/api/conversations/' + conversation_id, headers=API).json()['deleted'])
        self.assertEqual(kim.get('/api/conversations').json()['conversations'], [])

    def test_answers_export_to_documents_and_spreadsheets(self):
        kim = self.client('kim')
        _, events = self.ask(kim, '표로 정리해줘')
        conversation_id = events[-1]['conversation_id']
        self.assertEqual(kim.get(f'/api/conversations/{conversation_id}/export?index=1&format=xlsx').status_code, 400)   # 표가 없는 답
        response = kim.get(f'/api/conversations/{conversation_id}/export?index=1&format=docx')
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename*=UTF-8''", response.headers['content-disposition'])
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertIn('답 1', '\n'.join(p.text for p in Document(io.BytesIO(response.content)).paragraphs))
        for query in ('index=0&format=docx', 'index=9&format=docx', 'index=1&format=exe', 'index=x&format=docx'):
            self.assertEqual(kim.get(f'/api/conversations/{conversation_id}/export?{query}').status_code, 404, query)
        self.assertEqual(self.client('lee', 'plain-password').get(f'/api/conversations/{conversation_id}/export?index=1&format=docx').status_code, 404)
        self.assertEqual(self.client().get(f'/api/conversations/{conversation_id}/export?index=1&format=docx').status_code, 401)
        table = '## 부처별\n\n| 부처 | 건수 |\n|---|---|\n| 과기정통부 | 5,692 |\n| =cmd | 0012 |\n'
        sheets = docs.markdown_tables(table)
        book = load_workbook(io.BytesIO(docs.build_xlsx(sheets)))
        sheet = book['부처별']
        self.assertEqual([[cell.value for cell in row] for row in sheet.iter_rows()], [['부처', '건수'], ['과기정통부', 5692], ['=cmd', '0012']])
        self.assertEqual(sheet['A3'].data_type, 's')                          # '=' 로 시작해도 수식이 되지 않는다

    def test_model_can_hand_over_generated_files(self):
        kim = self.client('kim')
        self.make = ('create_document', {'filename': '../신청서', 'title': '참가 신청서', 'blocks': [
            {'type': 'table', 'rows': [[{'text': '신청 개요', 'colspan': 2, 'shade': True}], [{'text': '기업명', 'shade': True}, '(주)예시']]}]})
        _, events = self.ask(kim, '양식대로 만들어줘')
        created = [event['file'] for event in events if event['type'] == 'file']
        self.assertEqual([(item['name'], item['kind'], item['origin']) for item in created], [('신청서.docx', 'docx', 'generated')])
        stored = kim.get('/api/conversations/' + events[-1]['conversation_id']).json()['messages'][1]
        self.assertEqual(stored['files'][0]['id'], created[0]['id'])
        response = kim.get('/api/files/' + created[0]['id'])
        self.assertEqual((response.status_code, response.headers['content-type']), (200, files.MIME['docx']))
        self.assertIn('attachment', response.headers['content-disposition'])
        table = Document(io.BytesIO(response.content)).tables[0]
        self.assertEqual((table.cell(0, 0).text, table.cell(1, 1).text, table.cell(0, 0)._tc is table.cell(0, 1)._tc), ('신청 개요', '(주)예시', True))
        self.assertEqual(self.client('lee', 'plain-password').get('/api/files/' + created[0]['id']).status_code, 404)   # 남의 파일
        self.assertEqual(self.client().get('/api/files/' + created[0]['id']).status_code, 401)
        for bad in ('..', 'a' * 8 + '%2F..', 'short'):
            self.assertEqual(kim.get('/api/files/' + bad).status_code, 404, bad)
        self.make = ('create_spreadsheet', {'filename': '현황', 'sheets': [{'name': '과제', 'columns': ['연도', '건수'], 'rows': [[2025, 1118]]}]})
        _, events = self.ask(kim, '엑셀로 줘')
        self.assertEqual([event['file']['name'] for event in events if event['type'] == 'file'], ['현황.xlsx'])
        self.make = ('create_document', {'filename': 'x', 'blocks': [{'type': 'unknown'}]})
        result = asyncio.run(self.local.call(*self.make))
        self.assertTrue(result.is_error and '모르는 블록' in result.text and not result.files)
        self.assertIsNone(asyncio.run(self.local.call('search_projects', {})))        # 우리 도구가 아니면 MCP 쪽으로 넘긴다
        wrapped = asyncio.run(self.local.call('create_spreadsheet', {'filename': 'x', 'sheets': json.dumps([{'columns': ['a'], 'rows': [[1]]}])}))
        self.assertFalse(wrapped.is_error)                                    # 목록을 JSON 글로 감싸 보내도 받는다
        broken = asyncio.run(self.local.call('create_spreadsheet', {'filename': 'x', 'sheets': '[{"columns": '}))
        self.assertTrue(broken.is_error and '목록(배열) 그대로' in broken.text)

    @unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdfinfo'), 'poppler-utils is not installed')
    def test_pdf_form_upload_reaches_the_model_as_page_images(self):
        kim = self.client('kim')
        upload = lambda data, headers=API, name='신청 양식.pdf': kim.post('/api/files', files={'file': (name, data, 'application/pdf')}, headers=headers)
        self.assertEqual(upload(b'not a pdf at all').json()['error'], 'PDF, HWP, HWPX, DOCX 파일만 첨부할 수 있습니다.')
        self.assertEqual(upload(b'%PDF-1.4 truncated').status_code, 400)
        self.assertEqual(upload(tiny_pdf(), headers=ORIGIN).status_code, 403)                # API 헤더 없이는 올릴 수 없다
        self.assertEqual(self.client().post('/api/files', files={'file': ('a.pdf', tiny_pdf())}, headers=API).status_code, 401)
        response = upload(tiny_pdf('Company name:'), name='../../신청 양식.pdf')
        meta = response.json()
        self.assertEqual((response.status_code, meta['name'], meta['kind'], meta['pages'], meta['shown'], meta['origin']), (200, '신청 양식.pdf', 'pdf', 1, 1, 'upload'))
        self.assertEqual(sorted(path.name for path in (Path(self.data) / 'files').rglob('*') if path.is_file()), sorted([meta['id'], meta['id'] + '.json']))   # 디스크에는 무작위 이름으로만
        response = kim.post('/api/chat', json={'message': '이 양식대로 문서를 만들어줘', 'attachments': [meta['id']]}, headers=API)
        events = [json.loads(line[6:]) for line in response.text.split('\n') if line.startswith('data: ')]
        sent = self.calls[-1][-1]['content']
        self.assertEqual([part['type'] for part in sent], ['text', 'image_url'])
        self.assertIn(f'[첨부한 문서: 신청 양식.pdf · PDF · file_id: {meta["id"]} · 전체 1쪽, 앞 1쪽을 그림으로 첨부]', sent[0]['text'])
        self.assertIn('create_document', sent[0]['text'])                                # PDF 는 고칠 수 없으니 새로 만들라고 알려 준다
        self.assertFalse(meta['editable'])
        self.assertIn('Company name:', sent[0]['text'])
        self.assertTrue(sent[1]['image_url']['url'].startswith('data:image/jpeg;base64,/9j/'))
        conversation_id = events[-1]['conversation_id']
        self.assertEqual(kim.get('/api/conversations/' + conversation_id).json()['messages'][0]['files'][0]['id'], meta['id'])
        self.ask(kim, '3번 칸을 고쳐줘', conversation_id)
        earlier = self.calls[-1][0]['content']                                          # 지난 질문의 첨부는 글로만 다시 간다
        self.assertTrue(isinstance(earlier, str) and 'Company name:' in earlier and '그림으로 첨부' not in earlier)
        kim.post('/api/chat', json={'conversation_id': conversation_id, 'regenerate': True}, headers=API)
        self.assertEqual(self.calls[-1][-1]['content'], '3번 칸을 고쳐줘')
        lee = self.client('lee', 'plain-password')
        self.assertEqual(lee.post('/api/chat', json={'message': '남의 첨부', 'attachments': [meta['id']]}, headers=API).status_code, 400)
        self.assertEqual(kim.post('/api/chat', json={'message': 'x', 'attachments': ['a', 'b', 'c', 'd']}, headers=API).status_code, 400)
        big = kim.post('/api/files', content=b'x', headers={**API, 'content-type': 'multipart/form-data; boundary=x', 'content-length': str(files.MAX_UPLOAD * 2)})
        self.assertEqual(big.status_code, 413)

    def test_file_kind_is_decided_by_content_not_by_name(self):
        import zipfile

        def archive(*names):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as packed:
                for name in names:
                    packed.writestr(name, '<x/>')
            return buffer.getvalue()
        ole = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
        self.assertEqual(files.sniff(tiny_pdf()), 'pdf')
        self.assertEqual(files.sniff(ole + b'\x00' * 500 + b'HWP Document File' + b'\x00' * 100), 'hwp')
        self.assertIsNone(files.sniff(ole + b'\x00' * 600))                              # 옛 워드·엑셀 같은 다른 OLE 문서
        self.assertEqual(files.sniff(archive('mimetype', 'Contents/content.hpf', 'Contents/section0.xml')), 'hwpx')
        self.assertEqual(files.sniff(archive('[Content_Types].xml', 'word/document.xml')), 'docx')
        self.assertIsNone(files.sniff(archive('xl/workbook.xml')))                        # 엑셀은 받지 않는다
        self.assertIsNone(files.sniff(b'PK\x03\x04 not really a zip'))
        self.assertIsNone(files.sniff(b'#!/bin/sh\nrm -rf /'))
        kim = self.client('kim')
        response = kim.post('/api/files', files={'file': ('양식.pdf', b'#!/bin/sh', 'application/pdf')}, headers=API)
        self.assertEqual((response.status_code, response.json()['error']), (400, 'PDF, HWP, HWPX, DOCX 파일만 첨부할 수 있습니다.'))
        with self.assertRaises(files.FileError):
            files.office_to_pdf(b'x', 'pptx')

    def word_form(self) -> bytes:
        document = Document()
        document.add_heading('참가 신청서', level=1)
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text, table.cell(1, 0).text = '기업명', 'ㅇㅇ기업', '대표자'
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    @unittest.skipUnless(shutil.which('soffice') and shutil.which('pdftoppm'), 'LibreOffice is not installed')
    def test_attached_word_form_is_edited_in_place(self):
        kim = self.client('kim')
        original = self.word_form()
        response = kim.post('/api/files', files={'file': ('신청서 양식.docx', original, 'application/octet-stream')}, headers=API)
        meta = response.json()
        self.assertEqual((response.status_code, meta.get('name'), meta.get('kind'), meta.get('pages'), meta.get('editable'), meta.get('tables')),
                         (200, '신청서 양식.docx', 'docx', 1, True, 1), meta)
        self.assertEqual(kim.get('/api/files/' + meta['id']).content, original)          # 보관되는 것은 올린 원본 그대로다
        # 모델에는 쪽 그림과, 고칠 곳을 가리킬 좌표가 적힌 개요가 간다
        self.make = ('edit_document', {'file_id': meta['id'], 'filename': '../신청서', 'edits': [
            {'op': 'set_cell', 'table': 0, 'row': 0, 'col': 1, 'text': '(주)한빛'}, {'op': 'set_cell', 'table': 0, 'row': 1, 'col': 1, 'text': '홍길동'},
            {'op': 'set_cell', 'table': 3, 'row': 0, 'col': 0, 'text': 'x'}]})
        response = kim.post('/api/chat', json={'message': '우리 회사 정보로 채워줘', 'attachments': [meta['id']]}, headers=API)
        events = [json.loads(line[6:]) for line in response.text.split('\n') if line.startswith('data: ')]
        sent = self.calls[-1][-1]['content']
        self.assertEqual([part['type'] for part in sent], ['text', 'image_url'])
        self.assertIn(f'file_id: {meta["id"]}', sent[0]['text'])
        self.assertIn('r0: c0 "기업명" | c1 "ㅇㅇ기업"', sent[0]['text'])
        edited = [event['file'] for event in events if event['type'] == 'file'][0]
        self.assertEqual((edited['name'], edited['kind'], edited['origin'], edited['applied'], edited['failed'], edited['root'], edited['pages']),
                         ('신청서.docx', 'docx', 'edited', 2, 1, meta['id'], 1))
        self.assertEqual([(change['ok'], change.get('old'), change.get('new')) for change in edited['changes']][:2], [(True, 'ㅇㅇ기업', '(주)한빛'), (True, '', '홍길동')])
        note = json.loads(self.result.text)
        self.assertEqual((note['file_id'], note['applied'], len(note['failed']), note['pages_shown'], len(self.result.images)), (edited['id'], 2, 1, [1], 1))
        table = Document(io.BytesIO(kim.get('/api/files/' + edited['id']).content)).tables[0]
        self.assertEqual((table.cell(0, 0).text, table.cell(0, 1).text, table.cell(1, 1).text), ('기업명', '(주)한빛', '홍길동'))
        self.assertEqual(kim.get('/api/files/' + meta['id']).content, original)          # 원본은 바뀌지 않는다
        # 고친 것을 다시 고치면 앞의 것을 잇는다(원본은 같고, 화면에는 마지막 것만 남는다)
        self.make = ('edit_document', {'file_id': edited['id'], 'edits': [{'op': 'replace_text', 'find': '홍길동', 'replace': '김가온'}]})
        _, events = self.ask(kim, '대표자만 바꿔줘', events[-1]['conversation_id'])
        again = [event['file'] for event in events if event['type'] == 'file'][0]
        self.assertEqual((again['name'], again['root'], again['previous']), ('신청서.docx', meta['id'], edited['id']))
        self.assertIn(f'[이 답에서 만든 파일: 신청서.docx (file_id: {edited["id"]})]', self.calls[-1][1]['content'])    # 앞선 답의 파일을 모델이 다시 가리킬 수 있다
        # 미리보기: 파일 정보와 쪽 그림
        info = kim.get(f'/api/files/{again["id"]}/info').json()
        self.assertEqual((info['pages'], info['origin'], len(info['changes'])), (1, 'edited', 1))
        page = kim.get(f'/api/files/{again["id"]}/pages/1?size=thumb')
        self.assertEqual((page.status_code, page.headers['content-type'], page.content[:3]), (200, 'image/jpeg', b'\xff\xd8\xff'))
        for bad in ('pages/0', 'pages/99', 'pages/x', 'pages/1?size=huge'):
            self.assertEqual(kim.get(f'/api/files/{again["id"]}/{bad}').status_code, 404, bad)
        lee = self.client('lee', 'plain-password')
        for path in ('', '/info', '/pages/1'):
            self.assertEqual(lee.get(f'/api/files/{again["id"]}{path}').status_code, 404, path)   # 남의 파일은 볼 수도 없다
        # 문서가 아닌 것, 고칠 수 없는 것
        for arguments, expected in (({'file_id': 'nope-nope-nope', 'edits': [{}]}, 'file_id'), ({'file_id': meta['id'], 'edits': []}, 'edits'),
                                    ({'file_id': meta['id'], 'edits': [{'op': 'replace_text', 'find': '없는 글', 'replace': 'x'}]}, '하나도 없습니다')):
            result = asyncio.run(self.local.call('edit_document', arguments))
            self.assertTrue(result.is_error and expected in result.text and not result.files, result.text)
        inspected = asyncio.run(self.local.call('inspect_document', {'file_id': again['id'], 'table': 0, 'pages': [1, 7]}))
        self.assertTrue('c1 "김가온"' in inspected.text and len(inspected.images) == 1, inspected.text)

    def test_conversations_and_files_survive_a_restart(self):
        kim = self.client('kim')
        self.found = {'total': 1, 'items': [{'title': '가 과제', 'url': 'https://www.ntis.go.kr/p?id=1'}]}
        self.answer = '가 과제가 있습니다[1].'
        _, events = self.ask(kim, '양자 과제')
        first = events[-1]['conversation_id']
        self.ask(kim, '더 찾아줘', first)
        self.answer = None
        _, events = self.ask(kim, '지울 대화')
        second = events[-1]['conversation_id']
        kim.patch('/api/conversations/' + first, json={'title': '양자 과제 정리'}, headers=API)
        self.assertEqual(kim.delete('/api/conversations/' + second, headers=API).status_code, 200)
        uploaded = kim.post('/api/files', files={'file': ('양식.pdf', tiny_pdf(), 'application/pdf')}, headers=API).json()
        self.ask(self.client('lee', 'plain-password'), '이 씨의 대화')
        before = kim.get('/api/conversations/' + first).json()
        database = Path(self.data) / 'chat.sqlite3'
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)                     # 대화 파일은 서버 계정만 읽는다

        # 서버를 다시 띄운 것처럼 앱을 새로 만든다. 로그인해 둔 브라우저는 그대로 쓸 수 있다(세션도 파일에 남는다)
        cookie = kim.cookies.get(web.COOKIE)
        self.app = web.create_app(self.settings, self.users, runner=lambda *args: None, throttle=self.throttle)
        kim = self.client()
        kim.cookies.set(web.COOKIE, cookie)
        self.assertEqual(kim.get('/api/me').json()['name'], 'kim')
        self.assertEqual(self.client().get('/api/me').status_code, 401)
        self.assertEqual([(item['id'], item['title']) for item in kim.get('/api/conversations').json()['conversations']], [(first, '양자 과제 정리')])
        after = kim.get('/api/conversations/' + first).json()
        self.assertEqual(after, before)                                              # 메시지·출처·작업 기록까지 그대로
        self.assertEqual((len(after['messages']), after['sources'][0]['title'], after['messages'][1]['steps'][0]['name']), (4, '가 과제', 'count_projects'))
        self.assertEqual(kim.get('/api/conversations/' + second).status_code, 404)   # 지운 대화는 돌아오지 않는다
        self.assertEqual(kim.get(f'/api/files/{uploaded["id"]}').status_code, 200)   # 올린 파일도 남아 있다
        lee = self.client('lee', 'plain-password')
        self.assertEqual(len(lee.get('/api/conversations').json()['conversations']), 1)
        self.assertEqual(lee.get('/api/conversations/' + first).status_code, 404)    # 남의 대화는 여전히 볼 수 없다

    def desk_app(self, **options):
        """비서를 켠 설정으로 앱을 다시 만든다(대화와 맡긴 일은 파일에서 이어받는다). 메일 보내기는 가짜로 바꾼다."""
        import dataclasses
        from chat.settings import Briefing, Mail
        settings = dataclasses.replace(self.settings, briefing=Briefing(enabled=True, app_url='https://rnd.test', **options),
                                       mail=Mail(username='bot@gmail.com', password='app-password', sender_name='R&D 검색'),
                                       users=({'name': 'kim', 'email': 'kim@example.org'}, {'name': 'lee'}))
        if not hasattr(self, 'sent'):
            self.sent, self.topics, self.asked = [], [], []

            async def interests(llm, questions, previous, limit, muted=()):
                self.asked.append((questions, list(muted)))
                return self.topics
            for module, name, fake in ((web.brief, 'interests', interests),
                                       (web.mailer, 'send', lambda mail, to, subject, page, plain: self.sent.append((to, subject, page, plain)))):
                self.addCleanup(setattr, module, name, getattr(module, name))
                setattr(module, name, fake)
        self.app = web.create_app(settings, self.users, runner=self.runner, throttle=self.throttle)
        return self.app

    def test_tasks_are_given_to_the_assistant_in_words_and_done_on_schedule(self):
        from datetime import datetime
        self.assertEqual(self.client('kim').get('/api/assistant').json()['available'], False)       # 꺼 두면 없다
        app = self.desk_app()
        kim = self.client('kim')
        state = kim.get('/api/assistant').json()
        self.assertEqual((state['available'], state['tasks'], state['mail'], state['to'], state['max_tasks']), (True, [], True, 'ki***@example.org', 10))
        desk = state['conversation_id']
        self.assertEqual((kim.get('/api/conversations/' + desk).json()['kind'], kim.get('/api/assistant').json()['conversation_id']), ('assistant', desk))
        names = lambda: [spec['function']['name'] for spec in self.local.specs()]
        self.ask(kim, '보통 대화의 질문')
        self.assertNotIn('save_task', names())                                         # 보통 대화에서는 일을 맡길 수 없다
        # 비서 대화에서 말로 맡긴다
        self.make = ('save_task', {'title': '강원 드론', 'what': '강원도 드론 지원사업 공고가 새로 뜨면 알린다', 'days': ['thu', 'mon'], 'time': '9:30', 'notify': 'new', 'mail': True})
        _, events = self.ask(kim, '강원도 드론 공고 새로 뜨면 월·목 9시 반에 메일로도 알려줘', desk)
        self.assertEqual((events[-1]['conversation_id'], 'save_task' in names(), '여기는 사용자의 비서 대화' in self.local.system_note, '(아직 없음)' in self.local.system_note),
                         (desk, True, True, True))
        task = kim.get('/api/assistant').json()['tasks'][0]
        self.assertEqual((task['title'], task['when'], task['notify'], task['mail'], task['paused'], task['last'], json.loads(self.result.text)['task_id']),
                         ('강원 드론', '매주 월·목 09:30', 'new', True, False, None, task['id']))
        self.make = ('save_task', {'task_id': task['id'], 'what': '강원도 드론 지원사업 공고(마감된 것은 뺀다)'})      # 결과를 보고 고쳐 달라고 하면 그 일을 고친다
        self.ask(kim, '마감된 건 빼줘', desk)
        self.assertIn(f'task_id {task["id"]}: "강원 드론" · 매주 월·목 09:30 · 새 것이 있을 때만 알림 · 메일로도 보냄', self.local.system_note)
        task = kim.get('/api/assistant').json()['tasks'][0]
        self.assertEqual((len(kim.get('/api/assistant').json()['tasks']), task['what'], task['when'], task['mail']), (1, '강원도 드론 지원사업 공고(마감된 것은 뺀다)', '매주 월·목 09:30', True))
        for arguments, expected in (({'task_id': 'nope', 'what': 'x'}, '그 task_id 의 일이 없습니다'), ({'what': 'x', 'days': ['someday']}, '모르는 요일'),
                                    ({'what': 'x', 'time': '25시'}, '24시간 시각'), ({'title': '내용 없음'}, 'what 에'), ({}, 'what 에')):
            result = asyncio.run(self.local.call('save_task', arguments))
            self.assertTrue(result.is_error and expected in result.text, result.text)
        self.assertTrue(asyncio.run(self.local.call('delete_task', {})).is_error)
        self.make = None
        lee = self.client('lee', 'plain-password')                                    # 메일 주소가 없는 계정은 메일로 받지 못한다
        self.make = ('save_task', {'what': '양자 과제', 'mail': True})
        self.ask(lee, '양자 과제 매일 메일로', lee.get('/api/assistant').json()['conversation_id'])
        mine = lee.get('/api/assistant').json()
        self.assertEqual((mine['mail'], mine['tasks'][0]['mail'], mine['tasks'][0]['when'], '메일 주소가 없어' in self.result.text), (False, False, '매일 10:00', True))
        self.assertEqual(lee.post('/api/assistant', json={'id': mine['tasks'][0]['id'], 'action': 'mail_on'}, headers=API).status_code, 400)
        self.assertEqual(lee.post('/api/assistant', json={'id': task['id'], 'action': 'delete'}, headers=API).status_code, 404)      # 남의 일
        self.assertEqual(lee.post('/api/assistant', json={'id': mine['tasks'][0]['id'], 'action': 'delete'}, headers=API).json()['tasks'], [])
        # 정해진 요일·시각에 해서 비서 대화에 올리고 메일로도 보낸다(2026-11-19 는 목요일)
        tick = lambda moment: asyncio.run(app.state.task_tick(datetime.fromisoformat(moment)))
        self.make, self.found, self.answer = None, {'total': 1, 'items': [{'title': '강원 드론 실증 지원', 'url': 'https://www.bizinfo.go.kr/a?id=1'}]}, '새 공고가 있습니다[1].'
        before = len(kim.get('/api/conversations/' + desk).json()['messages'])
        self.assertEqual((tick('2026-11-18T10:00'), tick('2026-11-19T09:29'), tick('2026-11-19T09:31')), ([], [], [('kim', '강원 드론')]))
        order = self.calls[-1][-1]['content']
        self.assertTrue(order.startswith('[맡긴 일 실행] 오늘은 2026-11-19(목요일)') and '마감된 것은 뺀다' in order and '처음 하는 일' in order, order)
        thread = kim.get('/api/conversations/' + desk).json()
        self.assertEqual((len(thread['messages']), thread['messages'][-2]['auto'], thread['messages'][-1]['content'], [source['url'] for source in thread['sources']]),
                         (before + 2, {'task': task['id'], 'title': '강원 드론', 'day': '2026-11-19'}, '새 공고가 있습니다[1].', ['https://www.bizinfo.go.kr/a?id=1']))
        self.assertEqual([(to, subject) for to, subject, _, _ in self.sent], [('kim@example.org', '강원 드론 · 11월 19일')])
        state = kim.get('/api/assistant').json()
        self.assertEqual((state['tasks'][0]['last']['status'], state['tasks'][0]['last']['day'], state['delivered'] > 0, tick('2026-11-19T15:00')), ('done', '2026-11-19', True, []))
        self.assertEqual(kim.post('/api/chat', json={'conversation_id': desk, 'regenerate': True}, headers=API).status_code, 400)      # 올라온 결과는 다시 생성하지 않는다
        self.assertEqual(kim.get('/api/search', params={'q': '맡긴 일 실행'}).json()['results'], [])                                   # 실행 지시는 사용자가 쓴 글이 아니다
        # 다음 차례: 지난번에 보여 준 것뿐이면 알리지 않는다. 새 자료가 있으면 올린다
        self.assertEqual(tick('2026-11-23T09:31'), [('kim', '강원 드론')])
        self.assertIn('지난번(2026-11-19)에 한 번 정리했습니다', self.calls[-1][-1]['content'])
        state = kim.get('/api/assistant').json()
        self.assertEqual((state['tasks'][0]['last']['status'], len(kim.get('/api/conversations/' + desk).json()['messages']), len(self.sent)), ('quiet', before + 2, 1))
        self.answer = '새 소식 없음'
        self.assertEqual((tick('2026-11-26T09:31'), kim.get('/api/assistant').json()['tasks'][0]['last']['status'], len(self.sent)), ([('kim', '강원 드론')], 'quiet', 1))
        self.found, self.answer = {'total': 1, 'items': [{'title': '강원 드론 규제특구', 'url': 'https://www.bizinfo.go.kr/a?id=2'}]}, '또 올라왔습니다[2].'
        self.assertEqual((tick('2026-11-30T09:31'), len(kim.get('/api/conversations/' + desk).json()['messages']), len(self.sent)), ([('kim', '강원 드론')], before + 4, 2))
        # 실패하면 기록만 남고, 화면에서 멈추거나 지울 수 있다. 화면에서 지금 하라고 하면 새 것이 없어도 올린다
        self.error = LlmError('모델 서버가 답하지 않았습니다')
        self.assertEqual(tick('2026-12-03T09:31'), [('kim', '강원 드론')])
        last = kim.get('/api/assistant').json()['tasks'][0]['last']
        self.assertEqual((last['status'], last['note'], len(kim.get('/api/conversations/' + desk).json()['messages'])), ('error', '모델 서버가 답하지 않았습니다', before + 4))
        self.error = None
        asyncio.run(app.state.run_task('kim', task['id'], '2026-12-03', force=True))
        self.assertEqual(len(kim.get('/api/conversations/' + desk).json()['messages']), before + 6)
        act = lambda action, headers=API: kim.post('/api/assistant', json={'id': task['id'], 'action': action}, headers=headers)
        self.assertEqual((act('pause', headers=ORIGIN).status_code, act('nonsense').status_code, act('pause').json()['tasks'][0]['paused'], tick('2026-12-07T09:31')), (403, 404, True, []))
        self.assertEqual((act('resume').json()['tasks'][0]['paused'], act('mail_off').json()['tasks'][0]['mail']), (False, False))
        # 서버를 다시 띄워도 맡긴 일과 비서 대화는 그대로다
        app = self.desk_app()
        kim = self.client('kim')
        state = kim.get('/api/assistant').json()
        self.assertEqual((state['conversation_id'], [entry['title'] for entry in state['tasks']], state['tasks'][0]['mail']), (desk, ['강원 드론'], False))
        self.assertEqual(kim.post('/api/assistant', json={'id': task['id'], 'action': 'delete'}, headers=API).json()['tasks'], [])

    def test_the_assistant_keeps_notes_and_today_shows_cards_reactions_and_suggestions(self):
        from datetime import datetime
        app = self.desk_app()
        kim = self.client('kim')
        desk = kim.get('/api/assistant').json()['conversation_id']
        post = lambda body, client=kim: client.post('/api/assistant', json=body, headers=API)
        # 메모: 비서가 적고(save_note), 사용자가 보고 고치고 지운다. 일을 할 때와 비서와 이야기할 때 읽힌다
        self.make = ('save_note', {'text': '  경기도 화성의   제조 중소기업 '})
        self.ask(kim, '우리는 경기도 화성의 제조 중소기업이야', desk)
        notes = kim.get('/api/assistant').json()['notes']
        self.assertEqual([(note['text'], note['by']) for note in notes], [('경기도 화성의 제조 중소기업', 'assistant')])
        self.assertTrue({'save_note', 'delete_note'} <= {spec['function']['name'] for spec in self.local.specs()})
        self.assertTrue(asyncio.run(self.local.call('delete_note', {'note_id': 'nope'})).is_error and asyncio.run(self.local.call('save_note', {'text': ' '})).is_error)
        self.make = None
        self.ask(kim, '뭐 적어 뒀어?', desk)
        self.assertIn(f'note_id {notes[0]["id"]}: 경기도 화성의 제조 중소기업', self.local.system_note)
        added = post({'action': 'note_save', 'text': '마감이 지난 공고는 보여 주지 않는다'}).json()['notes']
        self.assertEqual([(note['text'], note['by']) for note in added][1:], [('마감이 지난 공고는 보여 주지 않는다', 'user')])
        edited = post({'action': 'note_save', 'id': notes[0]['id'], 'text': '경기도 화성의 제조 중소기업(종업원 40명)'}).json()['notes']
        self.assertEqual((edited[0]['text'], edited[0]['by'], len(edited)), ('경기도 화성의 제조 중소기업(종업원 40명)', 'user', 2))
        self.assertEqual((post({'action': 'note_save', 'text': '  '}).status_code, post({'action': 'note_delete', 'id': 'nope'}).status_code,
                          post({'action': 'note_save', 'text': 'x'}, self.client('lee', 'plain-password')).json()['notes'][0]['text']), (400, 400, 'x'))      # 메모는 계정마다 따로
        self.assertEqual(len(post({'action': 'note_delete', 'id': added[1]['id']}).json()['notes']), 1)
        self.make = ('save_task', {'title': '스마트공장', 'what': '스마트공장 지원사업 공고', 'time': '09:00'})
        self.ask(kim, '스마트공장 공고 매일 9시에 알려줘', desk)
        self.make = ('save_task', {'title': '양자 과제', 'what': '양자컴퓨팅 과제', 'time': '18:00', 'notify': 'new'})
        self.ask(kim, '양자 과제 새로 뜨면 저녁 6시에', desk)
        self.make = None
        first, second = kim.get('/api/assistant').json()['tasks']
        # 오늘: 아직 한 것이 없으면 예정만 보인다
        seen = lambda moment: app.state.today_of('kim', datetime.fromisoformat(moment))
        early = seen('2026-11-19T08:00')
        self.assertEqual((early['cards'], early['upcoming'], early['quiet'], early['errors'], early['suggestions']),
                         ([], [{'title': '스마트공장', 'time': '09:00'}, {'title': '양자 과제', 'time': '18:00'}], [], [], []))
        tick = lambda moment: asyncio.run(app.state.task_tick(datetime.fromisoformat(moment)))
        self.found = {'total': 1, 'items': [{'title': '화성 스마트공장 구축 지원', 'url': 'https://www.bizinfo.go.kr/a?id=7'}]}
        self.answer = '**스마트공장 — 새 공고 1건**\n\n- 화성 스마트공장 구축 지원이 11월 30일에 마감됩니다[1].\n\n| 공고 | 마감 |\n|---|---|\n| 화성 스마트공장 구축 지원 | 11-30 |'
        self.assertEqual(tick('2026-11-19T09:05'), [('kim', '스마트공장')])
        self.assertIn('사용자에 대해 적어 둔 것(결과에 반영합니다):\n- 경기도 화성의 제조 중소기업(종업원 40명)', self.calls[-1][-1]['content'])
        today = seen('2026-11-19T09:10')
        card = today['cards'][0]
        self.assertEqual((today['day'], len(today['cards']), card['title'], card['summary'], card['feedback'], card['kept'], today['upcoming']),
                         ('2026-11-19', 1, '스마트공장', '스마트공장 — 새 공고 1건 화성 스마트공장 구축 지원이 11월 30일에 마감됩니다.', '', True, [{'title': '양자 과제', 'time': '18:00'}]))
        thread = kim.get('/api/conversations/' + desk).json()['messages']
        self.assertEqual((thread[card['index']]['auto']['title'], thread[card['index'] + 1]['content']), ('스마트공장', self.answer))
        # 반응은 다음에 할 때 반영된다(같은 것을 다시 누르면 지운다)
        self.assertEqual(post({'id': first['id'], 'action': 'down'}).status_code, 400)
        marked = post({'id': first['id'], 'action': 'down', 'day': '2026-11-19'}).json()
        self.assertEqual((marked['tasks'][0]['feedback'], seen('2026-11-19T09:20')['cards'][0]['feedback']), ({'2026-11-19': 'down'}, 'down'))
        self.answer = '새 소식 없음'
        self.assertEqual(tick('2026-11-19T18:01'), [('kim', '양자 과제')])
        evening = seen('2026-11-19T18:05')
        self.assertEqual((evening['quiet'], evening['upcoming'], len(evening['cards'])), (['양자 과제'], [], 1))
        self.error = LlmError('모델 서버가 답하지 않았습니다')
        self.assertEqual(tick('2026-11-20T09:05'), [('kim', '스마트공장')])
        self.assertIn('지난 결과(2026-11-19)를 사용자가 별로라고 했습니다', self.calls[-1][-1]['content'])
        failed = seen('2026-11-20T09:10')
        self.assertEqual((failed['errors'], failed['day'], len(failed['cards'])), ([{'task': first['id'], 'title': '스마트공장', 'note': '모델 서버가 답하지 않았습니다'}], '2026-11-19', 1))     # 오늘 것이 없으면 마지막 날 것
        self.error = None
        self.assertEqual(post({'id': first['id'], 'action': 'down', 'day': '2026-11-19'}).json()['tasks'][0]['feedback'], {})
        # 권하는 일: 보통 대화에서 거듭 물은 주제를 하루 한 번 골라 권한다. 받아들이면 일이 되고, 물리면 다시 권하지 않는다
        self.assertEqual(self.asked, [])                                              # 보통 대화가 없으면 모델에 묻지도 않는다
        self.ask(kim, '강원도 드론 지원사업 알려줘')
        self.ask(kim, '드론 실증 사업은?')
        self.topics = [{'topic': '강원도 드론 지원사업', 'kind': '공고', 'conditions': '강원도'}, {'topic': '스마트공장', 'kind': '공고', 'conditions': ''},
                       {'topic': '드론 실증', 'kind': '과제', 'conditions': ''}]
        tick('2026-11-21T10:01')
        questions, muted = self.asked[-1]
        self.assertEqual((sorted(item['question'] for item in questions), '스마트공장' in muted, '양자 과제' in muted), (['강원도 드론 지원사업 알려줘', '드론 실증 사업은?'], True, True))
        offered = kim.get('/api/assistant').json()['today']['suggestions']
        self.assertEqual([(item['topic'], item['kind']) for item in offered], [('강원도 드론 지원사업', '공고'), ('드론 실증', '과제')])      # 이미 맡긴 일과 같은 것은 권하지 않는다
        tick('2026-11-21T11:00')
        self.assertEqual(len(self.asked), 1)                                          # 하루 한 번만
        self.assertEqual(post({'id': 'nope', 'action': 'suggest_accept'}).status_code, 404)
        accepted = post({'id': offered[0]['id'], 'action': 'suggest_accept'}).json()
        self.assertEqual(([task['title'] for task in accepted['tasks']], accepted['tasks'][-1]['notify'], accepted['tasks'][-1]['what'], len(accepted['today']['suggestions'])),
                         (['스마트공장', '양자 과제', '강원도 드론 지원사업'], 'new', '강원도 드론 지원사업: 새로 올라온 지원사업 공고를 찾아 정리한다. 조건: 강원도.', 1))
        self.assertEqual(post({'id': offered[1]['id'], 'action': 'suggest_dismiss'}).json()['today']['suggestions'], [])
        tick('2026-11-22T10:01')
        self.assertEqual(len(self.asked), 1)                                          # 새 대화가 없었으면 다시 묻지 않는다
        self.ask(kim, '드론 실증 과제 더 알려줘')
        tick('2026-11-23T10:01')
        self.assertEqual((len(self.asked), '드론 실증' in self.asked[-1][1], kim.get('/api/assistant').json()['today']['suggestions']), (2, True, []))     # 물린 주제는 빼 달라고 한다

    def test_tasks_wait_for_the_morning_refresh_and_old_brief_topics_become_a_task(self):
        from datetime import datetime
        database = storage.Database(Path(self.data) / 'chat.sqlite3')
        database.save_profile('kim', {'interests': [{'topic': '드론의 농업 활용', 'kind': '둘 다', 'conditions': '경기도', 'pinned': True}], 'paused': False}, 1.0)
        database.save_brief('kim', '2026-11-18', 'done', 1.0)
        status = Path(self.data) / 'refresh.json'
        app = self.desk_app(refresh_time='09:00', refresh_status=str(status), wait_minutes=30, time='07:00')
        kim = self.client('kim')
        moved = kim.get('/api/assistant').json()['tasks']
        self.assertEqual([(task['title'], task['when'], task['mail'], task['notify']) for task in moved], [('아침 브리핑', '매일 07:00', True, 'always')])     # 예전 브리핑의 주제가 일 하나로
        self.assertTrue('드론의 농업 활용' in moved[0]['what'] and '경기도' in moved[0]['what'])
        self.assertEqual(database.tasks('kim')[0]['last_ok'], '2026-11-18')
        tick = lambda moment: asyncio.run(app.state.task_tick(datetime.fromisoformat(moment)))
        self.answer = '새 소식 없음'
        self.assertEqual(tick('2026-11-19T08:00'), [])                                # 자료 갱신 시각보다 이르게 잡은 일은 그 시각에 맞춘다
        status.write_text(json.dumps({'started': '2026-11-19T09:00:01', 'finished': ''}))
        self.assertEqual(tick('2026-11-19T09:10'), [])                                # 갱신이 도는 동안은 조금 기다린다
        self.assertEqual(tick('2026-11-19T09:31'), [('kim', '아침 브리핑')])            # 너무 오래 걸리면 그냥 한다
        thread = kim.get('/api/conversations/' + kim.get('/api/assistant').json()['conversation_id']).json()
        self.assertEqual((thread['messages'][-1]['content'], thread['messages'][-2]['auto']['title']), ('새 소식 없음', '아침 브리핑'))      # 할 때마다 알리는 일은 없다는 것도 알린다
        kim.post('/api/assistant', json={'id': moved[0]['id'], 'action': 'delete'}, headers=API)
        self.desk_app(refresh_time='09:00')
        self.assertEqual(self.client('kim').get('/api/assistant').json()['tasks'], [])        # 지운 일이 다시 생기지 않는다(한 번만 옮긴다)

    def test_conversations_can_be_searched_by_what_was_said(self):
        kim, lee = self.client('kim'), self.client('lee', 'plain-password')
        self.answer = '**수소 연료전지** 과제는 | 3건 | 입니다. ' + '가' * 200 + ' 끝에 나오는 낱말 드론'
        first = self.ask(kim, '수소 과제 알려줘')[1][-1]['conversation_id']
        self.answer = '공고 두 건입니다.'
        second = self.ask(kim, '강원도 지원사업')[1][-1]['conversation_id']
        self.ask(lee, '수소 과제는 내 것')
        find = lambda client, words: client.get('/api/search', params={'q': words}).json()['results']
        self.assertEqual([item['id'] for item in find(kim, '수소')], [first])                  # 남의 대화는 찾지 않는다
        self.assertTrue(find(kim, '연료전지 3건')[0]['snippet'].startswith('수소 연료전지 과제는 3건 입니다.'))      # 답의 글에서도 찾고, 기호는 뺀다
        tail = find(kim, '드론')[0]['snippet']
        self.assertTrue(tail.startswith('…') and tail.endswith('드론') and len(tail) < 160)
        self.assertEqual([item['id'] for item in find(kim, '공고 강원도')], [second])            # 낱말은 모두 있어야 한다
        self.assertNotIn('snippet', find(kim, '강원도')[0])                              # 제목이 된 질문만 맞으면 덧붙일 글이 없다
        self.assertEqual((find(kim, '없는말'), find(kim, '  ')), ([], []))
        self.assertEqual(self.client().get('/api/search?q=x').status_code, 401)

    def test_documents_are_listed_apart_from_conversations_and_can_be_deleted(self):
        kim = self.client('kim')
        self.assertEqual(kim.get('/api/files').json(), {'files': [], 'bytes': 0})
        self.make = ('create_document', {'filename': '신청서', 'title': '참가 신청서', 'blocks': [{'type': 'paragraph', 'text': '내용'}]})
        _, events = self.ask(kim, '신청서 만들어줘')
        conversation_id, first = events[-1]['conversation_id'], [event['file'] for event in events if event['type'] == 'file'][0]
        kim.post('/api/chat', json={'conversation_id': conversation_id, 'regenerate': True}, headers=API)
        made = kim.get('/api/conversations/' + conversation_id).json()['messages'][1]['files'][0]
        self.assertNotEqual(made['id'], first['id'])
        self.assertEqual(kim.get('/api/files/' + first['id']).status_code, 404)       # 다시 생성으로 사라진 답의 파일은 남기지 않는다
        self.make = None
        uploaded = kim.post('/api/files', files={'file': ('양식.pdf', tiny_pdf(), 'application/pdf')}, headers=API).json()
        listing = kim.get('/api/files').json()
        self.assertEqual([(item['id'], item['name'], item['origin'], item.get('conversation')) for item in listing['files']],
                         [(uploaded['id'], '양식.pdf', 'upload', None), (made['id'], '신청서.docx', 'generated', {'id': conversation_id, 'title': '신청서 만들어줘'})])
        self.assertEqual(listing['bytes'], uploaded['size'] + made['size'])
        self.assertEqual((listing['files'][0]['pages'], 'changes' in listing['files'][1]), (1, False))
        lee = self.client('lee', 'plain-password')
        self.assertEqual(lee.get('/api/files').json()['files'], [])                   # 남의 문서는 목록에도 없다
        self.assertEqual(self.client().get('/api/files').status_code, 401)
        self.assertEqual(kim.delete('/api/files/' + made['id'], headers=ORIGIN).status_code, 403)
        self.assertEqual(lee.delete('/api/files/' + made['id'], headers=API).status_code, 404)
        self.assertEqual(kim.get('/api/files/' + made['id']).status_code, 200)
        self.assertEqual(kim.delete('/api/files/' + made['id'], headers=API).json(), {'deleted': True})
        self.assertEqual(kim.get('/api/files/' + made['id']).status_code, 404)
        self.assertNotIn('files', kim.get('/api/conversations/' + conversation_id).json()['messages'][1])     # 대화에 열리지 않는 카드가 남지 않는다
        self.assertEqual([item['id'] for item in kim.get('/api/files').json()['files']], [uploaded['id']])
        self.app = web.create_app(self.settings, self.users, runner=lambda *args: None, throttle=self.throttle)     # 서버를 다시 띄워도 그대로다
        kim = self.client('kim')
        self.assertEqual([item['id'] for item in kim.get('/api/files').json()['files']], [uploaded['id']])
        self.assertNotIn('files', kim.get('/api/conversations/' + conversation_id).json()['messages'][1])

    def signup_app(self, **options):
        """회원가입 신청을 켠 설정으로 앱을 다시 만든다. 메일 보내기는 가짜로 바꾼다."""
        import dataclasses
        from chat.settings import Briefing, Mail, Signup
        self.signup_settings = dataclasses.replace(
            self.settings, signup=Signup(enabled=True, admin='kim', **options), briefing=Briefing(app_url='https://rnd.test'),
            mail=Mail(username='bot@gmail.com', password='app-password'), users=({'name': 'kim', 'email': 'kim@example.org'}, {'name': 'lee'}))
        self.sent = []
        self.addCleanup(setattr, web.mailer, 'send', web.mailer.send)
        web.mailer.send = lambda mail, to, subject, page, plain: self.sent.append((to, subject, page, plain))
        self.restart()

    def restart(self):
        self.users = auth.Users([{'name': 'kim', 'password_hash': HASH}, {'name': 'lee', 'password': 'plain-password'}])
        self.app = web.create_app(self.signup_settings, self.users, runner=lambda *args: None, throttle=self.throttle)

    def test_signup_is_only_a_request_until_the_admin_approves_it(self):
        guest = self.client()
        self.assertEqual((guest.get('/signup').status_code, 'href="/signup"' in guest.get('/login').text), (404, False))      # 꺼 두면 없다
        self.assertEqual(guest.post('/api/signup', json={}, headers=API).status_code, 404)
        self.signup_app()
        guest = self.client()
        page = guest.get('/signup')
        self.assertEqual((page.status_code, '관리자가 검토한 뒤 계정이 등록됩니다' in page.text, 'href="/signup"' in guest.get('/login').text), (200, True, True))
        ask = lambda body, headers=API: guest.post('/api/signup', json=body, headers=headers)
        good = {'username': '박연구', 'email': 'park@example.org', 'password': 'correct horse'}
        self.assertEqual(ask(good, headers=ORIGIN).status_code, 403)                  # 이 화면에서 보낸 요청만 받는다
        self.assertEqual(ask({**good, 'password': 'x' * 5000}).status_code, 413)
        for change, field in (({'username': 'a'}, 'username'), ({'username': 'bad name'}, 'username'), ({'username': '<b>'}, 'username'), ({'email': 'not-mail'}, 'email'),
                              ({'email': 'a@b.c, evil@example.org'}, 'email'), ({'password': 'short'}, 'password'), ({'password': '박연구'}, 'password')):
            response = ask({**good, **change})
            self.assertEqual((response.status_code, response.json().get('field')), (400, field), change)
        self.assertEqual((ask({**good, 'username': 'KIM'}).status_code, ask({**good, 'username': 'lee'}).json()['field']), (409, 'username'))    # 있는 계정의 이름
        self.assertEqual(ask(good).json(), {'ok': True})
        self.assertEqual(ask({**good, 'email': 'other@example.org'}).status_code, 409)                # 같은 아이디로 또 신청
        to, subject, page, plain = self.sent[0]
        self.assertEqual((len(self.sent), to, subject), (1, 'kim@example.org', '가입 신청: 박연구'))    # 관리자에게 알린다
        self.assertTrue('park@example.org' in page and 'https://rnd.test/?view=accounts' in page and 'correct horse' not in page + plain and 'scrypt' not in page + plain)
        # 승인 전에는 로그인되지 않고(없는 계정과 똑같이 거절), 관리자 말고는 신청을 볼 수 없다
        refused = guest.post('/login', data={'username': '박연구', 'password': 'correct horse'}, headers=ORIGIN)
        self.assertEqual(refused.headers['location'], '/login?error=1')
        lee, kim = self.client('lee', 'plain-password'), self.client('kim')
        self.assertEqual((lee.get('/api/accounts').status_code, 'admin' in lee.get('/api/me').json(), self.client().get('/api/accounts').status_code), (404, False, 401))
        self.assertEqual((kim.get('/api/me').json()['admin'], kim.get('/api/me').json()['signups']), (True, 1))
        listing = kim.get('/api/accounts').json()
        self.assertEqual(([(entry['name'], entry['email']) for entry in listing['pending']], listing['active'], 'password_hash' in str(listing)),
                         ([('박연구', 'park@example.org')], [], False))
        decide = lambda name, action, client=kim, headers=API: client.post('/api/accounts', json={'name': name, 'action': action}, headers=headers)
        self.assertEqual((decide('박연구', 'approve', headers=ORIGIN).status_code, decide('박연구', 'approve', client=lee).status_code,
                          decide('없는이', 'approve').status_code, decide('박연구', 'remove').status_code), (403, 404, 404, 404))
        approved = decide('박연구', 'approve').json()
        self.assertEqual(([entry['name'] for entry in approved['active']], approved['pending']), (['박연구'], []))
        self.assertEqual([(to, subject) for to, subject, _, _ in self.sent[1:]], [('park@example.org', '가입이 승인되었습니다')])
        park = self.client('박연구', 'correct horse')
        self.assertEqual(park.get('/api/me').json()['name'], '박연구')
        self.assertEqual(self.client().post('/login', data={'username': '박연구', 'password': 'wrong'}, headers=ORIGIN).headers['location'], '/login?error=1')
        # 서버를 다시 띄워도 등록된 계정과 그 로그인은 그대로다
        cookie = park.cookies.get(web.COOKIE)
        self.restart()
        park = self.client()
        park.cookies.set(web.COOKIE, cookie)
        self.assertEqual(park.get('/api/me').json()['name'], '박연구')
        self.client('박연구', 'correct horse')
        # 거절하면 신청이 사라지고, 등록된 계정을 지우면 로그인과 남긴 것이 함께 사라진다
        kim, guest = self.client('kim'), self.client()
        decide = lambda name, action: kim.post('/api/accounts', json={'name': name, 'action': action}, headers=API)
        self.assertEqual(guest.post('/api/signup', json={'username': 'choi', 'email': 'choi@example.org', 'password': 'long enough'}, headers=API).json(), {'ok': True})
        self.assertEqual(decide('choi', 'reject').json()['pending'], [])
        self.assertEqual(guest.post('/login', data={'username': 'choi', 'password': 'long enough'}, headers=ORIGIN).headers['location'], '/login?error=1')
        database = storage.Database(Path(self.data) / 'chat.sqlite3')
        database.save('박연구', {'id': 'c1', 'title': '남긴 대화', 'messages': [], 'updated': 1.0})
        self.assertEqual(decide('박연구', 'remove').json(), {'pending': [], 'active': []})
        self.assertEqual((park.get('/api/me').status_code, database.conversations().get('박연구'), [row[1] for row in database.sessions() if row[1] == '박연구']), (401, None, []))
        self.assertEqual(self.client().post('/login', data={'username': '박연구', 'password': 'correct horse'}, headers=ORIGIN).headers['location'], '/login?error=1')

    def test_signup_mail_carries_the_steps_and_commands_and_the_terminal_command_registers_the_account(self):
        import contextlib
        from chat import accounts
        folder = Path(tempfile.mkdtemp(prefix='rndchat-test-'))
        self.addCleanup(shutil.rmtree, folder, True)
        config = folder / 'config.toml'
        config.write_text(f"""[llm]\nbase_url = "http://llm.test/v1"\nmodel = "hai"\n\n[[users]]\nname = "kim"\npassword_hash = "{HASH}"\nemail = "kim@example.org"\n
[mail]\nusername = "bot@gmail.com"\npassword = "app-password"\n\n[briefing]\napp_url = "https://rnd.test"\n\n[signup]\nenabled = true\nadmin = "kim"\nnotify = "boss@example.org"\n""", encoding='utf-8')
        settings = Settings.load(config)
        self.assertEqual((settings.config, settings.signup.notify), (str(config), 'boss@example.org'))
        self.sent = []
        self.addCleanup(setattr, web.mailer, 'send', web.mailer.send)
        web.mailer.send = lambda mail, to, subject, page, plain: self.sent.append((to, subject, page, plain))
        self.app = web.create_app(settings, auth.Users(list(settings.users)), runner=lambda *args: None, throttle=self.throttle)
        guest = self.client()
        for name in ('박연구', 'choi'):
            self.assertEqual(guest.post('/api/signup', json={'username': name, 'email': f'{name}@example.org', 'password': 'correct horse'}, headers=API).json(), {'ok': True})
        to, subject, page, plain = self.sent[0]
        self.assertEqual((to, subject), ('boss@example.org', '가입 신청: 박연구'))                 # [signup] notify 로 간다
        command = f'-m chat.accounts --config {config} approve 박연구'
        for part in ('화면에서 등록하기', 'kim 계정으로 로그인합니다.', '왼쪽 메뉴의 "계정"을 엽니다.', '서버 터미널에서 등록하기', command, f'--config {config} reject 박연구', '알아 둘 것'):
            self.assertIn(part, plain)
        self.assertTrue(command in page and '<pre' in page and '&quot;계정&quot;' in page and 'correct horse' not in page + plain)
        # 메일에 적힌 명령으로 등록한다: 서버를 다시 띄우지 않아도 그 아이디로 로그인된다
        refused = guest.post('/login', data={'username': '박연구', 'password': 'correct horse'}, headers=ORIGIN)
        self.assertEqual(refused.headers['location'], '/login?error=1')

        def run(*arguments):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = accounts.main(['--config', str(config), *arguments])
            return code, out.getvalue() + err.getvalue()
        code, listing = run('list')
        self.assertTrue(code == 0 and '기다리는 신청 2건' in listing and '박연구  박연구@example.org' in listing and '등록된 계정 0건' in listing, listing)
        code, said = run('approve', '박연구')
        self.assertTrue(code == 0 and '등록했습니다' in said and '승인 메일을 보냈습니다: 박연구@example.org' in said, said)
        self.assertEqual([(to, subject) for to, subject, _, _ in self.sent[2:]], [('박연구@example.org', '가입이 승인되었습니다')])
        self.assertEqual(self.client('박연구', 'correct horse').get('/api/me').json()['name'], '박연구')
        self.assertEqual((run('approve', '박연구')[0], run('approve', 'nobody')[0], run('reject', 'choi')[0], '기다리는 신청 0건' in run('list')[1]), (1, 1, 0, True))
        self.assertEqual(guest.post('/login', data={'username': 'choi', 'password': 'correct horse'}, headers=ORIGIN).headers['location'], '/login?error=1')
        database = storage.Database(folder / 'data' / 'chat.sqlite3')
        database.add_account('kim', 'x@example.org', HASH, 1.0)                               # 설정의 계정과 이름이 겹치는 신청은 등록하지 않는다
        self.assertEqual((run('approve', 'kim')[0], [account['status'] for account in database.accounts() if account['name'] == 'kim']), (1, ['pending']))

    def test_a_logged_in_visitor_is_not_left_on_the_login_page_and_signup_does_not_log_anyone_in(self):
        self.signup_app()
        guest, kim = self.client(), self.client('kim')
        # 로그인하지 않은 사람: 본 화면은 로그인 화면으로 보내고(알림 메일의 링크면 갈 곳을 기억해 둔다), 회원가입 화면은 그대로 보인다
        self.assertEqual((guest.get('/').headers['location'], guest.get('/?view=accounts').headers['location'], guest.get('/?view=evil').headers['location']),
                         ('/login', '/login?next=accounts', '/login'))
        self.assertEqual((guest.get('/signup').status_code, guest.get('/api/me').status_code, 'set-cookie' in guest.get('/signup').headers), (200, 401, False))
        # 로그인 화면은 스스로 로그인 여부를 확인해 가려던 곳으로 보낸다(다른 사이트에서 넘어온 첫 요청에는 쿠키가 실리지 않는다)
        script = guest.get('/static/login.js').text
        self.assertTrue("fetch('/api/me'" in script and "location.replace(onward ? '/?view=accounts' : '/')" in script and 'name="next"' in guest.get('/login').text)
        # 로그인한 사람이 회원가입 화면을 열어도 본 화면으로 튀지 않는다
        self.assertEqual((kim.get('/signup').status_code, 'location' in kim.get('/signup').headers), (200, False))
        # 로그인하면 기억해 둔 곳으로 간다. 정해 둔 값이 아니면 무시한다
        go = lambda **form: self.client().post('/login', data={'username': 'kim', 'password': PASSWORD, **form}, headers=ORIGIN).headers['location']
        self.assertEqual((go(next='accounts'), go(next='https://evil.example/'), go()), ('/?view=accounts', '/', '/'))
        wrong = self.client().post('/login', data={'username': 'kim', 'password': 'wrong', 'next': 'accounts'}, headers=ORIGIN)
        self.assertEqual(wrong.headers['location'], '/login?error=1&next=accounts')

    def test_signup_requests_are_limited(self):
        self.signup_app(per_hour=4, max_pending=3)
        guest = self.client()
        ask = lambda number: guest.post('/api/signup', json={'username': f'user{number}', 'email': f'u{number}@example.org', 'password': 'long enough'}, headers=API).status_code
        self.assertEqual([ask(number) for number in range(4)], [200, 200, 429, 429])       # 한 접속 주소는 한 시간 한도의 절반까지
        self.assertEqual(len(self.sent), 2)
        storage.Database(Path(self.data) / 'chat.sqlite3').add_account('waiting', 'w@example.org', HASH, 1.0)
        self.restart()
        late = self.client().post('/api/signup', json={'username': 'late', 'email': 'l@example.org', 'password': 'long enough'}, headers=API)
        self.assertEqual(late.status_code, 429)                                            # 검토를 기다리는 신청이 한도만큼 쌓였다
        from chat import settings as loaded
        with self.assertRaises(ValueError):
            loaded._signup({'enabled': True, 'admin': 'nobody'}, ({'name': 'kim'},))
        self.assertEqual(loaded._signup({'enabled': True, 'admin': 'kim'}, ({'name': 'kim'},)).admin, 'kim')

    def test_tool_status_lists_servers_tools_and_the_morning_refresh(self):
        import dataclasses
        from types import SimpleNamespace
        from chat.settings import Briefing
        report = Path(self.data) / 'refresh.json'
        report.write_text(json.dumps({'started': '2026-11-21T09:00:02', 'finished': '2026-11-21T09:03:30', 'ok': False, 'steps': [
            {'name': '진행 중 공고', 'ok': True, 'last': 'INFO: 일일 갱신 결과: 활성 1358건, 신규 26건, 목록 제외 19건'},
            {'name': '공고 이력', 'ok': False, 'last': 'ERROR: 수집 중단: 같은 출력 디렉토리에서 이미 수집이 실행 중입니다: /home/someone/data/history'},
            {'name': '뜻 색인', 'ok': True, 'last': '{"notices": 54011, "indexed": 54011}'}]}, ensure_ascii=False), encoding='utf-8')
        up, down = SimpleNamespace(name='bizinfo'), SimpleNamespace(name='ntis')
        hub = SimpleNamespace(servers=[up, down], unavailable={'ntis': 'ConnectError: http://10.0.0.1:8768'},
                              tools={name: SimpleNamespace(name=name, server=up) for name in ('search_notices', 'get_notice')})
        settings = dataclasses.replace(self.settings, briefing=Briefing(refresh_status=str(report)))
        self.app = web.create_app(settings, self.users, runner=lambda *a: None, hub=hub, throttle=self.throttle)
        response = self.client('kim').get('/api/status')
        status = response.json()
        self.assertEqual(status['model'], {'name': 'hai', 'ok': None})
        self.assertEqual(status['servers'], [{'name': 'bizinfo', 'ok': True, 'tools': ['search_notices', 'get_notice']}, {'name': 'ntis', 'ok': False, 'tools': []}])
        self.assertTrue({'edit_document', 'create_document', 'review_all'} <= set(status['local']))
        self.assertEqual(status['refresh'], {'started': '2026-11-21T09:00:02', 'finished': '2026-11-21T09:03:30', 'steps': [
            {'name': '진행 중 공고', 'ok': True, 'note': '1,358건 · 새로 26 · 내림 19'}, {'name': '공고 이력', 'ok': False, 'note': '다른 수집이 돌고 있어 건너뜀'},
            {'name': '뜻 색인', 'ok': True, 'note': '54,011건'}]})
        self.assertFalse('/home/' in response.text or '10.0.0.1' in response.text)          # 경로와 내부 주소는 내보내지 않는다
        self.assertEqual(self.client().get('/api/status').status_code, 401)

    def test_brief_helpers_pick_recent_questions_and_survive_a_bad_model_reply(self):
        from chat import brief, mailer
        from chat.settings import Mail
        now = 1_800_000_000.0
        items = [{'updated': now - 86400, 'messages': [{'role': 'user', 'content': ' 드론   공고 '}, {'role': 'assistant', 'content': '답'}]},
                 {'updated': now - 40 * 86400, 'messages': [{'role': 'user', 'content': '오래된 질문'}]},
                 {'updated': now, 'kind': 'brief', 'messages': [{'role': 'user', 'content': '11월 21일 브리핑'}, {'role': 'assistant', 'content': '글'},
                                                               {'role': 'user', 'content': '이 중 마감 임박한 것만'}]}]
        self.assertEqual([item['question'] for item in brief.asked(items, now, 30)], ['이 중 마감 임박한 것만', '드론 공고'])

        class Model:
            def __init__(self, content):
                self.content = content

            async def complete(self, messages, tools=None, on_text=None, thinking=None, max_tokens=None):
                self.prompt = messages
                if self.content is None:
                    raise LlmError('실패')
                return {'content': self.content, 'tool_calls': [], 'finish_reason': 'stop', 'usage': {}}
        previous = [{'topic': '앞서 뽑은 주제', 'kind': '공고', 'conditions': ''}]
        good = Model('다음과 같습니다. [{"topic": " 드론의  농업 활용 ", "kind": "이상한 값", "conditions": "경기도"}, {"topic": "드론의 농업 활용"}, {"topic": ""}, 3]')
        self.assertEqual(asyncio.run(brief.interests(good, [{'day': '11-19', 'question': '드론 공고'}], previous, 5)),
                         [{'topic': '드론의 농업 활용', 'kind': '공고', 'conditions': '경기도'}])
        self.assertIn('앞서 뽑아 둔 주제', good.prompt[1]['content'])
        for broken in (Model('모르겠습니다'), Model('[{"topic": '), Model(None)):                 # 답을 읽지 못하면 앞서 뽑아 둔 것을 쓴다
            self.assertEqual(asyncio.run(brief.interests(broken, [{'day': '11-19', 'question': 'x'}], previous, 5)), previous)
        from chat import tasks
        items.append({'updated': now, 'kind': 'assistant', 'messages': [{'role': 'user', 'content': '[맡긴 일 실행] …', 'auto': {'task': 't'}}, {'role': 'assistant', 'content': '결과'}]})
        self.assertNotIn('[맡긴 일 실행] …', [item['question'] for item in brief.asked(items, now, 30)])      # 실행 지시는 사용자의 질문이 아니다
        task = tasks.clean({'title': '드론', 'what': '드론 공고', 'days': '평일', 'notify': 'new'})
        later = tasks.instruction(task, '2026-11-21', '2026-11-20')
        self.assertTrue('posted_from 에 2026-11-20' in later and '2026-11-28' in later and '처음 하는 일' not in later and '"새 소식 없음" 한 줄만' in later)
        self.assertEqual((tasks.when_text(task), tasks.is_quiet('**새 소식 없음.**')), ('평일 10:00', True))
        self.assertTrue(tasks.is_quiet('**강원 드론 지원사업 — 새로 뜬 공고 없음**\n\n진행 중 공고 15건을 전부 확인했는데 드론 공고는 없습니다.'))      # 모델이 제 말로 없다고 쓴 것
        for told in ('새 소식 없음\n\n다만 아래 공고는 마감이 가깝습니다[3].', '새로 올라온 공고는 없습니다.\n\n| 공고 | 마감 |\n|---|---|\n| 가 | 11-30 |', '새 공고가 있습니다[1].'):
            self.assertFalse(tasks.is_quiet(told), told)
        letter = mailer.message(Mail(username='bot@gmail.com', password='x', sender_name='R&D 검색'), 'kim@example.org', '11월 21일\n브리핑', '<p>글</p>', '글')
        self.assertEqual((letter['To'], letter['Subject'], 'bot@gmail.com' in letter['From'], bool(letter['Date'])), ('kim@example.org', '11월 21일 브리핑', True, True))
        for bad in ('주소 아님', 'a@b', 'kim@example.org, evil@example.org'):
            with self.assertRaises(ValueError):
                mailer.send(Mail(username='bot@gmail.com', password='x'), bad, '제목', '<p>글</p>', '글')

    def test_answers_carry_sources_and_a_record_of_tool_use(self):
        kim = self.client('kim')
        self.found = {'total': 2, 'items': [{'title': '가 과제', 'organization': '가 대학', 'url': 'https://www.ntis.go.kr/p?id=1'},
                                            {'title': '나 과제', 'url': 'https://www.ntis.go.kr/p?id=2'}]}
        self.answer = '나 과제가 더 큽니다[2]. 가 과제도 있습니다[1].\n\n| 과제 | 기관 |\n|---|---|\n| 가 과제[1] | 가 대학 |'
        _, events = self.ask(kim, '양자 과제')
        self.assertEqual([item['cite'] for item in json.loads(self.seen)['items']], [1, 2])     # 모델이 본 결과에 번호가 있다
        self.assertEqual([source['n'] for event in events if event['type'] == 'sources' for source in event['sources']], [1, 2])
        conversation_id = events[-1]['conversation_id']
        stored = kim.get('/api/conversations/' + conversation_id).json()
        self.assertEqual([(source['n'], source['title'], source['site']) for source in stored['sources']], [(1, '가 과제', 'NTIS'), (2, '나 과제', 'NTIS')])
        answer = stored['messages'][1]
        self.assertEqual([(step['name'], step['is_error']) for step in answer['steps']], [('count_projects', False), ('search_projects', False)])
        self.assertEqual(answer['steps'][1]['args'], {'query_text': '양자', 'edits': '2건'})      # 긴 인자는 건수만 남긴다
        self.assertEqual(answer['steps'][1]['summary']['items'][0], {'n': 1, 'title': '가 과제'})
        self.assertGreaterEqual(answer['seconds'], 0)
        # 이어지는 질문: 같은 자료는 같은 번호, 앞서 쓰인 번호는 모델에 알려 준다
        self.found = {'items': [{'title': '나 과제', 'url': 'https://www.ntis.go.kr/p?id=2'}, {'title': '다 과제', 'url': 'https://www.ntis.go.kr/p?id=3'}]}
        self.ask(kim, '더 찾아줘', conversation_id)
        self.assertEqual([item['cite'] for item in json.loads(self.seen)['items']], [2, 3])
        self.assertIn('[2] 나 과제 — https://www.ntis.go.kr/p?id=2', self.local.system_note)
        # 앞서 무엇을 찾았고 그중 얼마나 봤는지도 알려 준다(앞선 답의 목록에서만 골라 답하지 않게)
        self.assertIn('search_projects(query_text=양자, edits=2건) → 전체 2건', self.local.system_note)
        self.assertEqual(len(kim.get('/api/conversations/' + conversation_id).json()['sources']), 3)
        # 내보낸 문서: 번호는 처음 나온 순서로 바뀌고 끝에 출처가 붙는다
        exported = Document(io.BytesIO(kim.get(f'/api/conversations/{conversation_id}/export?index=1&format=docx').content))
        text = '\n'.join(paragraph.text for paragraph in exported.paragraphs)
        self.assertIn('나 과제가 더 큽니다[1]. 가 과제도 있습니다[2].', text)
        self.assertIn('[1] 나 과제 — https://www.ntis.go.kr/p?id=2', text)
        self.assertEqual(exported.tables[0].cell(1, 0).text, '가 과제[2]')
        self.assertEqual(self.client('lee', 'plain-password').get('/api/conversations/' + conversation_id).status_code, 404)

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
