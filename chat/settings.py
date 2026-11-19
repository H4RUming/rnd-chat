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
    max_steps: int = 24
    tool_result_chars: int = 100000
    sweep_max_items: int = 5000          # 전체 확인(review_all)이 한 번에 넘겨 보는 자료 수의 상한
    sweep_seconds: float = 600.0         # 전체 확인 한 번에 쓰는 시간의 상한
    turn_result_chars: int = 300000      # 질문 하나에서 모델에 넘기는 도구 결과의 합(모델이 한 번에 읽을 수 있는 양을 넘지 않게)
    max_images: int = 4
    thinking_steps: int = 1              # [llm] thinking 이 켜져 있을 때, 질문마다 앞의 몇 단계까지 속생각을 쓸지(0 이면 모든 단계)
    thinking_tokens: int = 4000          # 속생각을 쓰는 단계의 길이 상한(속생각 + 답). 넘으면 생각 없이 다시 묻는다


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
class Mail:
    """메일 보내기(SMTP). username 이 비어 있으면 보내지 않는다."""
    host: str = 'smtp.gmail.com'
    port: int = 587
    username: str = ''
    password: str = ''
    sender_name: str = ''


@dataclass(frozen=True)
class Briefing:
    """아침 브리핑: 계정마다 대화 기록에서 관심사를 뽑아, 정해진 시각에 새 소식을 찾아 정리해 준다."""
    enabled: bool = False
    time: str = '10:00'                  # 만들기 시작하는 시각(서버 시간)
    until: str = '18:00'                 # 이 시각이 지나면 그날 것은 만들지 않는다(서버가 늦게 뜬 날)
    app_url: str = ''                    # 메일에 넣을 화면 주소(예: https://example.org)
    refresh_status: str = ''             # 아침 자료 갱신의 결과 파일. 주면 갱신이 끝나기를 기다렸다가 만든다
    wait_minutes: int = 30               # 갱신을 기다리는 최대 시간
    history_days: int = 30               # 관심사를 뽑을 때 돌아보는 대화 기간
    max_interests: int = 5
    first_years: tuple[int, ...] = ()    # 첫 브리핑에서 돌아볼 연도(비우면 올해와 지난해)


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
    mail: Mail = Mail()
    briefing: Briefing = Briefing()

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
            agent=Agent(max_steps=int(_number(agent, 'max_steps', 24, 1, 100)),
                        tool_result_chars=int(_number(agent, 'tool_result_chars', 100000, 1000, 400000)),
                        turn_result_chars=int(_number(agent, 'turn_result_chars', 300000, 10000, 2000000)),
                        sweep_max_items=int(_number(agent, 'sweep_max_items', 5000, 100, 200000)),
                        sweep_seconds=_number(agent, 'sweep_seconds', 600.0, 10.0, 7200.0),
                        max_images=int(_number(agent, 'max_images', 4, 0, 16)),
                        thinking_steps=int(_number(agent, 'thinking_steps', 1, 0, 50)),
                        thinking_tokens=int(_number(agent, 'thinking_tokens', 4000, 256, 65536))),
            servers=tuple(servers),
            web=Web(host=str(web.get('host') or '127.0.0.1'), port=int(_number(web, 'port', 8080, 1, 65535)),
                    session_hours=_number(web, 'session_hours', 12.0, 0.1, 720.0),
                    max_concurrent=int(_number(web, 'max_concurrent', 2, 1, 16)),
                    max_question_chars=int(_number(web, 'max_question_chars', 4000, 100, 100000)),
                    trust_forwarded=_flag(web, 'trust_forwarded', False), allowed_hosts=_hosts(web),
                    **_tls(web, appconfig.config_path(path).parent)),
            users=tuple(entry for entry in config.get('users', []) if isinstance(entry, dict)),
            data_dir=str(appconfig.config_path(path).parent / 'data'),
            rhwp=_program(_table(config, 'documents'), 'rhwp', appconfig.config_path(path).parent),
            mail=_mail(_table(config, 'mail')), briefing=_briefing(_table(config, 'briefing')))


def _clock(table: dict[str, Any], name: str, default: str) -> str:
    value = str(table.get(name, default)).strip()
    hours, _, minutes = value.partition(':')
    if not (hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60):
        raise ValueError(f'설정 {name} 은 "10:00" 같은 시각이어야 합니다')
    return f'{int(hours):02d}:{int(minutes):02d}'


def _mail(table: dict[str, Any]) -> Mail:
    return Mail(host=str(table.get('host') or 'smtp.gmail.com').strip(), port=int(_number(table, 'port', 587, 1, 65535)),
                username=str(table.get('username') or '').strip(), password=str(table.get('password') or '').replace(' ', ''),
                sender_name=str(table.get('sender_name') or '').strip())


def _briefing(table: dict[str, Any]) -> Briefing:
    years = table.get('first_years', [])
    if not isinstance(years, list) or not all(isinstance(year, int) and 2000 <= year <= 2100 for year in years):
        raise ValueError('설정 [briefing] first_years 는 연도 목록이어야 합니다')
    app_url = str(table.get('app_url') or '').strip().rstrip('/')
    if app_url and urlsplit(app_url).scheme not in ('http', 'https'):
        raise ValueError('설정 [briefing] app_url 은 주소여야 합니다')
    return Briefing(enabled=_flag(table, 'enabled', False), time=_clock(table, 'time', '10:00'), until=_clock(table, 'until', '18:00'),
                    app_url=app_url, refresh_status=str(table.get('refresh_status') or '').strip(),
                    wait_minutes=int(_number(table, 'wait_minutes', 30, 0, 600)), history_days=int(_number(table, 'history_days', 30, 1, 365)),
                    max_interests=int(_number(table, 'max_interests', 5, 1, 12)), first_years=tuple(years))


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
