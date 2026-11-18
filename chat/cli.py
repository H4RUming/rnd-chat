"""터미널에서 질문해 본다:  python -m chat.cli "질문"   (질문을 생략하면 대화형)

웹 화면 없이 모델·MCP 연결과 에이전트 루프를 확인하는 용도다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from . import agent
from .llm import Client, LlmError
from .settings import Settings
from .tools import Hub


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.load(args.config)
    hub = Hub(settings.servers)
    await hub.refresh()
    for name, reason in hub.unavailable.items():
        print(f'[연결 안 됨] {name}: {reason}', file=sys.stderr)
    print(f'[도구 {len(hub.tools)}개: {", ".join(hub.tools)}]', file=sys.stderr)
    if args.tools:
        return 0
    llm = Client(settings.llm)
    messages = [{'role': 'system', 'content': agent.system_prompt(hub)}]

    def show(event: dict) -> None:
        if event['type'] == 'tool_call':
            print(f'  → {event["name"]} {json.dumps(event["arguments"], ensure_ascii=False)}', file=sys.stderr)
        elif event['type'] == 'tool_result':
            extra = f', 그림 {event["images"]}장' if event['images'] else ''
            print(f'  ← {event["name"]} {"오류" if event["is_error"] else "결과"} {event["chars"]}자{extra}', file=sys.stderr)

    try:
        questions = [args.question] if args.question else iter(lambda: input('\n질문> ').strip(), '')
        for question in questions:
            messages.append({'role': 'user', 'content': question})
            try:
                outcome = await agent.run(llm, hub, messages, settings.agent, show)
            except LlmError as exc:
                print(f'[모델 오류] {exc}', file=sys.stderr)
                return 1
            messages = outcome.messages
            print(outcome.answer)
            note = {'max_steps': ' (도구 호출 상한에 걸려 마무리함)', 'length': ' (답이 길이 상한에서 끊김)'}.get(outcome.stopped, '')
            print(f'[왕복 {outcome.steps}회, 입력 {outcome.usage["prompt_tokens"]} / 출력 {outcome.usage["completion_tokens"]} 토큰{note}]', file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        await llm.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description='R&D 검색 채팅을 터미널에서 시험')
    parser.add_argument('question', nargs='?', default='')
    parser.add_argument('--config', type=Path, default=None)
    parser.add_argument('--tools', action='store_true', help='연결된 도구만 보여 주고 끝낸다')
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == '__main__':
    main()
