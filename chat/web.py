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
from datetime import datetime, timedelta
import hmac
import html
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import agent, brief, docs, editing, files as filestore, hwp, mailer, storage, sweep, tasks
from .auth import Session, Sessions, Throttle, Users, hash_password
from .llm import Client, LlmError
from .localtools import NAMES as LOCAL_TOOLS, LocalTools
from .settings import Settings
from .sources import Sources, cited_numbers, renumber
from .tools import Hub

log = logging.getLogger('rnd-chat')
COOKIE = 'rnd_session'
API_HEADER, API_HEADER_VALUE = 'x-requested-with', 'rnd-chat'
STATIC = Path(__file__).resolve().parent / 'static'
_JS, _CSS, _FONT = 'text/javascript; charset=utf-8', 'text/css; charset=utf-8', 'font/woff2'
STATIC_FILES = {'app.js': _JS, 'theme.js': _JS, 'login.js': _JS, 'backdrop.js': _JS, 'signup.js': _JS, 'base.css': _CSS, 'login.css': _CSS, 'app.css': _CSS,
                'd2coding-400.woff2': _FONT, 'd2coding-700.woff2': _FONT, 'd2coding-400-all.woff2': _FONT, 'd2coding-700-all.woff2': _FONT}
MAX_CONVERSATIONS, MAX_MESSAGES, CONTEXT_MESSAGES = 50, 200, 20
MAX_ATTACHMENTS, EARLIER_PDF_TEXT, EARLIER_OUTLINE = 3, 8000, 5000
ASSISTANT_SOURCES = 900          # 비서 대화가 들고 있는 출처 수(답이 가리키는 것만 남기며 이어 쓴다)
MAX_SUGGESTIONS = 3              # 대화 기록에서 뽑아 권하는 일을 한 번에 몇 개까지 보여 줄지
PAGE_WIDTHS = {'thumb': 420, 'full': 1500}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
LOGIN_ERRORS = {'1': '아이디 또는 비밀번호가 맞지 않습니다.', 'locked': '로그인 시도가 많아 잠시 잠겼습니다. 잠시 뒤 다시 해 보세요.',
                'origin': '요청을 확인하지 못했습니다. 이 페이지에서 다시 로그인해 주세요.'}
