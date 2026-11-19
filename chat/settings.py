"""설정 파일의 [llm], [agent], [web], [documents], [[mcp]], [[users]] 를 읽어 값의 범위를 확인한다."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import appconfig


@dataclass(frozen=True)
class Llm:
    base_url: str
    model: str
    api_key: str = ''
    temperature: float = 0.3
    max_tokens: int = 4096
    thinking: str = ''                   # 'on' | 'off' | ''(모델 기본). 생각하는 모델이 답하기 전에 따져 보게 할지
    timeout: float = 300.0


@dataclass(frozen=True)
class Agent:
    max_steps: int = 12
    tool_result_chars: int = 24000
    max_images: int = 4


@dataclass(frozen=True)
class Web:
    host: str = '127.0.0.1'
    port: int = 8080
    session_hours: float = 12.0          # 이만큼 쓰지 않으면 다시 로그인
    max_concurrent: int = 2              # 동시에 처리하는 질문 수(모델 서버에 거는 부하의 상한)
    max_question_chars: int = 4000
    trust_forwarded: bool = False        # Cloudflare 뒤에 둘 때만 true: 접속 IP 를 CF-Connecting-IP 에서 읽는다
    allowed_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost')   # 이 이름으로 온 요청만 받는다(포트 제외)
    tls_cert: str = ''                   # 인증서와 개인키 파일을 주면 https 로 연다(둘 다 있어야 한다)
    tls_key: str = ''


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str


@dataclass(frozen=True)
class Settings:
    llm: Llm
    agent: Agent
    servers: tuple[McpServer, ...]
    web: Web = Web()
    users: tuple[dict[str, Any], ...] = ()
    data_dir: str = ''                   # 올린 파일·만든 파일을 두는 곳(설정 파일 옆의 data/)
    rhwp: str = ''                       # 한글 문서 엔진(rhwp) 실행 파일. 비워 두면 PATH 에서 찾는다

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> 'Settings':
        config = appconfig.load(path)
        llm = _table(config, 'llm')
        base_url = str(llm.get('base_url') or '').rstrip('/')
        if urlsplit(base_url).scheme not in ('http', 'https'):
            raise ValueError(f'설정 [llm] base_url 이 없거나 주소가 아닙니다 ({appconfig.config_path(path)})')
        if not str(llm.get('model') or '').strip():
            raise ValueError('설정 [llm] model 이 없습니다')
        agent = _table(config, 'agent')
        web = _table(config, 'web')
        servers, seen = [], set()
        for entry in config.get('mcp', []):
            name, url = str(entry.get('name') or '').strip(), str(entry.get('url') or '').strip()
            if not name or urlsplit(url).scheme not in ('http', 'https'):
                raise ValueError(f'설정 [[mcp]] 항목에는 name 과 url 이 있어야 합니다: {entry}')
            if name in seen:
                raise ValueError(f'설정 [[mcp]] 의 name 이 겹칩니다: {name}')
            seen.add(name)
            servers.append(McpServer(name, url))
        return cls(
            llm=Llm(base_url=base_url, model=str(llm['model']).strip(), api_key=str(llm.get('api_key') or '').strip(),
                    temperature=_number(llm, 'temperature', 0.3, 0.0, 2.0), max_tokens=int(_number(llm, 'max_tokens', 4096, 64, 65536)),
                    timeout=_number(llm, 'timeout', 300.0, 5.0, 3600.0),
                    thinking={True: 'on', False: 'off'}[_flag(llm, 'thinking', True)] if 'thinking' in llm else ''),
            agent=Agent(max_steps=int(_number(agent, 'max_steps', 12, 1, 50)),
                        tool_result_chars=int(_number(agent, 'tool_result_chars', 24000, 1000, 400000)),
                        max_images=int(_number(agent, 'max_images', 4, 0, 16))),
            servers=tuple(servers),
            web=Web(host=str(web.get('host') or '127.0.0.1'), port=int(_number(web, 'port', 8080, 1, 65535)),
                    session_hours=_number(web, 'session_hours', 12.0, 0.1, 720.0),
                    max_concurrent=int(_number(web, 'max_concurrent', 2, 1, 16)),
                    max_question_chars=int(_number(web, 'max_question_chars', 4000, 100, 100000)),
                    trust_forwarded=_flag(web, 'trust_forwarded', False), allowed_hosts=_hosts(web),
                    **_tls(web, appconfig.config_path(path).parent)),
            users=tuple(entry for entry in config.get('users', []) if isinstance(entry, dict)),
            data_dir=str(appconfig.config_path(path).parent / 'data'),
            rhwp=_program(_table(config, 'documents'), 'rhwp', appconfig.config_path(path).parent))


def _table(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f'설정 파일의 [{name}] 은 표(table)여야 합니다')
    return value


def _tls(web: dict[str, Any], base: Path) -> dict[str, str]:
    """[web] tls_cert / tls_key. 상대 경로는 설정 파일이 있는 폴더 기준이다."""
    cert, key = str(web.get('tls_cert') or '').strip(), str(web.get('tls_key') or '').strip()
    if not cert and not key:
        return {}
    if not (cert and key):
        raise ValueError('설정 [web] tls_cert 와 tls_key 는 둘 다 있어야 합니다')
    found = {}
    for name, value in (('tls_cert', cert), ('tls_key', key)):
        path = Path(value).expanduser()
        path = path if path.is_absolute() else base / path
        if not path.is_file():
            raise ValueError(f'설정 [web] {name} 파일이 없습니다: {path}')
        found[name] = str(path)
    return found


def _program(table: dict[str, Any], name: str, base: Path) -> str:
    """[documents] 의 실행 파일 경로. 상대 경로는 설정 파일이 있는 폴더 기준이고, 적었으면 실제로 있어야 한다."""
    value = str(table.get(name) or '').strip()
    if not value:
        return ''
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    if not (path.is_file() and os.access(path, os.X_OK)):
        raise ValueError(f'설정 [documents] {name} 이 실행 파일이 아닙니다: {path}')
    return str(path)


def _hosts(web: dict[str, Any]) -> tuple[str, ...]:
    value = web.get('allowed_hosts', ['127.0.0.1', 'localhost'])
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() and '*' not in item for item in value):
        raise ValueError('설정 [web] allowed_hosts 는 호스트 이름 목록이어야 합니다(와일드카드 불가)')
    return tuple(item.strip().lower() for item in value)


def _flag(table: dict[str, Any], name: str, default: bool) -> bool:
    value = table.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f'설정 {name} 은 true 또는 false 여야 합니다')
    return value


def _number(table: dict[str, Any], name: str, default: float, low: float, high: float) -> float:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'설정 {name} 은 숫자여야 합니다')
    return min(max(float(value), low), high)
