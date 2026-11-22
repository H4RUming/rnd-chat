"""로그인: 설정 파일의 계정, 비밀번호 대조, 로그인 시도 제한, 서버 쪽 세션.

계정은 설정 파일의 `[[users]]` 에 직접 적는다. 비밀번호는 `python -m chat.passwd` 로 만든 해시(`password_hash`)를
권하고, 평문(`password`)도 받는다. 쿠키에는 무작위 토큰만 실린다. 세션은 서버 메모리에 두고, 보관할 곳(대화를 남기는
파일)이 있으면 거기에도 적어 서버를 다시 띄워도 로그인이 유지되게 한다. 파일에는 토큰이 아니라 토큰의 해시를 적으며,
그 계정의 비밀번호가 바뀌거나 계정이 없어지면 남아 있던 세션은 버린다.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
import time
from typing import Any, Callable

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1        # 메모리 약 32MB, 한 번에 0.1초 안팎
MAX_PASSWORD = 256                                   # 지나치게 긴 입력으로 해시 계산을 오래 끌지 못하게


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=2 ** 27, dklen=32)
    return 'scrypt${}${}${}${}${}'.format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def _check_hash(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split('$')
        if kind != 'scrypt':
            return False
        salt, digest = base64.b64decode(salt), base64.b64decode(digest)
        candidate = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), maxmem=2 ** 27, dklen=len(digest))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


_DUMMY = hash_password('not-a-real-account')         # 없는 계정에도 같은 시간을 쓰게 하는 용도


@dataclass(frozen=True)
class User:
    name: str
    password_hash: str = ''
    password: str = ''


class Users:
    def __init__(self, entries: list[dict[str, Any]]):
        self._users: dict[str, User] = {}
        self._stamps: dict[str, str] = {}
        for entry in entries:
            name = str(entry.get('name') or '').strip()
            hashed, plain = str(entry.get('password_hash') or ''), str(entry.get('password') or '')
            if not name or not (hashed or plain):
                raise ValueError('설정 [[users]] 항목에는 name 과 password_hash(또는 password)가 있어야 합니다')
            if hashed and not hashed.startswith('scrypt$'):
                raise ValueError(f'계정 {name} 의 password_hash 형식이 아닙니다(python -m chat.passwd 로 만듭니다)')
            if name in self._users:
                raise ValueError(f'설정 [[users]] 의 name 이 겹칩니다: {name}')
            self._users[name] = User(name, hashed, '' if hashed else plain)

    def __len__(self) -> int:
        return len(self._users)

    def taken(self, name: str) -> bool:
        """그 이름을 이미 쓰는 계정이 있는지(대소문자만 다른 이름도 같은 것으로 본다)."""
        return name.casefold() in {known.casefold() for known in self._users}

    def add(self, name: str, password_hash: str) -> bool:
        """가입 신청이 승인된 계정을 더한다. 이름이 겹치면 더하지 않는다."""
        if self.taken(name) or not password_hash.startswith('scrypt$'):
            return False
        self._users[name] = User(name, password_hash)
        return True

    def remove(self, name: str) -> None:
        self._users.pop(name, None)
        self._stamps.pop(name, None)

    @property
    def plaintext(self) -> list[str]:
        """비밀번호를 평문으로 적어 둔 계정 이름(경고용)."""
        return [user.name for user in self._users.values() if user.password]

    def stamp(self, name: str) -> str:
        """그 계정의 비밀번호에서 만든 표식(없는 계정이면 빈 글). 남겨 둔 세션이 지금 비밀번호로 만든 것인지 가리는 데 쓴다.
        평문 비밀번호는 표식에서 거꾸로 맞혀 보기 어렵게 scrypt 를 거친다."""
        user = self._users.get(name)
        if user is None:
            return ''
        if name not in self._stamps:
            secret = user.password_hash.encode() if user.password_hash else hashlib.scrypt(
                user.password.encode(), salt=hashlib.sha256(f'rnd-chat session {name}'.encode()).digest()[:16],
                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=2 ** 27, dklen=32)
            self._stamps[name] = hashlib.sha256(b'rnd-chat session stamp\0' + secret).hexdigest()[:32]
        return self._stamps[name]

    def verify(self, name: str, password: str) -> bool:
        user = self._users.get(name)
        password = password[:MAX_PASSWORD]
        if user is None:
            _check_hash(password, _DUMMY)            # 계정이 있는지에 따라 응답 시간이 달라지지 않게
            return False
        if user.password_hash:
            return _check_hash(password, user.password_hash)
        _check_hash(password, _DUMMY)
        return hmac.compare_digest(password.encode(), user.password.encode())


class Throttle:
    """연속 실패가 limit 번에 이르면 잠그고, 잠금이 풀린 뒤 또 틀리면 잠금 시간을 두 배씩 늘린다(최대 max_lock)."""

    def __init__(self, limit: int = 5, lock: float = 60.0, max_lock: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.limit, self.lock, self.max_lock, self._clock = limit, lock, max_lock, clock
        self._state: dict[str, list[float]] = {}     # key → [연속 실패 수, 잠금 해제 시각]

    def locked_for(self, key: str) -> float:
        """남은 잠금 시간(초). 잠겨 있지 않으면 0."""
        state = self._state.get(key)
        return max(0.0, state[1] - self._clock()) if state else 0.0

    def failure(self, key: str) -> None:
        if len(self._state) > 10000:                 # 임의의 이름으로 메모리를 채우지 못하게
            self._state.clear()
        state = self._state.setdefault(key, [0, 0.0])
        state[0] += 1
        if state[0] >= self.limit:
            state[1] = self._clock() + min(self.lock * 2 ** (state[0] - self.limit), self.max_lock)

    def success(self, key: str) -> None:
        self._state.pop(key, None)


@dataclass
class Session:
    user: str
    created: float
    seen: float
    task: Any = None                                 # 지금 답을 만들고 있는 일(asyncio.Task). 끝나면 다음 질문을 받는다
    data: dict[str, Any] = field(default_factory=dict)
    stamp: str = ''                                  # 로그인할 때의 비밀번호 표식(Users.stamp)

    @property
    def busy(self) -> bool:
        """질문 하나를 처리하는 중인지. 깃발을 따로 두지 않고 일 자체를 본다: 연결이 어떻게 끊기든 일이 끝나면 풀린다
        (예전에는 응답을 흘려보내기 전에 연결이 끊기면 깃발이 내려가지 않아, 그 로그인으로는 질문이 계속 거절됐다)."""
        return self.task is not None and not self.task.done()


def _key(token: str) -> str:
    """세션을 찾는 열쇠. 보관 파일에 토큰을 그대로 두지 않으려고 해시로 찾는다."""
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """store: 세션을 남길 곳(sessions, save_session, touch_session, delete_session 이 있는 것). 없으면 메모리에만 둔다.
    valid(user, stamp): 남아 있던 세션을 받아들일지(계정이 그대로 있고 비밀번호가 바뀌지 않았는지)."""
    TOUCH = 300.0                                    # 마지막으로 쓴 때를 파일에 고쳐 적는 간격(요청마다 적지 않는다)

    def __init__(self, idle: float = 12 * 3600.0, lifetime: float = 7 * 86400.0, per_user: int = 5,
                 clock: Callable[[], float] = time.time, store: Any = None, valid: Callable[[str, str], bool] | None = None):
        self.idle, self.lifetime, self.per_user, self._clock, self._store = idle, lifetime, per_user, clock, store
        self._sessions: dict[str, Session] = {}
        self._written: dict[str, float] = {}
        now = clock()
        for key, user, created, seen, stamp in (store.sessions() if store is not None else []):
            if now - seen > idle or now - created > lifetime or (valid is not None and not valid(user, stamp)):
                store.delete_session(key)
            else:
                self._sessions[key] = Session(user, created, seen, stamp=stamp)
                self._written[key] = seen

    def _drop(self, key: str) -> None:
        if self._sessions.pop(key, None) is not None and self._store is not None:
            self._store.delete_session(key)
        self._written.pop(key, None)

    def create(self, user: str, stamp: str = '') -> str:
        now = self._clock()
        mine = sorted((item for item in self._sessions.items() if item[1].user == user), key=lambda item: item[1].seen)
        for key, _ in mine[:max(0, len(mine) - self.per_user + 1)]:        # 한 계정의 세션 수를 묶는다(오래된 것부터 끊음)
            self._drop(key)
        token = secrets.token_urlsafe(32)
        self._sessions[_key(token)] = Session(user, now, now, stamp=stamp)
        if self._store is not None:
            self._store.save_session(_key(token), user, now, now, stamp)
            self._written[_key(token)] = now
        return token

    def get(self, token: str | None) -> Session | None:
        key = _key(token) if token else ''
        session = self._sessions.get(key)
        if session is None:
            return None
        now = self._clock()
        if now - session.seen > self.idle or now - session.created > self.lifetime:
            self._drop(key)
            return None
        session.seen = now
        if self._store is not None and now - self._written.get(key, 0.0) > self.TOUCH:
            self._store.touch_session(key, now)
            self._written[key] = now
        return session

    def delete(self, token: str | None) -> None:
        if token:
            self._drop(_key(token))

    def drop_user(self, user: str) -> None:
        """그 계정의 세션을 모두 끊는다(계정을 지울 때)."""
        for key in [key for key, session in self._sessions.items() if session.user == user]:
            self._drop(key)
