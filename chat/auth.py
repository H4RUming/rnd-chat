"""로그인: 설정 파일의 계정, 비밀번호 대조, 로그인 시도 제한, 서버 쪽 세션.

계정은 설정 파일의 `[[users]]` 에 직접 적는다. 비밀번호는 `python -m chat.passwd` 로 만든 해시(`password_hash`)를
권하고, 평문(`password`)도 받는다. 세션은 서버 메모리에만 두며 쿠키에는 무작위 토큰만 실린다.
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

    @property
    def plaintext(self) -> list[str]:
        """비밀번호를 평문으로 적어 둔 계정 이름(경고용)."""
        return [user.name for user in self._users.values() if user.password]

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
    busy: bool = False                               # 질문 하나를 처리하는 중인지
    data: dict[str, Any] = field(default_factory=dict)


class Sessions:
    def __init__(self, idle: float = 12 * 3600.0, lifetime: float = 7 * 86400.0, per_user: int = 5,
                 clock: Callable[[], float] = time.monotonic):
        self.idle, self.lifetime, self.per_user, self._clock = idle, lifetime, per_user, clock
        self._sessions: dict[str, Session] = {}

    def create(self, user: str) -> str:
        now = self._clock()
        mine = sorted((item for item in self._sessions.items() if item[1].user == user), key=lambda item: item[1].seen)
        for token, _ in mine[:max(0, len(mine) - self.per_user + 1)]:      # 한 계정의 세션 수를 묶는다(오래된 것부터 끊음)
            del self._sessions[token]
        token = secrets.token_urlsafe(32)
        self._sessions[token] = Session(user, now, now)
        return token

    def get(self, token: str | None) -> Session | None:
        session = self._sessions.get(token or '')
        if session is None:
            return None
        now = self._clock()
        if now - session.seen > self.idle or now - session.created > self.lifetime:
            del self._sessions[token]
            return None
        session.seen = now
        return session

    def delete(self, token: str | None) -> None:
        self._sessions.pop(token or '', None)