LOGIN_ERROR_MARK = '<!--error-->'
SIGNUP_MARK = '<!--signup-->'
SIGNUP_LINK = '<a class="alt" href="/signup">회원가입</a>'
SIGNUP_NAME = re.compile(r'[A-Za-z0-9가-힣][A-Za-z0-9가-힣._-]{1,31}')
SIGNUP_EMAIL = re.compile(r'[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+')
MIN_PASSWORD = 8

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
                         b'cache-control': b'no-store, no-transform'}       # no-transform: 앞단(Cloudflare)이 화면에 스크립트를 끼워 넣지 못하게
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
    """계정별 대화. database 를 주면 파일에도 남겨 서버를 다시 띄워도 그대로 있다(없으면 메모리에만 둔다).
    모델에는 사용자·조수의 글만 넘기고 도구 왕복은 남기지 않는다."""

    def __init__(self, database: Any = None):
        self._database = database
        self._data: dict[str, OrderedDict[str, dict[str, Any]]] = database.conversations() if database else {}

    def _keep(self, user: str, item: dict[str, Any]) -> None:
        if self._database:
            self._database.save(user, item)

    def list(self, user: str) -> list[dict[str, Any]]:
        return [{'id': item['id'], 'title': item['title'], 'updated': item['updated'], **({'kind': item['kind']} if item.get('kind') else {})}
                for item in reversed(self._data.get(user, {}).values())]

    def items(self, user: str) -> list[dict[str, Any]]:
        return list(self._data.get(user, {}).values())

    def search(self, user: str, query: str, limit: int = 30) -> list[dict[str, Any]]:
        """제목과 주고받은 글에서 낱말을 모두 가진 대화(최근 것부터). 낱말이 나온 곳 둘레의 글을 조금 붙여 준다."""
        words = query.lower().split()[:8]
        found: list[dict[str, Any]] = []
        for item in reversed(self._data.get(user, {}).values()) if words else ():
            texts = [' '.join(_MARKUP.sub(' ', str(message.get('content') or '')).split()) for index, message in enumerate(item['messages'])
                     if not (item.get('kind') == 'brief' and index == 0) and not message.get('auto')]      # 사용자가 쓰지 않은 글(실행 지시)은 찾지 않는다
            if not all(word in '\n'.join([item['title'], *texts]).lower() for word in words):
                continue
            entry = {'id': item['id'], 'title': item['title'], 'updated': item['updated'], **({'kind': item['kind']} if item.get('kind') else {})}
            for text in texts:
                places = [text.lower().find(word) for word in words if word in text.lower()]
                if places and not item['title'].startswith(text[:40]):          # 제목이 된 첫 질문은 되풀이해 보이지 않는다
                    start = max(0, min(places) - 40)
                    entry['snippet'] = ('…' if start else '') + text[start:start + 150] + ('…' if start + 150 < len(text) else '')
                    break
            found.append(entry)
            if len(found) >= limit:
                break
        return found

    def assistant(self, user: str) -> dict[str, Any]:
        """그 계정의 비서 대화(하나뿐이다). 없으면 만든다."""
        mine = self._data.setdefault(user, OrderedDict())
        found = next((item for item in mine.values() if item.get('kind') == 'assistant'), None)
        if found is None:
            found = {'id': secrets.token_urlsafe(9), 'title': '비서', 'messages': [], 'updated': time.time(), 'sources': [], 'kind': 'assistant'}
            mine[found['id']] = found
            self._keep(user, found)
        return found

    def tidy(self, user: str, conversation_id: str) -> None:
        """오래 이어지는 대화에서, 남아 있는 답이 가리키지 않는 출처를 덜어 낸다."""
        item = self.get(user, conversation_id)
        if item is not None:
            cited = set().union(*(cited_numbers(message['content']) for message in item['messages'] if message['role'] == 'assistant'))
            Sources(item['sources']).prune(cited)
            self._keep(user, item)

    def users(self) -> list[str]:
        return list(self._data)

    def purge(self, user: str) -> None:
        """그 계정의 대화를 메모리에서 지운다(파일 쪽은 Database.forget 이 지운다)."""
        self._data.pop(user, None)

    def files(self, user: str) -> dict[str, dict[str, str]]:
        """대화에 실린 파일이 어느 대화에 있는지(파일 id → 그 대화의 id 와 제목)."""
        where: dict[str, dict[str, str]] = {}
        for item in self._data.get(user, {}).values():
            for message in item['messages']:
                for meta in message.get('files') or []:
                    where[meta['id']] = {'id': item['id'], 'title': item['title']}
        return where

    def drop_file(self, user: str, file_id: str) -> None:
        """지운 파일을 대화에서도 뺀다(열리지 않는 카드가 남지 않게, 모델에도 없는 file_id 를 알려 주지 않게)."""
        for item in self._data.get(user, {}).values():
            changed = False
            for message in item['messages']:
                kept = [meta for meta in message.get('files') or [] if meta.get('id') != file_id]
                if len(kept) != len(message.get('files') or []):
                    changed = True
                    if kept:
                        message['files'] = kept
                    else:
                        del message['files']
            if changed:
                self._keep(user, item)

    def mark(self, user: str, conversation_id: str, **fields: Any) -> None:
        """대화에 딸린 값(종류, 제목)을 바꾼다."""
        item = self.get(user, conversation_id)
        if item is not None:
            item.update(fields)
            self._keep(user, item)

    def get(self, user: str, conversation_id: str) -> dict[str, Any] | None:
        return self._data.get(user, {}).get(conversation_id)

    def delete(self, user: str, conversation_id: str) -> bool:
        found = self._data.get(user, {}).pop(conversation_id, None) is not None
        if found and self._database:
            self._database.delete(user, conversation_id)
        return found

    def rename(self, user: str, conversation_id: str, title: str) -> bool:
        item = self.get(user, conversation_id)
        if item is not None:
            item['title'] = title
            self._keep(user, item)
        return item is not None

    def replace_answer(self, user: str, conversation_id: str, answer: str, files: list | None = None, **extra: Any) -> dict[str, Any]:
        """마지막 답을 새 답으로 바꾼다(다시 생성)."""
        item = self._data[user][conversation_id]
        item['messages'][-1] = _message('assistant', answer, files, **extra)
        item['updated'] = time.time()
        self._data[user].move_to_end(conversation_id)
        self._keep(user, item)
        return item

    def append(self, user: str, conversation_id: str | None, question: str, answer: str, attached: list | None = None,
               files: list | None = None, sources: list | None = None, auto: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        """질문과 답 한 쌍을 잇는다. auto: 사용자가 쓴 질문이 아니라 맡긴 일의 실행 지시일 때 그 일의 정보(화면은 말풍선 대신 이름표로 보인다)."""
        mine = self._data.setdefault(user, OrderedDict())
        item = mine.get(conversation_id or '')
        if item is None:
            # sources: 이 대화에서 도구가 찾은 자료들(번호는 대화 안에서 이어진다). 답의 [번호] 가 이것을 가리킨다.
            item = {'id': secrets.token_urlsafe(9), 'title': ' '.join(question.split())[:40], 'messages': [], 'updated': 0.0,
                    'sources': sources if sources is not None else []}
            mine[item['id']] = item
            while len(mine) > MAX_CONVERSATIONS:
                dropped = next(key for key, kept in mine.items() if kept.get('kind') != 'assistant')      # 비서 대화는 밀려나지 않는다
                del mine[dropped]
                if self._database:
                    self._database.delete(user, dropped)
        item['messages'] += [_message('user', question, attached, auto=auto), _message('assistant', answer, files, **extra)]
        del item['messages'][:-MAX_MESSAGES]
        item['updated'] = time.time()
        mine.move_to_end(item['id'])
        self._keep(user, item)
        return item


FILE_FIELDS = ('id', 'name', 'kind', 'size', 'created', 'origin', 'pages', 'applied', 'failed')     # 문서 목록에 싣는 것
_MARKUP = re.compile(r'[*#|`>\[\]]+|-{3,}')           # 검색 결과에 붙이는 글에서는 마크다운 기호를 뺀다


def _refresh_report(path: str) -> dict[str, Any] | None:
    """아침 자료 갱신의 결과 파일을 화면에 보일 만큼만 간추린다(기록의 원문에는 경로가 섞여 있어 그대로 내보내지 않는다)."""
    try:
        status = json.loads(Path(path).read_text(encoding='utf-8')) if path else None
    except (OSError, ValueError):
        status = None
    if not isinstance(status, dict):
        return None
    steps = []
    for step in status.get('steps') or []:
        if not isinstance(step, dict):
            continue
        last, note = str(step.get('last') or ''), ''
        counted = re.search(r'활성 (\d+)건, 신규 (\d+)건, 목록 제외 (\d+)건', last)
        indexed = re.search(r'"notices": (\d+)', last)
        if counted:
            note = f'{int(counted[1]):,}건 · 새로 {int(counted[2]):,} · 내림 {int(counted[3]):,}'
        elif indexed:
            note = f'{int(indexed[1]):,}건'
        elif '이미 수집이 실행 중' in last:
            note = '다른 수집이 돌고 있어 건너뜀'
        steps.append({'name': str(step.get('name') or '')[:40], 'ok': step.get('ok') is True, 'note': note})
    return {'started': str(status.get('started') or '')[:19], 'finished': str(status.get('finished') or '')[:19], 'steps': steps}


def _masked(address: str) -> str:
    name, _, domain = address.partition('@')
    return f'{name[:2]}***@{domain}' if domain else ''


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
        elif isinstance(value, dict):               # 검색 조건처럼 한 겹 안에 든 값은 풀어서 남긴다
            brief[str(key)[:40]] = ', '.join(f'{name}={inner}' for name, inner in value.items() if isinstance(inner, (str, int, float, bool)))[:160]
        else:
            brief[str(key)[:40]] = '{…}'
    return brief


def create_app(settings: Settings, users: Users, runner: Runner | None = None, hub: Hub | None = None,
               sessions: Sessions | None = None, throttle: Throttle | None = None, ip_throttle: Throttle | None = None) -> Starlette:
    web = settings.web
    throttle = throttle or Throttle()                        # 계정별
    # 접속 IP 별 한도는 훨씬 느슨하게 둔다: 한 사무실이 같은 IP 를 쓰고, 터널 뒤에서는 모두가 한 IP 로 보일 수 있다
    ip_throttle = ip_throttle or Throttle(limit=throttle.limit * 4, lock=throttle.lock, max_lock=throttle.max_lock)
    # 자료 폴더가 정해져 있으면 대화를 파일에 남기고, 대화가 가리키는 파일(올린 것·만든 것)도 다시 띄울 때 지우지 않는다
    database = storage.Database(Path(settings.data_dir) / 'chat.sqlite3') if settings.data_dir else None
    signup = settings.signup
    signup_open = signup.enabled and database is not None       # 신청을 남길 곳이 있어야 받는다
    for account in (database.accounts('active') if database is not None else []):       # 가입 신청이 승인된 계정들
        if not users.add(account['name'], account['password_hash']):
            log.warning('가입으로 등록된 계정 %r 은 설정의 계정과 이름이 겹쳐 쓰지 않습니다', account['name'])
    # 세션도 거기에 남겨, 서버를 다시 띄워도 로그인이 풀리지 않게 한다(비밀번호가 바뀌었거나 없어진 계정의 세션은 버린다)
    sessions = sessions or Sessions(idle=web.session_hours * 3600.0, store=database,
                                    valid=lambda user, stamp: bool(stamp) and hmac.compare_digest(stamp, users.stamp(user)))
    conversations = Conversations(database)
    store = filestore.FileStore(Path(settings.data_dir or tempfile.mkdtemp(prefix='rndchat-')) / 'files', clean=database is None)
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
        try:
            outcome = await agent.run(state['llm'], hub, [{'role': 'system', 'content': system}] + messages, settings.agent, listener, local)
        except LlmError:
            state['model_ok'] = False                # 도구 상태 화면에 보인다
            raise
        state['model_ok'] = True
        return outcome
    run = runner or default_runner

    # ── 비서: 맡긴 일을 정해진 때에 해서 비서 대화에 올린다 ─────────────────────────
    briefing = settings.briefing
    desk_on = briefing.enabled and database is not None          # 맡긴 일을 남길 곳이 있어야 한다
    emails = {str(entry.get('name')): str(entry.get('email') or '').strip() for entry in settings.users if str(entry.get('email') or '').strip()}
    for account in (database.accounts('active') if database is not None else []):
        emails.setdefault(account['name'], account['email'])
    running: set[tuple[str, str]] = set()                        # 지금 하고 있는 일 (계정, 일 id)

    def mail_ok(user: str) -> bool:
        return bool(emails.get(user) and settings.mail.username)

    def profile_of(user: str) -> dict[str, Any]:
        """계정에 딸린 것: notes(비서의 메모), suggestions(권하는 일), muted(권하지 말라고 한 주제), suggested(마지막으로 권할 것을 찾은 날)."""
        data = database.profile(user) if database is not None else {}
        data.pop('updated', None)
        return data

    def keep_profile(user: str, **changes: Any) -> dict[str, Any]:
        data = {**profile_of(user), **changes}
        database.save_profile(user, data, time.time())
        return data

    def notes_of(user: str) -> list[dict[str, Any]]:
        return [entry for entry in profile_of(user).get('notes') or [] if isinstance(entry, dict) and entry.get('id') and entry.get('text')]

    def desk_for(user: str) -> tasks.Desk:
        return tasks.Desk(lambda: database.tasks(user), lambda task: database.save_task(user, task), lambda task_id: database.delete_task(user, task_id),
                          briefing.max_tasks, mail_ok(user), briefing.time, notes=lambda: notes_of(user), keep_notes=lambda notes: keep_profile(user, notes=notes), earliest=briefing.refresh_time)

    if database is not None:                                     # 예전 아침 브리핑의 주제를 맡긴 일 하나로 옮긴다(한 번만)
        for owner, profile in database.profiles().items():
            if profile.get('moved'):
                continue
            moved = tasks.from_interests(profile.get('interests') or [], briefing.time, bool(emails.get(owner)), bool(profile.get('paused')))
            if moved and not database.tasks(owner):
                moved['last_ok'] = database.last_done(owner, '9999-12-31')
                database.save_task(owner, moved)
                log.info('brief topics moved to a task user=%s', owner)
            database.save_profile(owner, {**profile, 'moved': True}, time.time())

    async def run_task(user: str, task_id: str, day: str, force: bool = False) -> dict[str, Any] | None:
        """맡긴 일 하나를 한다: 찾아서 쓰기 → (알릴 것이 있으면) 비서 대화에 올리기 → 메일. force: 화면에서 지금 하라고 한 것(새 것이 없어도 올린다)."""
        def stored() -> dict[str, Any] | None:
            return next((task for task in database.tasks(user) if task['id'] == task_id), None)
        task = stored()
        if task is None or (user, task_id) in running:
            return None
        running.add((user, task_id))
        earlier = task.get('last') or {}
        attempts = int(earlier.get('attempts') or 0) + 1 if earlier.get('day') == day else 1
        database.save_task(user, {**task, 'last': {'day': day, 'status': 'running', 'at': time.time(), 'note': '', 'attempts': attempts}})

        def finish(status: str, note: str = '', **more: Any) -> None:
            fresh = stored()                                     # 하는 동안 사용자가 고치거나 지웠을 수 있다
            if fresh is not None:
                database.save_task(user, {**fresh, **more, 'last': {'day': day, 'status': status, 'at': time.time(), 'note': note[:300], 'attempts': attempts}})
        created: list[dict[str, Any]] = []
        try:
            item = conversations.assistant(user)
            steps: list[dict[str, Any]] = []

            async def listen(event: dict[str, Any]) -> None:
                if event['type'] == 'file':
                    created.append(event['file'])
                elif event['type'] == 'tool_call' and len(steps) < 60:
                    steps.append({'name': event['name'], 'args': _brief_arguments(event['arguments'])})
                elif event['type'] == 'tool_result' and steps:
                    steps[-1].update(is_error=event['is_error'], summary=event.get('summary') or {})
            sources = Sources(item['sources'], limit=ASSISTANT_SOURCES)
            local = LocalTools(store, user, previews, sources)
            order = tasks.instruction(task, day, str(task.get('last_ok') or ''), notes_of(user))
            async with gate:
                started = time.monotonic()
                outcome = await asyncio.wait_for(run([{'role': 'user', 'content': order}], listen, local), timeout=1800)
            answer = outcome.answer.strip()
            if not answer:
                raise RuntimeError('결과 글이 비어 있습니다')
            known = {source['n']: source['url'] for source in sources.items}
            cited = [known[number] for number in sorted(cited_numbers(answer)) if number in known]
            kept = {'last_ok': day, 'seen': tasks.remember(task, cited)}
            if not force and tasks.nothing_new(task, answer, cited):
                for meta in created:                             # 올리지 않는 결과가 만든 파일은 남기지 않는다
                    store.delete(user, meta['id'])
                conversations.tidy(user, item['id'])
                finish('quiet', **kept)
                log.info('task quiet user=%s task=%r', user, task['title'])
                return stored()
            conversations.append(user, item['id'], order, answer, None, _latest_files(created), auto={'task': task_id, 'title': task['title'], 'day': day},
                                 steps=steps, seconds=round(time.monotonic() - started, 1))
            conversations.tidy(user, item['id'])
            note = ''
            if task.get('mail') and mail_ok(user):
                try:
                    label = f'{task["title"]} · {int(day[5:7])}월 {int(day[8:10])}일'
                    page, plain = mailer.render(answer, sources.items, label, briefing.app_url)
                    await asyncio.to_thread(mailer.send, settings.mail, emails[user], label, page, plain)
                    note = 'mailed'
                except Exception as exc:                         # 메일이 안 가도 결과는 대화에 남는다
                    note = f'메일을 보내지 못했습니다: {type(exc).__name__}'
                    log.warning('task mail failed user=%s error=%s', user, type(exc).__name__)
            finish('done', note, **kept)
            log.info('task done user=%s task=%r steps=%d mailed=%s', user, task['title'], len(steps), note == 'mailed')
        except Exception as exc:
            log.exception('task failed user=%s task=%s', user, task_id)
            for meta in created:
                store.delete(user, meta['id'])
            finish('error', '모델 서버가 답하지 않았습니다' if isinstance(exc, (LlmError, asyncio.TimeoutError)) else f'{type(exc).__name__}: {str(exc)[:200]}')
        finally:
            running.discard((user, task_id))
        return stored()

    def refresh_pending(now: datetime) -> bool:
        """오늘 자료 갱신이 시작됐는데 아직 안 끝났는가(끝나기를 조금 기다렸다가 일을 한다)."""
        if not briefing.refresh_status:
            return False
        try:
            status = json.loads(Path(briefing.refresh_status).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return str(status.get('started', '')).startswith(now.strftime('%Y-%m-%d')) and not status.get('finished')

    async def task_tick(now: datetime | None = None) -> list[tuple[str, str]]:
        """할 때가 된 일을 한다. 돌려주는 것: 이번에 한 (계정, 일 이름)들. 서버가 꺼져 있던 시각의 일은 그날 안에 켜지면 그때 한다."""
        now = now or datetime.now()
        day, made = now.strftime('%Y-%m-%d'), []
        if not desk_on:
            return made
        waiting = refresh_pending(now)
        for user, mine in database.tasks().items():
            if not users.taken(user):                            # 없어진 계정의 일은 하지 않는다
                continue
            for task in mine:
                late = tasks.minutes_late(task, now, briefing.refresh_time)
                if task.get('paused') or late is None or late < 0 or (user, task['id']) in running or (waiting and late < briefing.wait_minutes):
                    continue
                last = task.get('last') or {}
                stale = last.get('status') == 'running' and time.time() - float(last.get('at') or 0) > 3600      # 하다가 서버가 꺼진 것
                again = last.get('status') == 'error' and int(last.get('attempts') or 0) < 3 and time.time() - float(last.get('at') or 0) > 1200
                if last.get('day') != day or stale or again:
                    await run_task(user, task['id'], day)
                    made.append((user, task['title']))
        if now.strftime('%H:%M') >= max(briefing.time, briefing.refresh_time):
            for user in conversations.users():
                if users.taken(user):
                    try:
                        await suggest_for(user, day)
                    except Exception:
                        log.exception('suggest failed user=%s', user)
        return made

    async def suggest_for(user: str, day: str) -> list[dict[str, Any]]:
        """보통 대화에서 거듭 물은 주제 가운데 아직 맡기지 않은 것을 골라 "이것도 챙겨 드릴까요" 하고 권할 거리로 둔다(일을 만들지는 않는다).
        하루 한 번, 지난번에 찾은 뒤로 새 대화가 있었던 계정만 모델에 묻는다."""
        profile = profile_of(user)
        pending = [entry for entry in profile.get('suggestions') or [] if isinstance(entry, dict) and entry.get('topic')]
        if profile.get('suggested') == day:
            return pending
        talks = [item for item in conversations.items(user) if item.get('kind') != 'assistant']
        newest = max((float(item.get('updated') or 0) for item in talks), default=0.0)
        changes: dict[str, Any] = {'suggested': day}
        if len(pending) < MAX_SUGGESTIONS and newest > float(profile.get('suggested_at') or 0):
            questions = brief.asked(talks, time.time(), briefing.history_days)
            if len(questions) >= 2:
                if state['llm'] is None:
                    state['llm'] = Client(settings.llm)
                taken = [*(profile.get('muted') or []), *(task['title'] for task in database.tasks(user)), *(entry['topic'] for entry in pending)]
                found = await brief.interests(state['llm'], questions, [], MAX_SUGGESTIONS, taken)
                known = {''.join(str(topic).split()).lower() for topic in taken}
                fresh = [topic for topic in found if ''.join(topic['topic'].split()).lower() not in known][:MAX_SUGGESTIONS - len(pending)]
                pending = [*pending, *({'id': secrets.token_urlsafe(5), 'day': day, **topic} for topic in fresh)]
                changes.update(suggestions=pending, suggested_at=time.time())
                log.info('suggested user=%s new=%d', user, len(fresh))
        keep_profile(user, **changes)
        return pending

    def today_of(user: str, now: datetime | None = None) -> dict[str, Any]:
        """오늘 화면에 보일 것: 올라온 결과의 카드(오늘 것이 없으면 최근 7일 안의 마지막 날 것), 하는 중·소식 없음·실패·예정, 권하는 일."""
        now = now or datetime.now()
        day, mine, item = now.strftime('%Y-%m-%d'), database.tasks(user), conversations.assistant(user)
        feedback = {task['id']: {entry['day']: entry['value'] for entry in task.get('feedback') or []} for task in mine}
        by_day: dict[str, list[dict[str, Any]]] = {}
        for index, message in enumerate(item['messages'][:-1]):
            auto = message.get('auto')
            if isinstance(auto, dict) and auto.get('day'):
                by_day.setdefault(auto['day'], []).append({
                    'index': index, 'task': auto.get('task'), 'title': auto.get('title') or '맡긴 일', 'day': auto['day'],
                    'summary': tasks.headline(item['messages'][index + 1]['content']), 'kept': auto.get('task') in feedback,
                    'feedback': feedback.get(auto.get('task'), {}).get(auto['day'], '')})
        floor = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        shown = day if day in by_day else max((known for known in by_day if known >= floor), default=day)
        last = {task['id']: task.get('last') or {} for task in mine}
        upcoming = sorted(({'title': task['title'], 'time': max(task['time'], briefing.refresh_time)} for task in mine
                           if not task.get('paused') and (tasks.minutes_late(task, now, briefing.refresh_time) or 0) < 0 and last[task['id']].get('day') != day),
                          key=lambda entry: entry['time'])
        return {'today': day, 'day': shown, 'cards': list(reversed(by_day.get(shown, []))),
                'running': [task['title'] for task in mine if (user, task['id']) in running],
                'quiet': [task['title'] for task in mine if last[task['id']].get('day') == day and last[task['id']].get('status') == 'quiet'],
                'errors': [{'task': task['id'], 'title': task['title'], 'note': last[task['id']].get('note') or ''} for task in mine
                           if last[task['id']].get('day') == day and last[task['id']].get('status') == 'error' and (user, task['id']) not in running],
                'upcoming': upcoming,
                'suggestions': [{key: entry.get(key, '') for key in ('id', 'topic', 'kind', 'conditions')} for entry in profile_of(user).get('suggestions') or []]}

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
        page = (STATIC / 'login.html').read_text(encoding='utf-8').replace(LOGIN_ERROR_MARK, error).replace(SIGNUP_MARK, SIGNUP_LINK if signup_open else '')
        return Response(page, media_type='text/html; charset=utf-8')

    # ── 회원가입 신청: 받아만 두고, 관리자가 검토해 등록한다 ─────────────────────
    asked_at: list[float] = []                               # 최근 한 시간에 받은 신청의 시각(전체)
    asked_by: dict[str, list[float]] = {}                    # 접속 주소별
    notify_to = signup.notify or next((str(entry.get('email') or '').strip() for entry in settings.users if entry.get('name') == signup.admin), '')

    def notice_task(to: str, subject: str, page: str, plain: str) -> BackgroundTask | None:
        """알림 메일을 응답을 보낸 뒤에 보낸다. 못 보내도 신청·승인은 그대로다(화면에서 볼 수 있다)."""
        if not (to and settings.mail.username):
            return None

        async def deliver() -> None:
            try:
                await asyncio.to_thread(mailer.send, settings.mail, to, subject, page, plain)
            except Exception as exc:
                log.warning('notice mail failed subject=%r: %s', subject, type(exc).__name__)
        return BackgroundTask(deliver)

    async def signup_page(request: Request) -> Response:
        if not signup_open:
            return Response('not found', status_code=404)
        if current(request) is not None:
            return RedirectResponse('/', status_code=303)
        return Response((STATIC / 'signup.html').read_bytes(), media_type='text/html; charset=utf-8')

    async def signup_api(request: Request) -> Response:
        if not signup_open:
            return JSONResponse({'error': '없는 주소입니다.'}, status_code=404)
        if not (same_origin(request) and request.headers.get(API_HEADER) == API_HEADER_VALUE):
            return JSONResponse({'error': '요청을 확인하지 못했습니다.'}, status_code=403)
        declared = request.headers.get('content-length') or ''
        if not declared.isdigit() or int(declared) > 4096:   # 로그인하지 않은 사람이 보내는 요청이라 본문을 읽기 전에 크기를 본다
            return JSONResponse({'error': '요청이 너무 큽니다.'}, status_code=413)
        try:
            body = json.loads(await request.body())
            name, email, password = (str(body.get(key) or '') for key in ('username', 'email', 'password'))
        except (ValueError, AttributeError):
            return JSONResponse({'error': '요청을 읽지 못했습니다.'}, status_code=400)
        name, email = name.strip(), email.strip()
        if not SIGNUP_NAME.fullmatch(name):
            return JSONResponse({'error': '아이디는 한글·영문·숫자로 2~32자입니다( . _ - 도 쓸 수 있습니다).', 'field': 'username'}, status_code=400)
        if len(email) > 120 or not SIGNUP_EMAIL.fullmatch(email):
            return JSONResponse({'error': '메일 주소를 확인해 주세요.', 'field': 'email'}, status_code=400)
        if not MIN_PASSWORD <= len(password) <= 256 or password.casefold() == name.casefold():
            return JSONResponse({'error': f'비밀번호는 {MIN_PASSWORD}자 이상이고 아이디와 달라야 합니다.', 'field': 'password'}, status_code=400)
        known = database.accounts()
        if users.taken(name) or name.casefold() in {account['name'].casefold() for account in known}:
            return JSONResponse({'error': '이미 쓰이고 있는 아이디입니다.', 'field': 'username'}, status_code=409)
        # 여기부터는 비밀번호 해시 계산(무겁다)과 메일이 따르므로 받는 수를 묶는다
        now, ip = time.time(), client_ip(request)
        asked_at[:] = [moment for moment in asked_at if now - moment < 3600]
        mine = [moment for moment in asked_by.get(ip, []) if now - moment < 3600]
        if len(asked_by) > 5000:
            asked_by.clear()
        waiting = sum(account['status'] == 'pending' for account in known)
        if len(asked_at) >= signup.per_hour or len(mine) >= max(1, signup.per_hour // 2) or waiting >= signup.max_pending:
            log.warning('signup refused (limit) ip=%s waiting=%d', ip, waiting)
            return JSONResponse({'error': '지금은 신청을 받을 수 없습니다. 잠시 뒤 다시 해 보거나 관리자에게 문의해 주세요.'}, status_code=429)
        asked_at.append(now)
        asked_by[ip] = mine + [now]
        hashed = await asyncio.to_thread(hash_password, password)
        try:
            database.add_account(name, email, hashed, now)
        except sqlite3.IntegrityError:                       # 그사이 같은 아이디로 신청이 들어왔다
            return JSONResponse({'error': '이미 쓰이고 있는 아이디입니다.', 'field': 'username'}, status_code=409)
        log.info('signup asked name=%r ip=%s', name, ip)
        page, plain = mailer.notice('가입 신청이 들어왔습니다', [('아이디', name), ('메일', email), ('신청한 때', time.strftime('%Y-%m-%d %H:%M', time.localtime(now)))],
                                    f'{briefing.app_url}/?view=accounts' if briefing.app_url else '', '검토하러 가기')
        return JSONResponse({'ok': True}, background=notice_task(notify_to, f'가입 신청: {name}', page, plain))

    async def accounts_api(request: Request) -> Response:
        """관리자가 가입 신청을 검토한다: 목록, 승인(approve), 거절(reject), 등록된 계정 지우기(remove)."""
        session = api_guard(request, write=request.method != 'GET')
        if isinstance(session, Response):
            return session
        if not signup_open or session.user != signup.admin:
            return JSONResponse({'error': '없는 주소입니다.'}, status_code=404)
        told = None
        if request.method == 'POST':
            try:
                body = json.loads(await request.body())
                name, action = str(body.get('name') or ''), str(body.get('action') or '')
            except (ValueError, AttributeError):
                name = action = ''
            account = next((entry for entry in database.accounts() if entry['name'] == name), None)
            if account is None or (action, account['status']) not in (('approve', 'pending'), ('reject', 'pending'), ('remove', 'active')):
                return JSONResponse({'error': '그 신청이나 계정을 찾지 못했습니다.'}, status_code=404)
            if action == 'approve':
                if not users.add(name, account['password_hash']):
                    return JSONResponse({'error': '이미 쓰이고 있는 아이디라 등록할 수 없습니다. 거절한 뒤 다른 아이디로 신청받으세요.'}, status_code=409)
                database.activate_account(name, time.time())
                emails.setdefault(name, account['email'])
                page, plain = mailer.notice('가입이 승인되었습니다', [('아이디', name)], briefing.app_url, '로그인하러 가기')
                told = notice_task(account['email'], '가입이 승인되었습니다', page, plain)
            else:
                database.delete_account(name)
                if action == 'remove':                       # 로그인도, 남긴 것도 모두 없앤다(같은 아이디로 다시 가입한 사람이 물려받지 않게)
                    users.remove(name)
                    emails.pop(name, None)
                    sessions.drop_user(name)
                    conversations.purge(name)
                    store.purge(name)
                    database.forget(name)
            log.info('account %s name=%r by=%s', action, name, session.user)
        listed = [{key: account[key] for key in ('name', 'email', 'status', 'created', 'decided')} for account in database.accounts()]
        return JSONResponse({'pending': [entry for entry in listed if entry['status'] == 'pending'],
                             'active': [entry for entry in listed if entry['status'] == 'active']}, background=told)

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
        response.set_cookie(COOKIE, sessions.create(name, users.stamp(name)), max_age=int(sessions.lifetime), httponly=True, samesite='strict',
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
                             'model': settings.llm.model, 'max_question_chars': web.max_question_chars,
                             **({'admin': True, 'signups': len(database.accounts('pending'))} if signup_open and session.user == signup.admin else {})})

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
            if len(earlier) < 2 or (previous.get('kind') == 'brief' and len(earlier) == 2) or earlier[-2].get('auto'):       # 브리핑·맡긴 일의 결과는 질문에서 나온 답이 아니다
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
                desk = desk_on and previous is not None and previous.get('kind') == 'assistant'      # 비서 대화에서만 일을 맡길 수 있다
                sources = Sources(previous['sources'] if previous else [], **({'limit': ASSISTANT_SOURCES} if desk else {}))
                local = LocalTools(store, session.user, previews, sources, extra=desk_for(session.user) if desk else None)
                # 앞선 답에서 쓰인 출처 번호를 알려 줘, 이어지는 질문에서도 같은 번호로 가리키게 한다
                local.system_note = sources.note(set().union(*(cited_numbers(message['content']) for message in earlier if message['role'] == 'assistant')))
                # 앞선 차례에 무엇을 찾았고 그중 얼마나 봤는지도 알려 준다(앞선 답의 목록에서만 골라 답하지 않게)
                local.system_note += agent.earlier_searches([message.get('steps') or [] for message in earlier[-CONTEXT_MESSAGES:] if message['role'] == 'assistant'])
                if desk:
                    local.system_note += tasks.note(database.tasks(session.user), briefing.max_tasks, mail_ok(session.user), briefing.time, notes=notes_of(session.user))
                async with gate:
                    started = time.monotonic()
                    outcome = await run(history, listen, local)
                final, record = _latest_files(created), {'steps': steps, 'seconds': round(time.monotonic() - started, 1)}
                if regenerate:
                    for meta in previous['messages'][-1].get('files') or []:      # 바뀌어 사라지는 답의 파일은 더 열 길이 없다
                        store.delete(session.user, meta['id'])
                    item = conversations.replace_answer(session.user, previous['id'], outcome.answer, final, **record)
                else:
                    item = conversations.append(session.user, previous['id'] if previous else None, question, outcome.answer, attached, final,
                                                sources.items, **record)
                if desk:
                    conversations.tidy(session.user, item['id'])
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
        session = api_guard(request, write=request.method != 'GET')
        if isinstance(session, Response):
            return session
        found = store.get(session.user, request.path_params['file_id'])     # 남의 파일은 id 를 알아도 못 받는다
        if found is None:
            return JSONResponse({'error': '없는 파일입니다.'}, status_code=404)
        if request.method == 'DELETE':
            store.delete(session.user, found[0]['id'])
            conversations.drop_file(session.user, found[0]['id'])
            log.info('file deleted user=%s name=%r', session.user, found[0]['name'])
            return JSONResponse({'deleted': True})
        return download(found[0]['name'], found[0]['kind'], found[1])

    async def file_list(request: Request) -> Response:
        """이 계정에 보관된 문서(올린 것, 만든 것, 고친 것)를 대화와 상관없이 최근 것부터."""
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        where, held = conversations.files(session.user), store.list(session.user)
        replaced = {meta.get('previous') for meta in held if meta.get('previous')}
        listed = []
        for meta in held:
            if meta.get('origin') != 'upload' and meta['id'] in replaced and meta['id'] not in where:
                continue                                 # 한 답 안에서 거쳐 간 중간본(카드로도 보여 주지 않는다)
            entry = {key: meta[key] for key in FILE_FIELDS if key in meta}
            if meta['id'] in where:
                entry['conversation'] = where[meta['id']]
            listed.append(entry)
        return JSONResponse({'files': listed, 'bytes': sum(int(entry.get('size') or 0) for entry in listed)})

    async def file_info(request: Request) -> Response:
        """파일 정보(쪽 수 포함). 미리보기 창이 연다. 쪽 수를 아직 모르면 여기서 미리보기를 만든다."""
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        found = store.get(session.user, request.path_params['file_id'])
        if found is None:
            return JSONResponse({'error': '없는 파일입니다.'}, status_code=404)
        meta = found[0]
        problem: dict[str, Any] = {}
        if not meta.get('pages'):
            try:
                pages = await asyncio.to_thread(filestore.pdf_pages, await previews.pdf(session.user, meta, found[1]))
                meta = store.update(session.user, meta['id'], pages=pages) or meta
            except filestore.FileError as exc:
                problem = {'preview_error': str(exc)}
        if meta.get('root') and not store.has(session.user, meta['root']):       # 원본을 지웠으면 견주어 볼 것이 없다
            meta = {key: value for key, value in meta.items() if key != 'root'}
        return JSONResponse({**meta, **problem})

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

    def desk_state(user: str) -> dict[str, Any]:
        mine = database.tasks(user) if desk_on else []
        item = conversations.assistant(user) if desk_on else None
        return {'available': desk_on, 'conversation_id': item['id'] if item else '', 'updated': item['updated'] if item else 0,
                'tasks': [tasks.shown(task, (user, task['id']) in running) for task in mine], 'max_tasks': briefing.max_tasks,
                'notes': [{key: entry.get(key, '') for key in ('id', 'text', 'by', 'at')} for entry in notes_of(user)] if desk_on else [], 'max_notes': tasks.MAX_NOTES,
                'today': today_of(user) if desk_on else None,
                'mail': mail_ok(user), 'to': _masked(emails[user]) if mail_ok(user) else '', 'running': any(owner == user for owner, _ in running),
                'delivered': max((float((task.get('last') or {}).get('at') or 0) for task in mine if (task.get('last') or {}).get('status') == 'done'), default=0)}

    async def assistant_api(request: Request) -> Response:
        """GET: 비서 대화의 id, 맡긴 일, 메모, 오늘 화면에 보일 것. POST {id, action, …}: 화면에서 하나를 다룬다 —
        일: run | pause | resume | delete | mail_on | mail_off | up | down(day 와 함께, 그날 결과에 대한 반응)
        메모: note_save(text, id 가 있으면 고치기) | note_delete        권한 일: suggest_accept | suggest_dismiss"""
        session = api_guard(request, write=request.method != 'GET')
        if isinstance(session, Response):
            return session
        user = session.user
        if request.method == 'POST':
            if not desk_on:
                return JSONResponse({'error': '비서를 쓸 수 없게 설정되어 있습니다.'}, status_code=403)
            try:
                body = json.loads(await request.body() or b'{}')
                task_id, action = str(body.get('id') or ''), str(body.get('action') or '')
            except (ValueError, AttributeError):
                body, task_id, action = {}, '', ''
            if action in ('note_save', 'note_delete'):           # 메모를 화면에서 고친다
                notes = notes_of(user)
                try:
                    if action == 'note_delete':
                        if not any(entry['id'] == task_id for entry in notes):
                            raise tasks.TaskError('그 메모를 찾지 못했습니다')
                        notes = [entry for entry in notes if entry['id'] != task_id]
                    else:
                        notes, _ = tasks.put_note(notes, body.get('text'), task_id, by='user')
                except tasks.TaskError as exc:
                    return JSONResponse({'error': str(exc).replace('note_id 의 ', '')}, status_code=400)
                keep_profile(user, notes=notes)
                return JSONResponse(desk_state(user))
            if action in ('suggest_accept', 'suggest_dismiss'):  # 권한 일을 받아들이거나 물린다
                profile = profile_of(user)
                pending = profile.get('suggestions') or []
                picked = next((entry for entry in pending if entry.get('id') == task_id), None)
                if picked is None:
                    return JSONResponse({'error': '그 제안을 찾지 못했습니다.'}, status_code=404)
                changes: dict[str, Any] = {'suggestions': [entry for entry in pending if entry is not picked]}
                if action == 'suggest_accept':
                    if len(database.tasks(user)) >= briefing.max_tasks:
                        return JSONResponse({'error': f'맡을 수 있는 일은 {briefing.max_tasks}개까지입니다.'}, status_code=400)
                    database.save_task(user, tasks.suggested(picked, briefing.time))
                else:
                    changes['muted'] = [*(profile.get('muted') or []), picked['topic']][-30:]      # 다시 권하지 않는다
                keep_profile(user, **changes)
                return JSONResponse(desk_state(user))
            task = next((entry for entry in database.tasks(user) if entry['id'] == task_id), None)
            if task is None or action not in ('run', 'pause', 'resume', 'delete', 'mail_on', 'mail_off', 'up', 'down'):
                return JSONResponse({'error': '그 일을 찾지 못했습니다.'}, status_code=404)
            if action == 'run':
                if (user, task_id) in running:
                    return JSONResponse({'error': '지금 하고 있습니다.'}, status_code=409)
                job = asyncio.create_task(run_task(user, task_id, datetime.now().strftime('%Y-%m-%d'), force=True))
                background.add(job)
                job.add_done_callback(background.discard)
                await asyncio.sleep(0)                           # 하기 시작한 것이 아래 목록에 보이게
            elif action == 'delete':
                database.delete_task(user, task_id)
            elif action in ('up', 'down'):                       # 그날 결과에 대한 반응(다음에 할 때 반영한다)
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(body.get('day') or '')):
                    return JSONResponse({'error': '어느 날의 결과인지 알 수 없습니다.'}, status_code=400)
                database.save_task(user, tasks.react(task, body['day'], action))
            elif action == 'mail_on' and not mail_ok(user):
                return JSONResponse({'error': '이 계정에는 메일 주소가 없습니다.'}, status_code=400)
            else:
                change = {'pause': {'paused': True}, 'resume': {'paused': False}, 'mail_on': {'mail': True}, 'mail_off': {'mail': False}}[action]
                database.save_task(user, {**task, **change})
        return JSONResponse(desk_state(user))

    async def search_api(request: Request) -> Response:
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        return JSONResponse({'results': conversations.search(session.user, request.query_params.get('q', '')[:200])})

    checking = asyncio.Lock()

    async def status_api(request: Request) -> Response:
        """도구 상태: 모델, 자료 서버마다 붙어 있는지와 쓸 수 있는 도구, 아침 자료 갱신의 결과."""
        session = api_guard(request, write=False)
        if isinstance(session, Response):
            return session
        if runner is None and hub.servers and time.monotonic() - state['refreshed'] > 15 and not checking.locked():
            async with checking:                         # 화면을 열 때 다시 붙어 본다(연달아 열어도 15초에 한 번)
                try:
                    await hub.refresh()
                    state['refreshed'] = time.monotonic()
                except Exception:
                    log.exception('MCP 서버 목록을 받지 못했습니다')
        offered: dict[str, list[str]] = {}
        for tool in hub.tools.values():
            offered.setdefault(tool.server.name, []).append(tool.name)
        return JSONResponse({'model': {'name': settings.llm.model, 'ok': state.get('model_ok')},
                             'servers': [{'name': server.name, 'ok': server.name not in hub.unavailable, 'tools': offered.get(server.name, [])}
                                         for server in hub.servers],
                             'local': [*LOCAL_TOOLS, *([sweep.NAME] if sweep.available(hub) else [])],
                             'refresh': _refresh_report(briefing.refresh_status)})

    background: set[asyncio.Task] = set()
    routes = [Route('/signup', signup_page), Route('/api/signup', signup_api, methods=['POST']), Route('/api/accounts', accounts_api, methods=['GET', 'POST']),
              Route('/api/assistant', assistant_api, methods=['GET', 'POST']), Route('/api/search', search_api), Route('/api/status', status_api), Route('/api/files', upload, methods=['POST']), Route('/api/files', file_list),
              Route('/api/files/{file_id}', file, methods=['GET', 'DELETE']),
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

        async def clockwork() -> None:               # 맡긴 일: 30초마다 할 때가 된 것이 있는지 본다
            while True:
                try:
                    await task_tick()
                except Exception:
                    log.exception('맡긴 일 확인 중 오류')
                await asyncio.sleep(30)
        timer = asyncio.create_task(clockwork()) if runner is None and desk_on else None
        yield
        for pending in (task, timer, *background):
            if pending and not pending.done():
                pending.cancel()
        if state['llm'] is not None:
            await state['llm'].close()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityHeaders),
                                                                  Middleware(TrustedHostMiddleware, allowed_hosts=list(web.allowed_hosts))])
    app.state.task_tick, app.state.run_task, app.state.suggest_for, app.state.today_of = task_tick, run_task, suggest_for, today_of      # 시험과 수동 실행에서 직접 부를 수 있게
    return app


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
