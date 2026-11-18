"""여러 MCP 서버의 도구를 한데 모아 모델에 내놓고, 모델이 부른 도구를 해당 서버에서 실행한다.

서버들은 stateless streamable HTTP 로 떠 있으므로 호출마다 연결을 새로 맺는다(같은 장비라 비용이 작고,
서버가 다시 떠도 따로 복구할 것이 없다). 도구 이름이 서버끼리 겹치면 `서버이름__도구이름` 으로 구분한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .settings import McpServer


@dataclass
class ToolResult:
    text: str
    images: list[tuple[str, str]] = field(default_factory=list)     # (mime, base64)
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str            # 모델에 보이는 이름
    server: McpServer
    remote_name: str     # 서버 안에서의 이름
    description: str
    schema: dict[str, Any]

    def spec(self) -> dict[str, Any]:
        """OpenAI 식 도구 정의."""
        return {'type': 'function', 'function': {'name': self.name, 'description': self.description,
                                                 'parameters': self.schema or {'type': 'object', 'properties': {}}}}


class Hub:
    def __init__(self, servers: tuple[McpServer, ...] | list[McpServer]):
        self.servers = list(servers)
        self.tools: dict[str, Tool] = {}
        self.instructions: dict[str, str] = {}      # 서버가 알려 준 사용 안내
        self.unavailable: dict[str, str] = {}       # 붙지 못한 서버와 이유

    async def refresh(self) -> None:
        """모든 서버의 도구 목록을 다시 받는다. 붙지 못한 서버는 빼고 계속한다."""
        tools, instructions, unavailable = {}, {}, {}
        listed: list[tuple[McpServer, Any]] = []
        for server in self.servers:
            try:
                async with _session(server) as (session, info):
                    instructions[server.name] = (info.instructions or '').strip()
                    listed += [(server, tool) for tool in (await session.list_tools()).tools]
            except Exception as exc:            # 서버 하나가 죽어 있어도 나머지로 답할 수 있어야 한다
                unavailable[server.name] = _reason(exc)
        names = [tool.name for _, tool in listed]
        for server, tool in listed:
            name = tool.name if names.count(tool.name) == 1 else f'{server.name}__{tool.name}'
            tools[name] = Tool(name, server, tool.name, (tool.description or '').strip(), tool.inputSchema or {})
        self.tools, self.instructions, self.unavailable = tools, instructions, unavailable

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self.tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f'없는 도구입니다: {name}. 쓸 수 있는 도구: {", ".join(self.tools)}', is_error=True)
        try:
            async with _session(tool.server) as (session, _):
                result = await session.call_tool(tool.remote_name, arguments)
        except Exception as exc:
            return ToolResult(f'{tool.server.name} 서버에 요청하지 못했습니다: {_reason(exc)}', is_error=True)
        texts, images = [], []
        for part in result.content:
            if part.type == 'text':
                texts.append(part.text)
            elif part.type == 'image':
                images.append((part.mimeType, part.data))
        return ToolResult('\n'.join(texts), images, bool(result.isError))


class _session:
    """연결 → 초기화 → (세션, 초기화 응답). 호출마다 열고 닫는다."""

    def __init__(self, server: McpServer):
        self.server = server

    async def __aenter__(self):
        self._transport = streamable_http_client(self.server.url)
        read, write, _ = await self._transport.__aenter__()
        try:
            self._client = ClientSession(read, write)
            session = await self._client.__aenter__()
            return session, await session.initialize()
        except BaseException:
            await self._transport.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *exc_info):
        try:
            await self._client.__aexit__(*exc_info)
        finally:
            await self._transport.__aexit__(*exc_info)


def _reason(exc: BaseException) -> str:
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:      # anyio 가 묶어 올린 예외에서 원인을 꺼낸다
        inner = inner.exceptions[0]
    return f'{type(inner).__name__}: {inner}'[:300]


def parse_arguments(raw: Any) -> dict[str, Any]:
    """모델이 준 도구 인자(JSON 글 또는 dict). 형식이 틀리면 ValueError."""
    if isinstance(raw, dict):
        return raw
    if raw in (None, ''):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError('도구 인자가 JSON 이 아닙니다') from None
    if not isinstance(value, dict):
        raise ValueError('도구 인자는 JSON 객체여야 합니다')
    return value
