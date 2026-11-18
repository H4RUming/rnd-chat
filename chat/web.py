"""웹 채팅 서버:  python -m chat.web

로그인한 사람만 쓸 수 있다. 외부에 공개할 것을 전제로 다음을 지킨다.
- 127.0.0.1 에만 연다. 밖에서는 터널(Cloudflare 등)을 거쳐서만 들어온다.
- 로그인·정적 파일 말고는 모두 세션이 있어야 한다. 세션은 서버 메모리에 두고 쿠키에는 무작위 토큰만 싣는다
  (HttpOnly, SameSite=Strict, https 면 Secure).
- 상태를 바꾸는 요청은 Origin 이 같은 호스트여야 하고, API 는 전용 헤더까지 있어야 한다(CSRF 방지).
- 로그인 실패는 계정별로, 그리고 더 느슨한 한도로 접속 IP 별로 세어 잠근다. 실패 이유는 구분해 알려 주지 않는다.
- 모든 응답에 보안 헤더(CSP 는 인라인 스크립트 금지)를 붙인다. 모델의 답은 브라우저에서 DOM 으로만 그린다.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
import html
import json
import logging
from pathlib import Path
import secrets
import sys
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import agent
from .auth import Session, Sessions, Throttle, Users
from .llm import Client, LlmError
from .settings import Settings
from .tools import Hub

log = logging.getLogger('rnd-chat')
COOKIE = 'rnd_session'
API_HEADER, API_HEADER_VALUE = 'x-requested-with', 'rnd-chat'
STATIC = Path(__file__).resolve().parent / 'static'
STATIC_FILES = {'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}
MAX_CONVERSATIONS, MAX_MESSAGES, CONTEXT_MESSAGES = 50, 200, 20
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
LOGIN_ERRORS = {'1': '이름 또는 비밀번호가 맞지 않습니다.', 'locked': '로그인 시도가 많아 잠시 잠겼습니다. 잠시 뒤 다시 해 보세요.',
                'origin': '요청을 확인하지 못했습니다. 이 페이지에서 다시 로그인해 주세요.'}
LOGIN_PAGE = '''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>로그인 · R&amp;D 검색</title><link rel="stylesheet" href="/static/style.css"></head>
<body class="login"><main><h1>R&amp;D 검색</h1>
<form method="post" action="/login" autocomplete="on">{error}
<label>이름<input name="username" autocomplete="username" maxlength="64" required autofocus></label>
<label>비밀번호<input name="password" type="password" autocomplete="current-password" maxlength="256" required></label>
<button type="submit">로그인</button></form></main></body></html>'''

Runner = Callable[[list[dict[str, Any]], agent.Listener], Awaitable[agent.Outcome]]


class SecurityHeaders:
    """모든 응답에 보안 헤더를 붙이는 ASGI 미들웨어(스트리밍 응답도 그대로 흘려보낸다)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        https = scope.get('scheme') == 'https' or dict(scope['headers']).get(b'x-forwarded-proto') == b'https'

        async def wrapped(message):
            if message['type'] == 'http.response.start':
                headers = [(name, value) for name, value in message.get('headers', []) if name.lower() != b'server']
                present = {name.lower() for name, _ in headers}
                extra = {b'content-security-policy': CSP.encode(), b'x-content-type-options': b'nosniff', b'x-frame-options': b'DENY',
                         # no-referrer 로 두면 브라우저가 같은 사이트의 폼 전송에도 Origin: null 을 보내 로그인이 막힌다(실측)
                         b'referrer-policy': b'same-origin', b'permissions-policy': b'camera=(), microphone=(), geolocation=()',
                         b'cross-origin-opener-policy': b'same-origin', b'cache-control': b'no-store'}
                if https:
                    extra[b'strict-transport-security'] = b'max-age=31536000'
                message = {**message, 'headers': headers + [(name, value) for name, value in extra.items() if name not in present]}
            await send(message)
        await self.app(scope, receive, wrapped)


class Conversations:
    """계정별 대화(메모리). 서버를 다시 띄우면 사라진다. 모델에는 사용자·조수의 글만 넘기고 도구 왕복은 남기지 않는다."""

    def __init__(self):
        self._data: dict[str, OrderedDict[str, dict[str, Any]]] = {}

    def list(self, user: str) -> list[dict[str, Any]]:
        return [{'id': item['id'], 'title': item['title'], 'updated': item['updated']} for item in reversed(self._data.get(user, {}).values())]

    def get(self, user: str, conversation_id: str) -> dict[str, Any] | None:
        return self._data.get(user, {}).get(conversation_id)

    def delete(self, user: str, conversation_id: str) -> bool:
        return self._data.get(user, {}).pop(conversation_id, None) is not None

    def append(self, user: str, conversation_id: str | None, question: str, answer: str) -> dict[str, Any]:
        mine = self._data.setdefault(user, OrderedDict())
        item = mine.get(conversation_id or '')
        if item is None:
            item = {'id': secrets.token_urlsafe(9), 'title': ' '.join(question.split())[:40], 'messages': [], 'updated': 0.0}
            mine[item['id']] = item
            while len(mine) > MAX_CONVERSATIONS:
                mine.popitem(last=False)
        item['messages'] += [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}]
        del item['messages'][:-MAX_MESSAGES]
        item['updated'] = time.time()
        mine.move_to_end(item['id'])
        return item


