"""OpenAI 호환 채팅 API(…/v1/chat/completions) 호출. 모델 서버에는 추론 요청만 보낸다."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx

from .settings import Llm


class LlmError(Exception):
    pass


class Client:
    def __init__(self, settings: Llm, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        headers = {'Authorization': f'Bearer {settings.api_key}'} if settings.api_key else {}
        self._http = httpx.AsyncClient(base_url=settings.base_url, headers=headers, transport=transport,
                                       timeout=httpx.Timeout(settings.timeout, connect=10.0))

    async def close(self) -> None:
        await self._http.aclose()

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                       on_text: Callable[[str], Awaitable[None] | None] | None = None, thinking: bool | None = None) -> dict[str, Any]:
        """답 하나를 받는다. 돌려주는 것: {'content': 글, 'tool_calls': [{'id', 'name', 'arguments'}], 'finish_reason', 'usage'}.

        on_text 를 주면 스트리밍으로 받아, 글이 조각조각 올 때마다 on_text(조각)을 부른다. 돌려주는 값은 같다.
        thinking: 답하기 전에 속으로 따져 보게 할지(생각하는 모델에서만 뜻이 있다). None 이면 설정을, 설정도 없으면 모델 기본을 따른다.
        """
        body: dict[str, Any] = {'model': self.settings.model, 'messages': messages, 'temperature': self.settings.temperature,
                                'max_tokens': self.settings.max_tokens}
        if thinking is None and self.settings.thinking:
            thinking = self.settings.thinking == 'on'
        if thinking is not None:                         # vLLM 이 대화 틀(chat template)에 넘겨 주는 값. 모르는 모델은 무시한다
            body['chat_template_kwargs'] = {'enable_thinking': thinking}
        if tools:
            body['tools'] = tools
        if on_text is not None:
            return await self._stream(body, on_text)
        try:
            response = await self._http.post('/chat/completions', json=body)
        except httpx.TimeoutException:
            raise LlmError(f'모델 서버가 {self.settings.timeout:.0f}초 안에 답하지 않았습니다') from None
        except httpx.HTTPError as exc:
            raise LlmError(f'모델 서버에 연결하지 못했습니다: {type(exc).__name__}') from None
        if response.status_code != 200:
            raise LlmError(f'모델 서버 오류 HTTP {response.status_code}: {response.text[:300]}')
        try:
            data = response.json()
            choice = data['choices'][0]
            message = choice['message']
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError('모델 서버의 답을 해석하지 못했습니다: ' + response.text[:200]) from None
        calls = [{'id': call.get('id') or f'call_{index}', 'name': (call.get('function') or {}).get('name', ''),
                  'arguments': (call.get('function') or {}).get('arguments', '')}
                 for index, call in enumerate(message.get('tool_calls') or [])]
        return {'content': message.get('content') or '', 'tool_calls': calls, 'finish_reason': choice.get('finish_reason'),
                'usage': data.get('usage') or {}}

    async def _stream(self, body: dict[str, Any], on_text: Callable[[str], Awaitable[None] | None]) -> dict[str, Any]:
        body = {**body, 'stream': True, 'stream_options': {'include_usage': True}}
        content, calls, finish, usage = [], {}, None, {}
        try:
            async with self._http.stream('POST', '/chat/completions', json=body) as response:
                if response.status_code != 200:
                    raise LlmError(f'모델 서버 오류 HTTP {response.status_code}: {(await response.aread())[:300].decode("utf-8", "replace")}')
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    usage = chunk.get('usage') or usage
                    for choice in chunk.get('choices') or []:
                        delta = choice.get('delta') or {}
                        if delta.get('content'):
                            content.append(delta['content'])
                            result = on_text(delta['content'])
                            if result is not None:
                                await result
                        for call in delta.get('tool_calls') or []:      # 도구 호출은 이름 한 번, 인자는 조각으로 온다
                            slot = calls.setdefault(call.get('index', 0), {'id': '', 'name': '', 'arguments': ''})
                            function = call.get('function') or {}
                            slot['id'] = call.get('id') or slot['id']
                            slot['name'] += function.get('name') or ''
                            slot['arguments'] += function.get('arguments') or ''
                        finish = choice.get('finish_reason') or finish
        except httpx.TimeoutException:
            raise LlmError(f'모델 서버가 {self.settings.timeout:.0f}초 안에 답하지 않았습니다') from None
        except httpx.HTTPError as exc:
            raise LlmError(f'모델 서버에 연결하지 못했습니다: {type(exc).__name__}') from None
        ordered = [calls[index] for index in sorted(calls)]
        for index, call in enumerate(ordered):
            call['id'] = call['id'] or f'call_{index}'
        return {'content': ''.join(content), 'tool_calls': ordered, 'finish_reason': finish, 'usage': usage}
