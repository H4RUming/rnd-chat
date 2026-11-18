"""설정 파일의 [llm], [agent], [[mcp]] 를 읽어 값의 범위를 확인한다."""
from __future__ import annotations

from dataclasses import dataclass
import os
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
    timeout: float = 300.0


@dataclass(frozen=True)
class Agent:
    max_steps: int = 12
    tool_result_chars: int = 24000
    max_images: int = 4


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str


@dataclass(frozen=True)
class Settings:
    llm: Llm
    agent: Agent
    servers: tuple[McpServer, ...]

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
                    timeout=_number(llm, 'timeout', 300.0, 5.0, 3600.0)),
            agent=Agent(max_steps=int(_number(agent, 'max_steps', 12, 1, 50)),
                        tool_result_chars=int(_number(agent, 'tool_result_chars', 24000, 1000, 400000)),
                        max_images=int(_number(agent, 'max_images', 4, 0, 16))),
            servers=tuple(servers))


def _table(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f'설정 파일의 [{name}] 은 표(table)여야 합니다')
    return value


def _number(table: dict[str, Any], name: str, default: float, low: float, high: float) -> float:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'설정 {name} 은 숫자여야 합니다')
    return min(max(float(value), low), high)
