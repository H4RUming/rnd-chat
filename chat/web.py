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
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
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

from . import agent, brief, docs, editing, files as filestore, hwp, mailer, storage, sweep
from .auth import Session, Sessions, Throttle, Users
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
                     if not (item.get('kind') == 'brief' and index == 0)]
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
               files: list | None = None, sources: list | None = None, **extra: Any) -> dict[str, Any]:
        mine = self._data.setdefault(user, OrderedDict())
        item = mine.get(conversation_id or '')
        if item is None:
            # sources: 이 대화에서 도구가 찾은 자료들(번호는 대화 안에서 이어진다). 답의 [번호] 가 이것을 가리킨다.
            item = {'id': secrets.token_urlsafe(9), 'title': ' '.join(question.split())[:40], 'messages': [], 'updated': 0.0,
                    'sources': sources if sources is not None else []}
            mine[item['id']] = item
            while len(mine) > MAX_CONVERSATIONS:
                dropped, _ = mine.popitem(last=False)
                if self._database:
                    self._database.delete(user, dropped)
        item['messages'] += [_message('user', question, attached), _message('assistant', answer, files, **extra)]
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
    sessions = sessions or Sessions(idle=web.session_hours * 3600.0)
    throttle = throttle or Throttle()                        # 계정별
    # 접속 IP 별 한도는 훨씬 느슨하게 둔다: 한 사무실이 같은 IP 를 쓰고, 터널 뒤에서는 모두가 한 IP 로 보일 수 있다
    ip_throttle = ip_throttle or Throttle(limit=throttle.limit * 4, lock=throttle.lock, max_lock=throttle.max_lock)
    # 자료 폴더가 정해져 있으면 대화를 파일에 남기고, 대화가 가리키는 파일(올린 것·만든 것)도 다시 띄울 때 지우지 않는다
    database = storage.Database(Path(settings.data_dir) / 'chat.sqlite3') if settings.data_dir else None
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

    # ── 아침 브리핑 ──────────────────────────────────────────────────────────
    briefing = settings.briefing
    receivers = {str(entry.get('name')): str(entry.get('email') or '').strip() for entry in settings.users
                 if str(entry.get('email') or '').strip() or entry.get('brief') is True}        # 브리핑을 받는 계정 → 메일 주소(없으면 화면으로만)
    making: set[str] = set()

    def profile_of(user: str) -> dict[str, Any]:
        """계정의 브리핑 설정: interests(챙겨 볼 주제), muted(사용자가 뺀 주제), paused(아침마다 받지 않음)."""
        data = database.profile(user) if database is not None else {}
        data.pop('updated', None)
        return data

    async def make_brief(user: str, day: str) -> dict[str, Any]:
        """그 계정의 그날 브리핑을 만든다: 관심사 뽑기 → 찾아서 쓰기 → 대화로 남기기 → 메일."""
        making.add(user)
        database.save_brief(user, day, 'running', time.time(), attempt=True)
        try:
            questions = brief.asked(conversations.items(user), time.time(), briefing.history_days)
            if state['llm'] is None:
                state['llm'] = Client(settings.llm)
            profile = profile_of(user)
            stored, muted = profile.get('interests') or [], profile.get('muted') or []
            found = await brief.interests(state['llm'], questions, stored, briefing.max_interests, muted) if questions else []
            # 사용자가 직접 정한 주제는 대화가 없어도 남고, 대화에서 뽑은 주제는 그 뒤에 잇는다
            topics = brief.merge([item for item in stored if item.get('pinned')], found, muted, briefing.max_interests)
            if not topics:
                database.save_brief(user, day, 'skipped', time.time(), note='챙겨 볼 주제를 찾을 만한 대화가 아직 없습니다')
                return database.brief(user, day)
            database.save_profile(user, {**profile_of(user), 'interests': topics}, time.time())
            steps: list[dict[str, Any]] = []
            created: list[dict[str, Any]] = []

            async def listen(event: dict[str, Any]) -> None:
                if event['type'] == 'file':
                    created.append(event['file'])
                elif event['type'] == 'tool_call' and len(steps) < 60:
                    steps.append({'name': event['name'], 'args': _brief_arguments(event['arguments'])})
                elif event['type'] == 'tool_result' and steps:
                    steps[-1].update(is_error=event['is_error'], summary=event.get('summary') or {})
            sources = Sources([])
            local = LocalTools(store, user, previews, sources)
            label = brief.title(day)
            async with gate:
                started = time.monotonic()
                outcome = await asyncio.wait_for(run([{'role': 'user', 'content': brief.instruction(
                    topics, day, database.last_done(user, day), briefing.first_years)}], listen, local), timeout=1800)
            if not outcome.answer.strip():
                raise RuntimeError('브리핑 글이 비어 있습니다')
            item = conversations.append(user, None, label, outcome.answer, None, _latest_files(created), sources.items,
                                        steps=steps, seconds=round(time.monotonic() - started, 1))
            conversations.mark(user, item['id'], kind='brief', title=label)
            mailed, note = '', ''
            if receivers.get(user) and settings.mail.username:
                try:
                    page, plain = mailer.render(outcome.answer, sources.items, label, briefing.app_url)
                    await asyncio.to_thread(mailer.send, settings.mail, receivers[user], label, page, plain)
                    mailed = receivers[user]
                except Exception as exc:                 # 메일이 안 가도 브리핑은 화면에 남는다
                    note = f'메일을 보내지 못했습니다: {type(exc).__name__}'
                    log.warning('brief mail failed user=%s error=%s', user, type(exc).__name__)
            database.save_brief(user, day, 'done', time.time(), conversation_id=item['id'], mailed=mailed, note=note)
            log.info('brief done user=%s day=%s topics=%d steps=%d mailed=%s', user, day, len(topics), len(steps), bool(mailed))
        except Exception as exc:
            log.exception('brief failed user=%s', user)
            database.save_brief(user, day, 'error', time.time(), note=f'{type(exc).__name__}: {str(exc)[:200]}')
        finally:
            making.discard(user)
        return database.brief(user, day)

    def refresh_pending(now: datetime) -> bool:
        """오늘 자료 갱신이 시작됐는데 아직 안 끝났는가(끝나기를 조금 기다렸다가 브리핑을 만든다)."""
        if not briefing.refresh_status:
            return False
        try:
            status = json.loads(Path(briefing.refresh_status).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return str(status.get('started', '')).startswith(now.strftime('%Y-%m-%d')) and not status.get('finished')

    async def brief_tick(now: datetime | None = None) -> list[str]:
        """만들 때가 된 브리핑을 만든다. 돌려주는 것: 이번에 만든 계정들."""
        now = now or datetime.now()
        clock, day, made = now.strftime('%H:%M'), now.strftime('%Y-%m-%d'), []
        if not (briefing.enabled and database is not None and briefing.time <= clock < briefing.until):
            return made
        hours, minutes = (int(part) for part in briefing.time.split(':'))
        if refresh_pending(now) and (now.hour * 60 + now.minute) - (hours * 60 + minutes) < briefing.wait_minutes:
            return made
        for user in receivers:
            if profile_of(user).get('paused'):               # 화면에서 꺼 둔 계정(지금 받기는 된다)
                continue
            record = database.brief(user, day)
            stale = record and record['status'] == 'running' and user not in making and time.time() - record['updated'] > 3600       # 만들다 서버가 꺼진 것
            again = record and record['status'] == 'error' and record['attempts'] < 3 and time.time() - record['updated'] > 1200
            if user not in making and (record is None or stale or again):
                await make_brief(user, day)
                made.append(user)
        return made

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
            if len(earlier) < 2 or (previous.get('kind') == 'brief' and len(earlier) == 2):       # 브리핑 글은 질문에서 나온 답이 아니다
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
                # 앞선 차례에 무엇을 찾았고 그중 얼마나 봤는지도 알려 준다(앞선 답의 목록에서만 골라 답하지 않게)
                local.system_note += agent.earlier_searches([message.get('steps') or [] for message in earlier[-CONTEXT_MESSAGES:] if message['role'] == 'assistant'])
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

    async def brief_api(request: Request) -> Response:
        """GET: 이 계정의 브리핑 상태·주제·지난 기록. POST: 오늘 브리핑을 지금 만든다(이미 있으면 새로 만든다).
        PUT: 주제 목록(interests)이나 아침마다 받기를 끌지(paused)를 바꾼다."""
        session = api_guard(request, write=request.method != 'GET')
        if isinstance(session, Response):
            return session
        user, available = session.user, database is not None and briefing.enabled and session.user in receivers
        if request.method == 'PUT':
            if not available:
                return JSONResponse({'error': '이 계정은 브리핑을 받도록 설정되어 있지 않습니다.'}, status_code=403)
            try:
                body = json.loads(await request.body() or b'{}')
                wanted, paused = body.get('interests'), body.get('paused')
            except (ValueError, AttributeError):
                return JSONResponse({'error': '요청 형식이 잘못되었습니다.'}, status_code=400)
            profile = profile_of(user)
            if wanted is not None:
                if not isinstance(wanted, list) or len(wanted) > briefing.max_interests:
                    return JSONResponse({'error': f'주제는 {briefing.max_interests}개까지입니다.'}, status_code=400)
                profile['interests'], profile['muted'] = brief.edited(profile.get('interests') or [], profile.get('muted') or [], wanted, briefing.max_interests)
            if isinstance(paused, bool):
                profile['paused'] = paused
            database.save_profile(user, profile, time.time())
            return JSONResponse({'interests': profile.get('interests') or [], 'paused': bool(profile.get('paused'))})
        if request.method == 'POST':
            if not available:
                return JSONResponse({'error': '이 계정은 브리핑을 받도록 설정되어 있지 않습니다.'}, status_code=403)
            if user in making:
                return JSONResponse({'error': '브리핑을 만드는 중입니다.'}, status_code=409)
            task = asyncio.create_task(make_brief(user, datetime.now().strftime('%Y-%m-%d')))
            background.add(task)
            task.add_done_callback(background.discard)
            return JSONResponse({'status': 'running'}, status_code=202)
        def shown(record: dict[str, Any]) -> dict[str, Any]:
            kept = conversations.get(user, record['conversation_id']) is not None         # 지운 브리핑은 열 수 없다
            return {'day': record['day'], 'title': brief.title(record['day']), 'status': record['status'], 'note': record['note'],
                    'conversation_id': record['conversation_id'] if kept else '', 'mailed': bool(record['mailed'])}
        history = [shown(record) for record in database.briefs(user)] if database is not None else []
        last = database.brief(user) if database is not None else None
        mailed = bool(receivers.get(user) and settings.mail.username)
        profile = profile_of(user)
        return JSONResponse({'available': available, 'time': briefing.time, 'mail': mailed, 'to': _masked(receivers[user]) if mailed else '',
                             'running': user in making, 'paused': bool(profile.get('paused')), 'max_interests': briefing.max_interests,
                             'interests': profile.get('interests') or [], 'history': history,
                             'last': {key: last[key] for key in ('day', 'status', 'conversation_id', 'note')} | {'mailed': bool(last['mailed'])} if last else None})

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
    routes = [Route('/api/brief', brief_api, methods=['GET', 'POST', 'PUT']), Route('/api/search', search_api), Route('/api/status', status_api), Route('/api/files', upload, methods=['POST']), Route('/api/files', file_list),
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

        async def clockwork() -> None:               # 아침 브리핑: 30초마다 만들 때가 됐는지 본다
            while True:
                try:
                    await brief_tick()
                except Exception:
                    log.exception('브리핑 확인 중 오류')
                await asyncio.sleep(30)
        timer = asyncio.create_task(clockwork()) if runner is None and briefing.enabled and database is not None else None
        yield
        for pending in (task, timer, *background):
            if pending and not pending.done():
                pending.cancel()
        if state['llm'] is not None:
            await state['llm'].close()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityHeaders),
                                                                  Middleware(TrustedHostMiddleware, allowed_hosts=list(web.allowed_hosts))])
    app.state.brief_tick, app.state.make_brief = brief_tick, make_brief          # 시험과 수동 실행에서 직접 부를 수 있게
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
