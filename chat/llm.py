"""OpenAI 호환 채팅 API(…/v1/chat/completions) 호출. 모델 서버에는 추론 요청만 보낸다."""
from __future__ import annotations

from typing import Any

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

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """답 하나를 받는다. 돌려주는 것: {'content': 글, 'tool_calls': [{'id', 'name', 'arguments'}], 'finish_reason', 'usage'}."""
        body: dict[str, Any] = {'model': self.settings.model, 'messages': messages, 'temperature': self.settings.temperature,
                                'max_tokens': self.settings.max_tokens}
        if tools:
            body['tools'] = tools
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
