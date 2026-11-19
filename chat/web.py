"""웹 채팅 서버:  python -m chat.web

로그인한 사람만 쓸 수 있다. 외부에 공개할 것을 전제로 다음을 지킨다.
- 기본은 127.0.0.1 에만 연다. 밖에서는 터널(Cloudflare 등)을 거쳐서만 들어온다. 내부망에 직접 열 때는
  설정의 tls_cert / tls_key 로 https 를 켠다.
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
import contextlib
import html
import json
import logging
from pathlib import Path
import secrets
import sys
import tempfile
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import agent, docs, editing, files as filestore, hwp
from .auth import Session, Sessions, Throttle, Users
from .llm import Client, LlmError
from .localtools import LocalTools
from .settings import Settings
from .sources import Sources, cited_numbers, renumber
from .tools import Hub

log = logging.getLogger('rnd-chat')
COOKIE = 'rnd_session'
API_HEADER, API_HEADER_VALUE = 'x-requested-with', 'rnd-chat'
STATIC = Path(__file__).resolve().parent / 'static'
_JS, _CSS, _FONT = 'text/javascript; charset=utf-8', 'text/css; charset=utf-8', 'font/woff2'
STATIC_FILES = {'app.js': _JS, 'theme.js': _JS, 'login.js': _JS, 'base.css': _CSS, 'login.css': _CSS, 'app.css': _CSS,
                'd2coding-400.woff2': _FONT, 'd2coding-700.woff2': _FONT, 'd2coding-400-all.woff2': _FONT, 'd2coding-700-all.woff2': _FONT}
MAX_CONVERSATIONS, MAX_MESSAGES, CONTEXT_MESSAGES = 50, 200, 20
MAX_ATTACHMENTS, EARLIER_PDF_TEXT, EARLIER_OUTLINE = 3, 8000, 5000
PAGE_WIDTHS = {'thumb': 420, 'full': 1500}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
LOGIN_ERRORS = {'1': '이름 또는 비밀번호가 맞지 않습니다.', 'locked': '로그인 시도가 많아 잠시 잠겼습니다. 잠시 뒤 다시 해 보세요.',
                'origin': '요청을 확인하지 못했습니다. 이 페이지에서 다시 로그인해 주세요.'}
LOGIN_ERROR_MARK = '<!--error-->'

Runner = Callable[[list[dict[str, Any]], agent.Listener, Any], Awaitable[agent.Outcome]]


class SecurityHeaders:
    """모든 응답에 보안 헤더를 붙이는 ASGI 미들웨어(스트리밍 응답도 그대로 흘려보낸다)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        forwarded = dict(scope['headers']).get(b'x-forwarded-proto') == b'https'       # 앞단(터널)이 https 를 받았다
        https = scope.get('scheme') == 'https' or forwarded

        async def wrapped(message):
            if message['type'] == 'http.response.start':
                headers = [(name, value) for name, value in message.get('headers', []) if name.lower() != b'server']
                present = {name.lower() for name, _ in headers}
                extra = {b'content-security-policy': CSP.encode(), b'x-content-type-options': b'nosniff', b'x-frame-options': b'DENY',
                         # no-referrer 로 두면 브라우저가 같은 사이트의 폼 전송에도 Origin: null 을 보내 로그인이 막힌다(실측)
                         b'referrer-policy': b'same-origin', b'permissions-policy': b'camera=(), microphone=(), geolocation=()',
                         b'cache-control': b'no-store'}
                if https:                # https 에서만 뜻이 있다(http 에서는 브라우저가 무시하고 경고를 낸다)
                    extra[b'cross-origin-opener-policy'] = b'same-origin'
                if forwarded:
                    # HSTS 는 공개 도메인(터널 뒤)에만 붙인다. 내부망 이름에 붙이면 그 이름의 다른 포트(http 서비스)까지
                    # 브라우저가 https 로 바꿔 버린다.
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

    def rename(self, user: str, conversation_id: str, title: str) -> bool:
        item = self.get(user, conversation_id)
        if item is not None:
            item['title'] = title
        return item is not None

    def replace_answer(self, user: str, conversation_id: str, answer: str, files: list | None = None, **extra: Any) -> dict[str, Any]:
        """마지막 답을 새 답으로 바꾼다(다시 생성)."""
        item = self._data[user][conversation_id]
        item['messages'][-1] = _message('assistant', answer, files, **extra)
        item['updated'] = time.time()
        self._data[user].move_to_end(conversation_id)
        return item

    def append(self, user: str, conversation_id: str | None, question: str, answer: str, attached: list | None = None,
               files: list | None = None, sources: list | None = None, **extra: Any) -> dict[str, Any]:
        mine = self._data.setdefault(user, OrderedDict())
        item = mine.get(conversation_id or '')
        if item is None:
            # sources: 이 대화에서 도구가 찾은 자료들(번호는 대화 안에서 이어진다). 답의 [번호] 가 이것을 가리킨다.
            item = {'id': secrets.token_urlsafe(9), 'title': ' '.join(question.split())[:40], 'messages': [], 'updated': 0.0,
                    'sources': sources if sources is not None else []}
            mine[item['id']] = item
            while len(mine) > MAX_CONVERSATIONS:
                mine.popitem(last=False)
        item['messages'] += [_message('user', question, attached), _message('assistant', answer, files, **extra)]
        del item['messages'][:-MAX_MESSAGES]
        item['updated'] = time.time()
        mine.move_to_end(item['id'])
        return item


