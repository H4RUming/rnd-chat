# R&D 검색 채팅

여러 MCP 서버를 묶어, 모델이 필요할 때 자료를 직접 찾아 답하는 채팅입니다. 지원사업 공고(기업마당)와 국가R&D 과제(NTIS)를 찾고 취합하는 데 씁니다.

MCP 서버들은 각자 다른 저장소에 있고 따로 띄웁니다. 이 저장소는 그 서버들에 붙는 쪽만 다룹니다.

| 서버 | 기본 주소 | 하는 일 |
|---|---|---|
| bizinfo | `http://127.0.0.1:8765/mcp` | 진행 중인 지원사업 공고와 첨부 본문 검색 |
| linkreader | `http://127.0.0.1:8766/mcp` | 공고에 걸린 외부 링크 읽기 |
| history | `http://127.0.0.1:8767/mcp` | 마감 포함 공고 이력 검색과 집계 |
| ntis | `http://127.0.0.1:8768/mcp` | 국가R&D 과제·성과·연구보고서 검색과 집계 |

## 지금 되는 것

- 설정 파일로 모델 서버와 MCP 서버 목록을 정합니다.
- 모델이 도구를 부르면 해당 서버에서 실행해 결과를 돌려주고, 글로 답할 때까지 되풀이합니다(에이전트 루프).
- 터미널에서 질문해 볼 수 있습니다.

웹 화면, 로그인, 외부 공개는 아직 없습니다.

## 설치와 실행

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp config.example.toml config.toml && chmod 600 config.toml   # config.toml 은 git 에 올라가지 않는다
# config.toml 의 [llm] base_url 을 모델 서버 주소로 고친다
.venv/bin/python -m chat.cli --tools                           # 붙은 MCP 서버와 도구 확인
.venv/bin/python -m chat.cli "최근 3년 인공지능 반도체 과제가 연도별로 몇 건이야?"
.venv/bin/python -m chat.cli                                   # 대화형
```

모델 서버는 OpenAI 호환 API(`/v1/chat/completions`)와 도구 호출(`tool_calls`)을 지원해야 합니다. 모델 서버에는 추론 요청만 보냅니다.

## 동작 방식과 상한

- MCP 서버에는 호출마다 연결을 새로 맺습니다. 서버 하나가 죽어 있으면 그 서버만 빼고 답하며, 모델에게 그 사실을 알립니다.
- 도구 이름이 서버끼리 겹치면 `서버이름__도구이름`으로 구분합니다.
- 질문 하나에 도구 왕복은 `max_steps`회까지입니다. 넘으면 도구 없이 지금까지 찾은 것으로 마무리하게 합니다.
- 도구 결과는 `tool_result_chars`자에서 자르고, 잘랐다는 사실과 전체 길이를 모델에 알립니다.
- 도구가 돌려준 그림(스캔 PDF, 화면 사진)은 `max_images`장까지 모델에 보입니다.
- 도구가 돌려준 내용은 자료로만 다룹니다. 그 안에 지시처럼 보이는 글이 있어도 따르지 않도록 안내문에 적어 둡니다.

## 구성

| 파일 | 내용 |
|---|---|
| `chat/settings.py` | 설정 읽기와 값 범위 확인 |
| `chat/tools.py` | MCP 서버 연결, 도구 목록, 도구 실행 |
| `chat/llm.py` | 모델 API 호출 |
| `chat/agent.py` | 에이전트 루프와 안내문 |
| `chat/cli.py` | 터미널 시험용 |

## 테스트

```bash
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
```

테스트는 가짜 모델과 가짜 도구로 돌며 모델 서버와 MCP 서버가 필요 없습니다.