def create_app(settings: Settings, users: Users, runner: Runner | None = None, hub: Hub | None = None,
               sessions: Sessions | None = None, throttle: Throttle | None = None, ip_throttle: Throttle | None = None) -> Starlette:
    web = settings.web
    sessions = sessions or Sessions(idle=web.session_hours * 3600.0)
    throttle = throttle or Throttle()                        # 계정별
    # 접속 IP 별 한도는 훨씬 느슨하게 둔다: 한 사무실이 같은 IP 를 쓰고, 터널 뒤에서는 모두가 한 IP 로 보일 수 있다
    ip_throttle = ip_throttle or Throttle(limit=throttle.limit * 4, lock=throttle.lock, max_lock=throttle.max_lock)
    conversations = Conversations()
    hub = hub or Hub(settings.servers)
    state: dict[str, Any] = {'llm': None, 'refreshed': 0.0}
    gate = asyncio.Semaphore(web.max_concurrent)

    async def default_runner(messages: list[dict[str, Any]], listener: agent.Listener) -> agent.Outcome:
        if state['llm'] is None:
            state['llm'] = Client(settings.llm)
        if not state['refreshed'] or (hub.unavailable and time.monotonic() - state['refreshed'] > 60):
            await hub.refresh()                      # 처음 한 번, 그리고 죽어 있던 서버가 있으면 1분마다 다시 붙어 본다
            state['refreshed'] = time.monotonic()
        return await agent.run(state['llm'], hub, [{'role': 'system', 'content': agent.system_prompt(hub)}] + messages,
                               settings.agent, listener)
    run = runner or default_runner

    def client_ip(request: Request) -> str:
        if web.trust_forwarded and request.headers.get('cf-connecting-ip'):
            return request.headers['cf-connecting-ip'][:64]
        return request.client.host if request.client else 'unknown'

    def is_https(request: Request) -> bool:
        return request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'

    def same_origin(request: Request) -> bool:
        origin = request.headers.get('origin')
        return bool(origin) and urlsplit(origin).netloc.lower() == (request.headers.get('host') or '').lower()

    def current(request: Request) -> Session | None:
        return sessions.get(request.cookies.get(COOKIE))

    def api_guard(request: Request, write: bool) -> Session | Response:
        session = current(request)
        if session is None:
            return JSONResponse({'error': '로그인이 필요합니다.'}, status_code=401)
        if write and not (same_origin(request) and request.headers.get(API_HEADER) == API_HEADER_VALUE):
            return JSONResponse({'error': '요청을 확인하지 못했습니다.'}, status_code=403)
        return session

    async def index(request: Request) -> Response:
        if current(request) is None:
            return RedirectResponse('/login', status_code=303)
        return Response((STATIC / 'index.html').read_bytes(), media_type='text/html; charset=utf-8')

    async def static(request: Request) -> Response:
        name = request.path_params['name']
        if name not in STATIC_FILES:                 # 정해 둔 파일만 내준다(경로 조작 여지를 없앤다)
            return Response('not found', status_code=404)
        return Response((STATIC / name).read_bytes(), media_type=STATIC_FILES[name], headers={'cache-control': 'no-cache'})

    async def login_page(request: Request) -> Response:
        if current(request) is not None:
            return RedirectResponse('/', status_code=303)
        message = LOGIN_ERRORS.get(request.query_params.get('error', ''), '')
        error = f'<p class="error" role="alert">{html.escape(message)}</p>' if message else ''
        return Response(LOGIN_PAGE.format(error=error), media_type='text/html; charset=utf-8')

    async def login(request: Request) -> Response:
        if not same_origin(request):
            return RedirectResponse('/login?error=origin', status_code=303)
        form = await request.form()
        name, password = str(form.get('username') or '').strip()[:64], str(form.get('password') or '')
        ip = client_ip(request)
        if throttle.locked_for(name) or ip_throttle.locked_for(ip):
            log.warning('login blocked (locked) name=%r ip=%s', name, ip)
            return RedirectResponse('/login?error=locked', status_code=303)
        if not users.verify(name, password):
            throttle.failure(name)
            ip_throttle.failure(ip)
            log.warning('login failed name=%r ip=%s', name, ip)
            return RedirectResponse('/login?error=1', status_code=303)
        throttle.success(name)
        log.info('login ok user=%s ip=%s', name, ip)
        response = RedirectResponse('/', status_code=303)
        response.set_cookie(COOKIE, sessions.create(name), max_age=int(sessions.lifetime), httponly=True, samesite='strict',
                            secure=is_https(request), path='/')
        return response

    async def logout(request: Request) -> Response:
        if not same_origin(request):
            return RedirectResponse('/', status_code=303)
        sessions.delete(request.cookies.get(COOKIE))
        response = RedirectResponse('/login', status_code=303)
        response.delete_cookie(COOKIE, path='/')
        return response

    async def me(request: Request) -> Response:
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        return JSONResponse({'name': session.user, 'servers': [{'name': server.name, 'ok': server.name not in hub.unavailable}
                                                              for server in hub.servers] if state['refreshed'] else [],
                             'max_question_chars': web.max_question_chars})

    async def conversation_list(request: Request) -> Response:
        session = api_guard(request, write=False)
        return session if isinstance(session, Response) else JSONResponse({'conversations': conversations.list(session.user)})

    async def conversation(request: Request) -> Response:
        session = api_guard(request, write=request.method == 'DELETE')
        if isinstance(session, Response):
            return session
        conversation_id = request.path_params['conversation_id']
        if request.method == 'DELETE':
            return JSONResponse({'deleted': conversations.delete(session.user, conversation_id)})
        item = conversations.get(session.user, conversation_id)     # 남의 대화는 id 를 알아도 볼 수 없다(계정별로 따로 둔다)
        return JSONResponse(item) if item else JSONResponse({'error': '없는 대화입니다.'}, status_code=404)

    async def chat(request: Request) -> Response:
        session = api_guard(request, write=True)
        if isinstance(session, Response):
            return session
        try:
            body = json.loads(await request.body() or b'{}')
            question = str(body.get('message') or '').strip()
            conversation_id = body.get('conversation_id')
        except (ValueError, AttributeError):
            return JSONResponse({'error': '요청 형식이 잘못되었습니다.'}, status_code=400)
        if not question or len(question) > web.max_question_chars:
            return JSONResponse({'error': f'질문은 1~{web.max_question_chars}자여야 합니다.'}, status_code=400)
        previous = conversations.get(session.user, conversation_id) if isinstance(conversation_id, str) else None
        if conversation_id and previous is None:
            return JSONResponse({'error': '없는 대화입니다.'}, status_code=404)
        if session.busy:
            return JSONResponse({'error': '앞선 질문에 답하는 중입니다.'}, status_code=409)
        session.busy = True
        history = (previous['messages'][-CONTEXT_MESSAGES:] if previous else []) + [{'role': 'user', 'content': question}]
        queue: asyncio.Queue = asyncio.Queue()

        async def work() -> None:
            try:
                if gate.locked():
                    await queue.put({'type': 'queued'})
                async with gate:
                    outcome = await run(history, queue.put)
                item = conversations.append(session.user, previous['id'] if previous else None, question, outcome.answer)
                await queue.put({'type': 'done', 'conversation_id': item['id'], 'title': item['title']})
            except LlmError as exc:
                await queue.put({'type': 'error', 'message': str(exc)})
            except Exception:
                log.exception('chat failed user=%s', session.user)
                await queue.put({'type': 'error', 'message': '답을 만드는 중 오류가 났습니다.'})
            finally:
                await queue.put(None)

        async def stream():
            task = asyncio.create_task(work())
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ': keep-alive\n\n'     # 터널·프록시가 조용한 연결을 끊지 않게
                        continue
                    if event is None:
                        break
                    yield f'data: {json.dumps(event, ensure_ascii=False)}\n\n'
            finally:
                session.busy = False
                if not task.done():                  # 브라우저가 떠나면 모델 호출도 멈춘다
                    task.cancel()
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'x-accel-buffering': 'no'})

    routes = [Route('/', index), Route('/static/{name}', static), Route('/login', login_page), Route('/login', login, methods=['POST']),
              Route('/logout', logout, methods=['POST']), Route('/api/me', me), Route('/api/conversations', conversation_list),
              Route('/api/conversations/{conversation_id}', conversation, methods=['GET', 'DELETE']),
              Route('/api/chat', chat, methods=['POST'])]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders),
                                                Middleware(TrustedHostMiddleware, allowed_hosts=list(web.allowed_hosts))])


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description='R&D 검색 웹 채팅 서버')
    parser.add_argument('--config', type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        settings = Settings.load(args.config)
        users = Users(list(settings.users))
    except ValueError as exc:
        sys.exit(f'설정 오류: {exc}')
    if not len(users):
        sys.exit('로그인 계정이 없습니다. config.toml 에 [[users]] 를 하나 이상 넣으세요(비밀번호 해시는 python -m chat.passwd).')
    if users.plaintext:
        log.warning('비밀번호를 평문으로 적은 계정이 있습니다: %s — python -m chat.passwd 로 해시를 만들어 바꾸기를 권합니다', ', '.join(users.plaintext))
    if settings.web.host not in ('127.0.0.1', 'localhost', '::1'):
        log.warning('[web] host 가 %s 입니다. 이 장비 밖에서 직접 붙을 수 있습니다(TLS 없이).', settings.web.host)
    uvicorn.run(create_app(settings, users), host=settings.web.host, port=settings.web.port, server_header=False,
                proxy_headers=False, log_level='info')


if __name__ == '__main__':
    main()
