"""대화를 파일(SQLite)에 남긴다. 서버를 다시 띄워도 대화와 그 안의 출처·작업 기록이 그대로 있다.

대화 하나가 행 하나다(내용은 JSON 한 덩어리). 서버는 대화를 메모리에 두고 쓰다가 바뀔 때마다 그 대화만 다시 적는다.
계정당 대화 수와 대화당 메시지 수에 상한이 있어(web.py) 통째로 읽고 써도 가볍다. 파일은 서버를 돌리는 계정만 읽을 수 있게 둔다.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS conversations (
    user TEXT NOT NULL,
    id TEXT NOT NULL,
    updated REAL NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (user, id)
);
CREATE TABLE IF NOT EXISTS profiles (       -- 계정의 관심사(대화 기록에서 뽑은 것)
    user TEXT PRIMARY KEY,
    updated REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS briefs (         -- 아침 브리핑: 계정마다 하루 한 건
    user TEXT NOT NULL,
    day TEXT NOT NULL,                      -- YYYY-MM-DD
    status TEXT NOT NULL,                   -- running | done | skipped | error
    conversation_id TEXT NOT NULL DEFAULT '',
    mailed TEXT NOT NULL DEFAULT '',        -- 메일을 보낸 주소(안 보냈으면 빈 글)
    note TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL,
    PRIMARY KEY (user, day)
);
'''


class Database:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        if fresh:
            self.path.chmod(0o600)
        self._connection.execute('PRAGMA journal_mode = WAL')
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        for extra in (self.path.with_name(self.path.name + '-wal'), self.path.with_name(self.path.name + '-shm')):
            if extra.exists():
                extra.chmod(0o600)

    def conversations(self) -> dict[str, OrderedDict[str, dict[str, Any]]]:
        """계정별 대화(오래된 것부터). 읽을 수 없는 행은 건너뛴다(하나가 깨져도 나머지는 보여야 한다)."""
        found: dict[str, OrderedDict[str, dict[str, Any]]] = {}
        for user, conversation_id, data in self._connection.execute('SELECT user, id, data FROM conversations ORDER BY updated, id'):
            try:
                item = json.loads(data)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get('id') == conversation_id and isinstance(item.get('messages'), list):
                found.setdefault(user, OrderedDict())[conversation_id] = item
        return found

    def save(self, user: str, item: dict[str, Any]) -> None:
        self._connection.execute('INSERT OR REPLACE INTO conversations (user, id, updated, data) VALUES (?, ?, ?, ?)',
                                 (user, item['id'], float(item.get('updated') or 0.0), json.dumps(item, ensure_ascii=False, separators=(',', ':'))))
        self._connection.commit()

    def delete(self, user: str, conversation_id: str) -> None:
        self._connection.execute('DELETE FROM conversations WHERE user = ? AND id = ?', (user, conversation_id))
        self._connection.commit()

    def profile(self, user: str) -> dict[str, Any]:
        row = self._connection.execute('SELECT data, updated FROM profiles WHERE user = ?', (user,)).fetchone()
        try:
            data = json.loads(row[0]) if row else {}
        except ValueError:
            data = {}
        return {**data, 'updated': row[1]} if row and isinstance(data, dict) else {}

    def save_profile(self, user: str, data: dict[str, Any], now: float) -> None:
        self._connection.execute('INSERT OR REPLACE INTO profiles (user, updated, data) VALUES (?, ?, ?)',
                                 (user, now, json.dumps(data, ensure_ascii=False, separators=(',', ':'))))
        self._connection.commit()

    _BRIEF = ('day', 'status', 'conversation_id', 'mailed', 'note', 'attempts', 'updated')

    def brief(self, user: str, day: str = '') -> dict[str, Any] | None:
        """그날의 브리핑 기록. day 를 비우면 가장 최근 것."""
        row = self._connection.execute(
            'SELECT day, status, conversation_id, mailed, note, attempts, updated FROM briefs WHERE user = ? AND (day = ? OR ? = \'\') '
            'ORDER BY day DESC LIMIT 1', (user, day, day)).fetchone()
        return dict(zip(self._BRIEF, row)) if row else None

    def briefs(self, user: str, limit: int = 14) -> list[dict[str, Any]]:
        """그 계정의 브리핑 기록(최근 날부터)."""
        rows = self._connection.execute('SELECT day, status, conversation_id, mailed, note, attempts, updated FROM briefs WHERE user = ? '
                                        'ORDER BY day DESC LIMIT ?', (user, limit)).fetchall()
        return [dict(zip(self._BRIEF, row)) for row in rows]

    def last_done(self, user: str, before: str) -> str:
        """before 보다 앞선 날 가운데 마지막으로 브리핑을 만든 날(없으면 빈 글)."""
        row = self._connection.execute("SELECT MAX(day) FROM briefs WHERE user = ? AND status = 'done' AND day < ?", (user, before)).fetchone()
        return row[0] or ''

    def save_brief(self, user: str, day: str, status: str, now: float, conversation_id: str = '', mailed: str = '', note: str = '',
                   attempt: bool = False) -> None:
        previous = self.brief(user, day) or {}
        self._connection.execute(
            'INSERT OR REPLACE INTO briefs (user, day, status, conversation_id, mailed, note, attempts, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (user, day, status, conversation_id or previous.get('conversation_id', ''), mailed or previous.get('mailed', ''), note[:500],
             int(previous.get('attempts', 0)) + (1 if attempt else 0), now))
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
