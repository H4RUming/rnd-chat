"""설정 파일의 [llm], [agent], [web], [documents], [[mcp]], [[users]], [mail], [briefing], [signup] 을 읽어 값의 범위를 확인한다."""
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
    turn_prompt_tokens: int = 600000     # 질문 하나에 모델이 읽는 토큰의 합이 이만큼을 넘으면 더 찾지 않고 답하게 한다(헤매는 실행을 묶는다. 보통은 3만~40만)
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
    """비서: 사용자가 맡긴 일을 정해진 요일·시각에 찾아 비서 대화에 올린다(예전의 아침 브리핑을 잇는다). time 은 시각을 말하지 않은 일의 기본 시각."""
    enabled: bool = False
    time: str = '10:00'                  # 만들기 시작하는 시각(서버 시간)
    until: str = '18:00'                 # 이 시각이 지나면 그날 것은 만들지 않는다(서버가 늦게 뜬 날)
    app_url: str = ''                    # 메일에 넣을 화면 주소(예: https://example.org)
    refresh_status: str = ''             # 아침 자료 갱신의 결과 파일. 주면 갱신이 끝나기를 기다렸다가 만든다
    wait_minutes: int = 30               # 갱신을 기다리는 최대 시간
    history_days: int = 30               # 관심사를 뽑을 때 돌아보는 대화 기간
    max_interests: int = 5
    first_years: tuple[int, ...] = ()    # 첫 브리핑에서 돌아볼 연도(비우면 올해와 지난해)
    refresh_time: str = ''               # 아침 자료 갱신이 도는 시각. 주면 그보다 이르게 잡은 일도 이 시각에 맞춰 한다
    max_tasks: int = 10                  # 계정 하나가 비서에게 맡길 수 있는 일의 수


@dataclass(frozen=True)
class Signup:
    """회원가입 신청: 로그인 화면에서 신청을 받아 두고, 관리자가 검토해 등록한다(바로 가입되지 않는다)."""
    enabled: bool = False
    admin: str = ''                      # 신청을 검토하는 계정([[users]] 의 name)
    notify: str = ''                     # 신청이 오면 알릴 메일 주소(비우면 admin 계정의 email)
    max_pending: int = 30                # 검토를 기다리는 신청이 이만큼 쌓이면 더 받지 않는다
    per_hour: int = 10                   # 한 시간에 받는 신청 수(전체). 한 접속 주소는 그 절반까지


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
    signup: Signup = Signup()
    config: str = ''                     # 읽은 설정 파일의 경로(터미널 명령을 안내할 때 쓴다)

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
        users = tuple(entry for entry in config.get('users', []) if isinstance(entry, dict))
        return cls(
            llm=Llm(base_url=base_url, model=str(llm['model']).strip(), api_key=str(llm.get('api_key') or '').strip(),
                    temperature=_number(llm, 'temperature', 0.3, 0.0, 2.0), max_tokens=int(_number(llm, 'max_tokens', 4096, 64, 65536)),
                    timeout=_number(llm, 'timeout', 300.0, 5.0, 3600.0),
                    thinking={True: 'on', False: 'off'}[_flag(llm, 'thinking', True)] if 'thinking' in llm else ''),
            agent=Agent(max_steps=int(_number(agent, 'max_steps', 24, 1, 100)),
                        tool_result_chars=int(_number(agent, 'tool_result_chars', 100000, 1000, 400000)),
                        turn_result_chars=int(_number(agent, 'turn_result_chars', 300000, 10000, 2000000)),
                        turn_prompt_tokens=int(_number(agent, 'turn_prompt_tokens', 600000, 20000, 20000000)),
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
            users=users,
            data_dir=str(appconfig.config_path(path).parent / 'data'),
            rhwp=_program(_table(config, 'documents'), 'rhwp', appconfig.config_path(path).parent),
            mail=_mail(_table(config, 'mail')), briefing=_briefing(_table(config, 'briefing')), signup=_signup(_table(config, 'signup'), users),
            config=str(appconfig.config_path(path)))


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
                    max_interests=int(_number(table, 'max_interests', 5, 1, 12)), first_years=tuple(years),
                    refresh_time=_clock(table, 'refresh_time', '00:00') if table.get('refresh_time') else '', max_tasks=int(_number(table, 'max_tasks', 10, 1, 50)))


def _signup(table: dict[str, Any], users: tuple[dict[str, Any], ...]) -> Signup:
    enabled, admin, notify = _flag(table, 'enabled', False), str(table.get('admin') or '').strip(), str(table.get('notify') or '').strip()
    if enabled and admin not in [str(entry.get('name') or '').strip() for entry in users]:
        raise ValueError('설정 [signup] admin 에는 신청을 검토할 계정([[users]] 의 name)을 적어야 합니다')
    if notify and '@' not in notify:
        raise ValueError('설정 [signup] notify 는 메일 주소여야 합니다')
    return Signup(enabled=enabled, admin=admin, notify=notify, max_pending=int(_number(table, 'max_pending', 30, 1, 1000)),
                  per_hour=int(_number(table, 'per_hour', 10, 1, 1000)))


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