def _latest_files(created: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """한 답에서 같은 문서를 여러 번 고쳤으면 마지막 것만 남긴다(중간본은 카드로 보여 주지 않는다)."""
    replaced = {meta.get('previous') for meta in created if meta.get('previous')}
    return [meta for meta in created if meta['id'] not in replaced]


def _message(role: str, content: str, files: list | None, **extra: Any) -> dict[str, Any]:
    """대화에 두는 메시지 하나. extra: 답에 딸린 기록(steps = 도구 사용 내역, seconds = 걸린 시간)."""
    message = {'role': role, 'content': content, **{key: value for key, value in extra.items() if value not in (None, [], {})}}
    if files:
        message['files'] = files
    return message


def _brief_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """도구 인자를 화면의 작업 기록에 남길 만큼만 줄인다(문서 내용처럼 긴 값은 건수만)."""
    brief: dict[str, Any] = {}
    for key, value in list(arguments.items())[:20]:
        if isinstance(value, str):
            brief[str(key)[:40]] = value[:160]
        elif isinstance(value, (int, float, bool)) or value is None:
            brief[str(key)[:40]] = value
        elif isinstance(value, list):
            brief[str(key)[:40]] = f'{len(value)}건'
        else:
            brief[str(key)[:40]] = '{…}'
    return brief


def create_app(settings: Settings, users: Users, runner: Runner | None = None, hub: Hub | None = None,
               sessions: Sessions | None = None, throttle: Throttle | None = None, ip_throttle: Throttle | None = None) -> Starlette:
    web = settings.web
    sessions = sessions or Sessions(idle=web.session_hours * 3600.0)
    throttle = throttle or Throttle()                        # 계정별
    # 접속 IP 별 한도는 훨씬 느슨하게 둔다: 한 사무실이 같은 IP 를 쓰고, 터널 뒤에서는 모두가 한 IP 로 보일 수 있다
    ip_throttle = ip_throttle or Throttle(limit=throttle.limit * 4, lock=throttle.lock, max_lock=throttle.max_lock)
    conversations = Conversations()
    store = filestore.FileStore(Path(settings.data_dir or tempfile.mkdtemp(prefix='rndchat-')) / 'files')
    previews = filestore.Previews(store)
    hwp.configure(settings.rhwp)
    if not hwp.available():
        log.warning('한글 문서 엔진(rhwp)을 찾지 못했습니다. HWP·HWPX 는 읽기만 되고 고칠 수 없습니다([documents] rhwp 에 경로를 적으세요).')
    hub = hub or Hub(settings.servers)
    state: dict[str, Any] = {'llm': None, 'refreshed': 0.0}
    gate = asyncio.Semaphore(web.max_concurrent)

    async def default_runner(messages: list[dict[str, Any]], listener: agent.Listener, local: Any = None) -> agent.Outcome:
        if state['llm'] is None:
            state['llm'] = Client(settings.llm)
        if not state['refreshed'] or (hub.unavailable and time.monotonic() - state['refreshed'] > 60):
            await hub.refresh()                      # 처음 한 번, 그리고 죽어 있던 서버가 있으면 1분마다 다시 붙어 본다
            state['refreshed'] = time.monotonic()
        system = agent.system_prompt(hub) + (getattr(local, 'system_note', '') or '')
        return await agent.run(state['llm'], hub, [{'role': 'system', 'content': system}] + messages, settings.agent, listener, local)
    run = runner or default_runner

    async def attachment_text(user: str, meta: dict[str, Any], current: bool) -> tuple[str, list[str]]:
        """첨부 하나를 모델에 설명하는 글과 쪽 그림(지금 질문일 때만). 고칠 수 있는 문서는 좌표가 적힌 개요를 준다."""
        found = store.get(user, meta['id'])
        if found is None:
            return f'[첨부했던 "{meta["name"]}" 은 더 이상 보관되어 있지 않습니다]', []
        kind, images, pages = found[0]['kind'], [], ''
        parsed = None
        try:
            parsed = await previews.parsed(user, found[0], found[1])
            pages = f' · 전체 {parsed["pages"]}쪽' + (f', 앞 {parsed["shown"]}쪽을 그림으로 첨부' if current else '')
            images = parsed['images'] if current else []
        except filestore.FileError:
            pages = ' · 쪽 그림은 만들지 못함'
        head = f'[첨부한 문서: {meta["name"]} · {kind.upper()} · file_id: {meta["id"]}{pages}]'
        if kind in editing.EDITABLE:
            try:
                outline = await asyncio.to_thread(editing.outline, found[1], kind)
                body = editing.outline_text(outline, limit=editing.OUTLINE_CHARS if current else EARLIER_OUTLINE)
                return (f'{head}\n이 문서는 edit_document 로 원본을 직접 고칠 수 있습니다. 개요(p = 문단 번호, 표의 r·c = 행·열, 모두 0 부터):\n{body}'), images
            except editing.EditError as exc:
                head += f'\n(이 문서는 지금 직접 고칠 수 없습니다: {exc})'
        elif kind == 'pdf':
            head += '\n(PDF 는 원본을 고칠 수 없습니다. 이 모양을 따라 만들려면 create_document 를 씁니다.)'
        text = (parsed or {}).get('text', '')
        body = text if current else text[:EARLIER_PDF_TEXT]
        return f'{head}\n추출한 글(배치는 그림을 기준으로 볼 것):\n{body or "(글자를 추출하지 못함 — 그림을 볼 것)"}', images

    async def model_message(user: str, message: dict[str, Any], current: bool) -> dict[str, Any]:
        """대화에 둔 메시지를 모델에 보낼 모양으로. 첨부는 지금 질문이면 쪽 그림까지, 지난 질문이면 글만 붙인다."""
        attached = message.get('files')
        if not attached:
            return {'role': message['role'], 'content': message['content']}
        if message['role'] != 'user':                    # 앞선 답에서 만든 파일: 이어서 고칠 수 있게 file_id 를 알려 준다
            made = ', '.join(f'{meta["name"]} (file_id: {meta["id"]})' for meta in attached)
            return {'role': message['role'], 'content': f'{message["content"]}\n\n[이 답에서 만든 파일: {made}]'}
        text, images = message['content'], []
        for meta in attached:
            described, pictures = await attachment_text(user, meta, current)
            text += '\n\n' + described
            images += [{'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image}'}} for image in pictures]
        return {'role': 'user', 'content': [{'type': 'text', 'text': text}] + images if images else text}

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
        path = STATIC / name
        stat = path.stat()
        # 글꼴은 바뀌지 않으니 오래 두고, 나머지는 매번 바뀌었는지만 묻게 한다(바뀌지 않았으면 본문 없이 304)
        headers = {'etag': f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"',
                   'cache-control': 'public, max-age=2592000' if STATIC_FILES[name] == _FONT else 'no-cache'}
        if request.headers.get('if-none-match') == headers['etag']:
            return Response(status_code=304, headers=headers)
        return Response(path.read_bytes(), media_type=STATIC_FILES[name], headers=headers)

    async def login_page(request: Request) -> Response:
        if current(request) is not None:
            return RedirectResponse('/', status_code=303)
        message = LOGIN_ERRORS.get(request.query_params.get('error', ''), '')
        error = f'<p class="alert" role="alert">{html.escape(message)}</p>' if message else ''
        page = (STATIC / 'login.html').read_text(encoding='utf-8').replace(LOGIN_ERROR_MARK, error)
        return Response(page, media_type='text/html; charset=utf-8')

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
                             'model': settings.llm.model, 'max_question_chars': web.max_question_chars})

    async def conversation_list(request: Request) -> Response:
        session = api_guard(request, write=False)
        return session if isinstance(session, Response) else JSONResponse({'conversations': conversations.list(session.user)})

    async def conversation(request: Request) -> Response:
        session = api_guard(request, write=request.method != 'GET')
        if isinstance(session, Response):
            return session
        conversation_id = request.path_params['conversation_id']
        if request.method == 'DELETE':
            return JSONResponse({'deleted': conversations.delete(session.user, conversation_id)})
        if request.method == 'PATCH':
            try:
                title = ' '.join(str(json.loads(await request.body()).get('title') or '').split())[:80]
            except (ValueError, AttributeError):
                title = ''
            if not title:
                return JSONResponse({'error': '제목은 1~80자여야 합니다.'}, status_code=400)
            if not conversations.rename(session.user, conversation_id, title):
                return JSONResponse({'error': '없는 대화입니다.'}, status_code=404)
            return JSONResponse({'id': conversation_id, 'title': title})
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
            regenerate = body.get('regenerate') is True
            wanted = body.get('attachments') or []
        except (ValueError, AttributeError):
            return JSONResponse({'error': '요청 형식이 잘못되었습니다.'}, status_code=400)
        previous = conversations.get(session.user, conversation_id) if isinstance(conversation_id, str) else None
        if conversation_id and previous is None:
            return JSONResponse({'error': '없는 대화입니다.'}, status_code=404)
        earlier = previous['messages'] if previous else []
        if regenerate:                               # 마지막 질문을 다시 묻고, 그 답을 새 답으로 바꾼다
            if len(earlier) < 2:
                return JSONResponse({'error': '다시 만들 답이 없습니다.'}, status_code=400)
            asked, earlier = earlier[-2], earlier[:-2]
            question, attached = asked['content'], asked.get('files') or []
        else:
            if not question or len(question) > web.max_question_chars:
                return JSONResponse({'error': f'질문은 1~{web.max_question_chars}자여야 합니다.'}, status_code=400)
            if not isinstance(wanted, list) or len(wanted) > MAX_ATTACHMENTS or not all(isinstance(item, str) for item in wanted):
                return JSONResponse({'error': f'첨부는 {MAX_ATTACHMENTS}개까지입니다.'}, status_code=400)
            attached = []
            for file_id in wanted:
                found = store.get(session.user, file_id)
                if found is None or found[0].get('origin') != 'upload':
                    return JSONResponse({'error': '첨부한 파일을 찾을 수 없습니다. 다시 올려 주세요.'}, status_code=400)
                attached.append(found[0])
        if session.busy:
            return JSONResponse({'error': '앞선 질문에 답하는 중입니다.'}, status_code=409)
        session.busy = True
        queue: asyncio.Queue = asyncio.Queue()
        created: list[dict[str, Any]] = []

        steps: list[dict[str, Any]] = []                 # 도구 사용 내역(대화를 다시 열어도 볼 수 있게 답과 함께 둔다)

        async def listen(event: dict[str, Any]) -> None:
            if event['type'] == 'file':
                created.append(event['file'])
            elif event['type'] == 'tool_call' and len(steps) < 60:
                steps.append({'name': event['name'], 'args': _brief_arguments(event['arguments'])})
            elif event['type'] == 'tool_result' and steps:
                steps[-1].update(is_error=event['is_error'], summary=event.get('summary') or {})
            await queue.put(event)

        async def work() -> None:
            try:
                history = [await model_message(session.user, message, False) for message in earlier[-CONTEXT_MESSAGES:]]
                history.append(await model_message(session.user, _message('user', question, attached), True))
                if gate.locked():
                    await queue.put({'type': 'queued'})
                sources = Sources(previous['sources'] if previous else [])
                local = LocalTools(store, session.user, previews, sources)
                # 앞선 답에서 쓰인 출처 번호를 알려 줘, 이어지는 질문에서도 같은 번호로 가리키게 한다
                local.system_note = sources.note(set().union(*(cited_numbers(message['content']) for message in earlier if message['role'] == 'assistant')))
                async with gate:
                    started = time.monotonic()
                    outcome = await run(history, listen, local)
                final, record = _latest_files(created), {'steps': steps, 'seconds': round(time.monotonic() - started, 1)}
                if regenerate:
                    item = conversations.replace_answer(session.user, previous['id'], outcome.answer, final, **record)
                else:
                    item = conversations.append(session.user, previous['id'] if previous else None, question, outcome.answer, attached, final,
                                                sources.items, **record)
                await queue.put({'type': 'done', 'conversation_id': item['id'], 'title': item['title'], 'index': len(item['messages']) - 1})
            except LlmError as exc:
                await queue.put({'type': 'error', 'message': str(exc)})
            except (filestore.FileError, editing.EditError) as exc:
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

    def download(name: str, kind: str, data: bytes) -> Response:
        # 항상 내려받기로만 준다(브라우저가 열어서 실행하지 않게). 이름은 한글이 깨지지 않게 RFC 5987 로 적는다.
        return Response(data, media_type=filestore.MIME[kind],
                        headers={'content-disposition': f"attachment; filename*=UTF-8''{quote(name)}"})

    async def upload(request: Request) -> Response:
        session = api_guard(request, write=True)
        if isinstance(session, Response):
            return session
        try:
            declared = int(request.headers.get('content-length') or -1)
        except ValueError:
            declared = -1
        if declared < 0:
            return JSONResponse({'error': '파일 크기를 알 수 없는 요청입니다.'}, status_code=411)
        if declared > filestore.MAX_UPLOAD + 64 * 1024:              # 본문을 읽기 전에 거른다
            return JSONResponse({'error': f'파일이 너무 큽니다({filestore.MAX_UPLOAD // 1024 // 1024}MB 이하).'}, status_code=413)
        try:
            form = await request.form(max_files=1, max_fields=4)
            item = form.get('file')
            data = await item.read(filestore.MAX_UPLOAD + 1) if hasattr(item, 'read') else None
        except Exception:
            data = None
        if not data:
            return JSONResponse({'error': '올린 파일을 읽지 못했습니다.'}, status_code=400)
        try:
            kind = filestore.sniff(data)
        except filestore.FileError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        if kind is None:
            return JSONResponse({'error': f'{filestore.ACCEPTED} 파일만 첨부할 수 있습니다.'}, status_code=400)
        # 원본을 그대로 둔다(고칠 때 이 원본을 고친다). 쪽 그림은 여기서 한 번 만들어 둔다.
        meta = store.save(session.user, docs.safe_filename(item.filename, kind, '첨부'), data, kind, origin='upload')
        extra: dict[str, Any] = {'editable': False}
        try:
            parsed = await previews.parsed(session.user, meta, data)
            extra.update(pages=parsed['pages'], shown=parsed['shown'])
        except filestore.FileError as exc:
            if kind == 'pdf':                            # PDF 는 쪽을 못 그리면 쓸 수 없다
                store.delete(session.user, meta['id'])
                return JSONResponse({'error': str(exc)}, status_code=400)
            extra['preview_error'] = str(exc)
        if kind in editing.EDITABLE:
            try:
                outline = await asyncio.to_thread(editing.outline, data, kind)
                extra.update(editable=True, tables=len(outline['tables']), fields=len(outline.get('fields') or []))
            except editing.EditError as exc:
                extra['edit_error'] = str(exc)
                if 'preview_error' in extra:             # 볼 수도 고칠 수도 없는 파일
                    store.delete(session.user, meta['id'])
                    return JSONResponse({'error': str(exc)}, status_code=400)
        meta = store.update(session.user, meta['id'], **extra) or meta
        log.info('upload user=%s name=%r kind=%s bytes=%d pages=%s editable=%s', session.user, meta['name'], kind, meta['size'],
                 meta.get('pages'), meta.get('editable'))
        return JSONResponse(meta)

    async def file(request: Request) -> Response:
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        found = store.get(session.user, request.path_params['file_id'])     # 남의 파일은 id 를 알아도 못 받는다
        if found is None:
            return JSONResponse({'error': '없는 파일입니다.'}, status_code=404)
        return download(found[0]['name'], found[0]['kind'], found[1])

    async def file_info(request: Request) -> Response:
        """파일 정보(쪽 수 포함). 미리보기 창이 연다. 쪽 수를 아직 모르면 여기서 미리보기를 만든다."""
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        found = store.get(session.user, request.path_params['file_id'])
        if found is None:
            return JSONResponse({'error': '없는 파일입니다.'}, status_code=404)
        meta = found[0]
        if not meta.get('pages'):
            try:
                pages = await asyncio.to_thread(filestore.pdf_pages, await previews.pdf(session.user, meta, found[1]))
                meta = store.update(session.user, meta['id'], pages=pages) or meta
            except filestore.FileError as exc:
                return JSONResponse({**meta, 'preview_error': str(exc)})
        return JSONResponse(meta)

    async def file_page(request: Request) -> Response:
        """파일의 한 쪽을 그림으로(미리보기)."""
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        found = store.get(session.user, request.path_params['file_id'])
        width = PAGE_WIDTHS.get(request.query_params.get('size', 'full'))
        try:
            page = int(request.path_params['page'])
        except ValueError:
            page = 0
        if found is None or width is None or not 1 <= page <= filestore.MAX_PREVIEW_PAGES:
            return JSONResponse({'error': '없는 쪽입니다.'}, status_code=404)
        try:
            image = await previews.page(session.user, found[0], page, width)
        except filestore.FileError as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)
        return Response(image, media_type='image/jpeg', headers={'cache-control': 'private, max-age=3600'})

    async def export(request: Request) -> Response:
        """대화에 있는 답 하나를 문서(docx)나 엑셀(xlsx, 답 안의 표들)로 내려받는다."""
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        item = conversations.get(session.user, request.path_params['conversation_id'])
        kind = request.query_params.get('format', '')
        try:
            message = item['messages'][int(request.query_params.get('index', ''))]
        except (TypeError, ValueError, IndexError):
            message = None
        if item is None or message is None or message['role'] != 'assistant' or kind not in ('docx', 'xlsx'):
            return JSONResponse({'error': '내보낼 답을 찾지 못했습니다.'}, status_code=404)
        try:
            if kind == 'docx':
                content, cited = renumber(message['content'], item.get('sources') or [])     # 번호를 화면과 같게(처음 나온 순서)
                blocks = docs.markdown_blocks(content) or [{'type': 'paragraph', 'text': ''}]
                if cited:                                # 답의 [번호] 가 가리키는 자료를 문서 끝에 적는다
                    blocks += [{'type': 'heading', 'text': '출처', 'level': 2},
                               {'type': 'bullets', 'items': [f'[{number}] {source["title"]} — {source["url"]}' for number, source in cited]}]
                data = docs.build_docx(blocks, item['title'])
            else:
                tables = docs.markdown_tables(renumber(message['content'], item.get('sources') or [])[0])
                if not tables:
                    return JSONResponse({'error': '이 답에는 표가 없습니다.'}, status_code=400)
                data = docs.build_xlsx(tables)
        except docs.DocumentError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        return download(docs.safe_filename(item['title'], kind), kind, data)

    routes = [Route('/api/files', upload, methods=['POST']), Route('/api/files/{file_id}', file),
              Route('/api/files/{file_id}/info', file_info), Route('/api/files/{file_id}/pages/{page}', file_page),
              Route('/api/conversations/{conversation_id}/export', export), Route('/', index), Route('/static/{name}', static), Route('/login', login_page), Route('/login', login, methods=['POST']),
              Route('/logout', logout, methods=['POST']), Route('/api/me', me), Route('/api/conversations', conversation_list),
              Route('/api/conversations/{conversation_id}', conversation, methods=['GET', 'DELETE', 'PATCH']),
              Route('/api/chat', chat, methods=['POST'])]
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async def warm() -> None:                    # 첫 질문 전에 자료 서버 상태를 알아 둔다(실패해도 서버는 뜬다)
            try:
                await hub.refresh()
                state['refreshed'] = time.monotonic()
            except Exception:
                log.exception('MCP 서버 목록을 받지 못했습니다')
        task = asyncio.create_task(warm()) if runner is None else None
        yield
        if task and not task.done():
            task.cancel()
        if state['llm'] is not None:
            await state['llm'].close()

    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityHeaders),
                                                Middleware(TrustedHostMiddleware, allowed_hosts=list(web.allowed_hosts))])


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description='R&D 검색 웹 채팅 서버')
    parser.add_argument('--config', type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    for noisy in ('httpx', 'mcp'):                   # 자료 서버 호출마다 한 줄씩 남기지 않는다
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = Settings.load(args.config)
        users = Users(list(settings.users))
    except ValueError as exc:
        sys.exit(f'설정 오류: {exc}')
    if not len(users):
        sys.exit('로그인 계정이 없습니다. config.toml 에 [[users]] 를 하나 이상 넣으세요(비밀번호 해시는 python -m chat.passwd).')
    if users.plaintext:
        log.warning('비밀번호를 평문으로 적은 계정이 있습니다: %s — python -m chat.passwd 로 해시를 만들어 바꾸기를 권합니다', ', '.join(users.plaintext))
    tls = {'ssl_certfile': settings.web.tls_cert, 'ssl_keyfile': settings.web.tls_key} if settings.web.tls_cert else {}
    if settings.web.host not in ('127.0.0.1', 'localhost', '::1') and not tls:
        log.warning('[web] host 가 %s 인데 https 가 꺼져 있습니다. 같은 망의 다른 기기에서 암호화 없이(http) 접속하게 됩니다. '
                    'tls_cert / tls_key 를 설정하세요(python -m chat.tlscert).', settings.web.host)
    log.info('%s://%s:%s 에서 엽니다', 'https' if tls else 'http', settings.web.host, settings.web.port)
    uvicorn.run(create_app(settings, users), host=settings.web.host, port=settings.web.port, server_header=False,
                proxy_headers=False, log_level='info', **tls)


if __name__ == '__main__':
    main()
