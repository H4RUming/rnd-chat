"""비서에게 맡긴 일: 사용자가 비서와의 대화에서 말로 맡기면 적어 두고, 정해진 요일·시각에 찾아서 그 대화에 올린다.

- 일은 자동으로 생기지 않는다. 사용자가 맡기거나(비서가 save_task 를 부른다) 화면에서 고칠 때만 바뀐다.
- 일 하나: 제목(title), 무엇을 찾아 정리할지(what), 요일(days)과 시각(time), 알릴 조건(notify: 항상 | 새 것이 있을 때만),
  메일로도 보낼지(mail), 멈춰 둘지(paused). 그 밖에 실행 기록(last, last_ok)과 지난 실행에서 보여 준 자료 주소(seen)를 함께 둔다.
- "새 것이 있을 때만"은 두 가지로 가린다: 모델이 새 소식이 없다고 답했거나, 답이 가리킨 자료가 모두 지난 실행에서 이미 보여 준 것일 때 알리지 않는다.
- 메모: 비서가 사용자의 취향·사정을 적어 두는 짧은 글들(save_note). 일을 할 때마다, 비서와 이야기할 때마다 읽는다. 사용자가 화면에서 보고 고치고 지울 수 있다.
- 오늘: 그날 올라온 결과를 카드로 간추린 것(headline), 사용자의 반응(좋아요·별로예요, 다음 실행에 반영), 대화 기록에서 뽑아 권하는 일(suggested).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
import secrets
import time
from typing import Any, Callable

from .tools import ToolResult

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DAY_NAMES = dict(zip(DAYS, '월화수목금토일'))
_DAY_WORDS = {**{name: key for key, name in DAY_NAMES.items()}, **{f'{name}요일': key for key, name in DAY_NAMES.items()},
              'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed', 'thursday': 'thu', 'friday': 'fri', 'saturday': 'sat', 'sunday': 'sun'}
NOTIFY = ('always', 'new')
QUIET = '새 소식 없음'
MAX_SEEN = 3000                  # 일 하나가 기억하는 "이미 보여 준 자료" 수
STATE = ('last', 'last_ok', 'seen', 'created', 'feedback')      # 실행하면서 붙는 값(모델이 고치는 값이 아니다)
MAX_NOTES, NOTE_CHARS = 30, 200  # 메모의 수와 길이
REACTIONS = {'up': '좋았다', 'down': '별로였다'}

SPECS = [
    {'type': 'function', 'function': {
        'name': 'save_task',
        'description': '''사용자가 계속 챙겨 달라고 맡긴 일을 저장하거나(새로 맡길 때는 task_id 없이), 이미 맡긴 일을 고친다(task_id 를 준다).
저장해 두면 정해진 요일·시각마다 그 일을 해서 이 대화에 결과를 올린다. "매일 알려줘", "새로 뜨면 알려줘", "매주 월요일에 정리해줘" 같은 말에 쓴다.
한 번만 찾아 달라는 말에는 쓰지 않고 그 자리에서 찾아 답한다.

- title: 목록에 보일 짧은 이름(예: "강원 드론 지원사업").
- what: 그때마다 할 일을 그것만 읽고도 할 수 있게 구체적으로 적는다. 무엇을(공고인지 과제인지), 어떤 조건으로(지역·대상·분야),
  어떻게 정리할지(빼 달라고 한 것, 꼭 넣어 달라고 한 것)를 사용자가 말한 대로 담는다. 말하지 않은 조건을 지어내지 않는다.
  사용자가 목록에서 읽는 글이므로 도구 이름이나 검색 인자(query=… 같은 것)는 적지 않고 보통 말로 적는다.
- days: 하는 요일. ["mon","thu"] 처럼 준다. 매일이면 생략하거나 빈 목록, 평일이면 ["mon","tue","wed","thu","fri"].
- time: 하는 시각 "HH:MM"(24시간). 사용자가 말하지 않았으면 생략한다(기본 시각이 쓰인다).
- notify: "always" 면 할 때마다 결과를 올리고, "new" 면 새로 올라온 것이 있을 때만 올린다. "새로 뜨면", "변동 있으면" 같은 말이면 "new".
- mail: 결과를 메일로도 보낼지. 사용자가 메일로 받겠다고 했을 때만 true.
- paused: 잠시 멈춰 달라고 하면 true, 다시 해 달라고 하면 false.
고칠 때는 바꿀 값만 주면 나머지는 그대로 남는다. 사용자가 결과를 보고 "이건 빼줘", "더 자세히" 라고 하면 그 일의 what 을 고쳐 저장한다.''',
        'parameters': {'type': 'object', 'properties': {
            'task_id': {'type': 'string', 'description': '고칠 일의 id(새로 맡길 때는 생략)'},
            'title': {'type': 'string'}, 'what': {'type': 'string'},
            'days': {'type': 'array', 'items': {'type': 'string', 'enum': list(DAYS)}},
            'time': {'type': 'string', 'description': '"HH:MM"'}, 'notify': {'type': 'string', 'enum': list(NOTIFY)},
            'mail': {'type': 'boolean'}, 'paused': {'type': 'boolean'}}}}},
    {'type': 'function', 'function': {
        'name': 'delete_task',
        'description': '맡긴 일을 지운다. 사용자가 그 일을 더 하지 말라고(그만, 삭제, 취소) 했을 때만 쓴다. 잠시 멈추는 것은 save_task 의 paused 로 한다.',
        'parameters': {'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']}}},
]
NOTE_SPECS = [
    {'type': 'function', 'function': {
        'name': 'save_note',
        'description': '''사용자에 대해 앞으로도 계속 기억해 둘 것을 메모로 적는다(note_id 를 주면 그 메모를 고친다). 메모는 맡긴 일을 할 때마다, 이 대화에서 답할 때마다 읽힌다.
적을 것: 사용자의 사정(지역, 업종·기업 규모, 하는 일), 늘 빼 달라거나 꼭 넣어 달라는 것, 결과를 정리하는 방식에 대한 취향. 사용자가 그렇게 말했을 때만 적는다.
적지 않을 것: 한 가지 일에만 해당하는 조건(그 일의 what 을 고친다), 한 번 묻고 만 질문, 짐작한 것, 주민번호·비밀번호 같은 민감한 정보.
한 메모에는 한 가지만, 사용자가 읽어도 되게 사실대로 짧게 적는다(예: "경기도 화성의 제조 중소기업", "마감이 지난 공고는 보여 주지 않는다").''',
        'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'note_id': {'type': 'string', 'description': '고칠 메모의 id(새로 적을 때는 생략)'}},
                       'required': ['text']}}},
    {'type': 'function', 'function': {
        'name': 'delete_note',
        'description': '적어 둔 메모를 지운다. 사용자가 그 내용이 더는 맞지 않다고 하거나 잊어 달라고 했을 때 쓴다.',
        'parameters': {'type': 'object', 'properties': {'note_id': {'type': 'string'}}, 'required': ['note_id']}}},
]
NAMES = tuple(spec['function']['name'] for spec in SPECS + NOTE_SPECS)


class TaskError(ValueError):
    """맡긴 일을 받아들일 수 없을 때. 메시지는 모델(또는 화면)에 그대로 돌려준다."""


def _days(value: Any) -> list[str]:
    if value in (None, '', 'daily', 'everyday', '매일'):
        return []
    if value in ('weekdays', '평일'):
        return list(DAYS[:5])
    if isinstance(value, str):
        value = re.split(r'[\s,·/]+', value.strip())
    if not isinstance(value, list):
        raise TaskError('days 는 ["mon","thu"] 같은 요일 목록이어야 합니다')
    wanted = set()
    for entry in value:
        key = _DAY_WORDS.get(str(entry).strip().lower(), str(entry).strip().lower()[:3])
        if key not in DAYS:
            raise TaskError(f'days 에 모르는 요일이 있습니다: {entry}. mon tue wed thu fri sat sun 가운데서 씁니다')
        wanted.add(key)
    return [] if len(wanted) == 7 else [day for day in DAYS if day in wanted]


def _clock(value: Any, default: str) -> str:
    found = re.fullmatch(r'\s*(\d{1,2})\s*[:시]\s*(\d{1,2})?\s*분?\s*', str(value)) if value not in (None, '') else None
    if value in (None, ''):
        return default
    if not found or int(found[1]) > 23 or int(found[2] or 0) > 59:
        raise TaskError('time 은 "09:30" 같은 24시간 시각이어야 합니다')
    return f'{int(found[1]):02d}:{int(found[2] or 0):02d}'


def clean(entry: dict[str, Any], known: dict[str, Any] | None = None, default_time: str = '10:00') -> dict[str, Any]:
    """모델이나 화면이 준 값을 일 하나로 다듬는다. known 이 있으면 고치는 것이라, 주지 않은 값은 그대로 둔다."""
    if not isinstance(entry, dict):
        raise TaskError('일의 내용을 읽지 못했습니다')
    base = known or {}

    def text(key: str, limit: int) -> str:
        return ' '.join(str(entry[key]).split())[:limit] if isinstance(entry.get(key), str) and entry[key].strip() else str(base.get(key) or '')
    task = {'id': base.get('id') or secrets.token_urlsafe(6), 'title': text('title', 60), 'what': text('what', 1200),
            'days': _days(entry['days']) if 'days' in entry else list(base.get('days') or []),
            'time': _clock(entry.get('time'), str(base.get('time') or default_time)),
            'notify': entry.get('notify') if entry.get('notify') in NOTIFY else base.get('notify') or 'always',
            'mail': entry['mail'] if isinstance(entry.get('mail'), bool) else bool(base.get('mail')),
            'paused': entry['paused'] if isinstance(entry.get('paused'), bool) else bool(base.get('paused'))}
    if not task['what']:
        raise TaskError('what 에 그때마다 무엇을 찾아 정리할지 적어야 합니다')
    task['title'] = task['title'] or task['what'][:30]
    return {**{key: base[key] for key in STATE if key in base}, 'created': base.get('created') or time.time(), **task}


def when_text(task: dict[str, Any]) -> str:
    days = task.get('days') or []
    if not days:
        return f'매일 {task["time"]}'
    if days == list(DAYS[:5]):
        return f'평일 {task["time"]}'
    return f'매주 {"·".join(DAY_NAMES[day] for day in days)} {task["time"]}'


def minutes_late(task: dict[str, Any], now: datetime, earliest: str = '') -> int | None:
    """오늘 이 일을 할 시각에서 몇 분 지났는지. 오늘 하는 날이 아니면 None, 아직 때가 안 됐으면 음수.
    earliest: 그날 자료 갱신이 도는 시각. 그보다 이르게 잡은 일은 그 시각에 맞춘다(갱신된 자료로 찾게)."""
    days = task.get('days') or []
    if days and DAYS[now.weekday()] not in days:
        return None
    hours, minutes = (int(part) for part in max(str(task.get('time') or '10:00'), earliest or '00:00').split(':'))
    return now.hour * 60 + now.minute - (hours * 60 + minutes)


def clean_note(text: Any) -> str:
    cleaned = ' '.join(str(text or '').split())[:NOTE_CHARS]
    if not cleaned:
        raise TaskError('메모에 적을 글이 없습니다')
    return cleaned


def put_note(notes: list[dict[str, Any]], text: Any, note_id: str = '', by: str = 'assistant') -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """메모를 더하거나(note_id 없이) 고친 새 목록과 그 메모. by: 누가 적었는지(assistant | user)."""
    cleaned = clean_note(text)
    if note_id:
        if not any(entry['id'] == note_id for entry in notes):
            raise TaskError('그 note_id 의 메모가 없습니다')
        saved = [{**entry, 'text': cleaned, 'by': by, 'at': time.time()} if entry['id'] == note_id else entry for entry in notes]
        return saved, next(entry for entry in saved if entry['id'] == note_id)
    if any(entry['text'] == cleaned for entry in notes):
        return notes, next(entry for entry in notes if entry['text'] == cleaned)
    if len(notes) >= MAX_NOTES:
        raise TaskError(f'메모는 {MAX_NOTES}개까지입니다. 비슷한 메모를 하나로 합치거나 맞지 않게 된 것을 지운 뒤 적으세요')
    entry = {'id': secrets.token_urlsafe(5), 'text': cleaned, 'by': by, 'at': time.time()}
    return [*notes, entry], entry


def notes_text(notes: list[dict[str, Any]], ids: bool = False) -> str:
    return '\n'.join(f'- {"note_id " + entry["id"] + ": " if ids else ""}{entry["text"]}' for entry in notes)


def react(task: dict[str, Any], day: str, value: str) -> dict[str, Any]:
    """그날 결과에 대한 사용자의 반응을 적은 일(같은 것을 다시 누르면 지운다). 다음에 할 때 반영한다."""
    kept = [entry for entry in task.get('feedback') or [] if entry.get('day') != day]
    again = any(entry.get('day') == day and entry.get('value') == value for entry in task.get('feedback') or [])
    return {**task, 'feedback': (kept if again else [*kept, {'day': day, 'value': value}])[-5:]}


def headline(answer: str, limit: int = 260) -> str:
    """결과의 맨 위 두세 줄(카드에 싣는다). 표·제목 기호와 출처 번호는 뺀다."""
    lines = []
    for line in (answer or '').strip().split('\n'):
        line = line.strip()
        if line.startswith('|') or (lines and (line.startswith('#') or re.fullmatch(r'-{3,}', line))):
            break
        line = re.sub(r'\[\d{1,3}(?:\s*,\s*\d{1,3})*\]', '', re.sub(r'^#{1,6}\s*|\*\*|`', '', line)).strip()
        if line:
            lines.append(re.sub(r'^[-*•]\s+', '', line))
        if sum(len(kept) for kept in lines) >= limit or len(lines) >= 4:
            break
    text = re.sub(r'^(오늘\s*)?(가장\s*)?눈여겨\s*볼\s*것\s*[:：—-]?\s*', '', ' '.join(lines))      # 모델이 머리말로 붙이곤 하는 말
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def suggested(item: dict[str, Any], default_time: str) -> dict[str, Any]:
    """대화 기록에서 뽑아 권한 주제를, 사용자가 받아들였을 때 맡긴 일로 만든다."""
    looking = {'공고': '지원사업 공고', '과제': '국가연구개발 과제', '둘 다': '지원사업 공고와 국가연구개발 과제'}.get(str(item.get('kind')), '지원사업 공고')
    what = f'{item["topic"]}: 새로 올라온 {looking}를 찾아 정리한다.' + (f' 조건: {item["conditions"]}.' if item.get('conditions') else '')
    return clean({'title': str(item['topic'])[:40], 'what': what, 'notify': 'new'}, default_time=default_time)


def instruction(task: dict[str, Any], day: str, since: str = '', notes: list[dict[str, Any]] | None = None) -> str:
    """맡긴 일을 하게 하는 글(모델에 사용자 차례로 준다). since: 지난번에 한 날(없으면 처음). notes: 사용자에 대해 적어 둔 메모."""
    today = date.fromisoformat(day)
    soon = (today + timedelta(days=7)).isoformat()
    if since:
        scope = f'''지난번({since})에 한 번 정리했습니다. 이번에는 그 뒤의 변화를 다룹니다.
- 공고: {since} 이후 새로 등록된 것(posted_from 에 {since}), 그리고 오늘부터 {soon} 사이에 마감되는 것.
- 과제: 올해 등록된 것 가운데 맞는 것을 최근 것부터.
- 맡긴 내용이 범위를 따로 정했으면(예: "매번 전체 현황") 그 범위를 따릅니다.'''
    else:
        scope = f'''처음 하는 일입니다. 맡긴 내용에 기간이 없으면 지금 신청할 수 있는 것과 최근 1년 사이의 것을 정리합니다.
오늘부터 {soon} 사이에 마감되는 것은 따로 짚습니다.'''
    about = ('\n사용자에 대해 적어 둔 것(결과에 반영합니다):\n' + notes_text(notes) + '\n') if notes else ''
    reaction = (task.get('feedback') or [{}])[-1]
    if reaction.get('value') == 'down':
        about += f'\n지난 결과({reaction["day"]})를 사용자가 별로라고 했습니다. 같은 구성을 되풀이하지 말고, 맡긴 내용에 꼭 맞는 것만 더 간추려 싣습니다.\n'
    elif reaction.get('value') == 'up':
        about += f'\n지난 결과({reaction["day"]})를 사용자가 좋다고 했습니다. 그 구성을 유지합니다.\n'
    quiet = (f'\n- 새로 올라온 것도 마감이 가까운 것도 없으면, 다른 말 없이 "{QUIET}" 한 줄만 씁니다.' if task.get('notify') == 'new' and since else
             f'\n- 알릴 것이 없으면 "{QUIET}" 한 줄만 씁니다.')
    return f'''[맡긴 일 실행] 오늘은 {day}({DAY_NAMES[DAYS[today.weekday()]]}요일)입니다. 사용자가 비서에게 맡겨 둔 일을 할 차례입니다.

맡긴 일: {task["title"]}
내용: {task["what"]}
{about}
{scope}

쓰는 법:
- 도구로 직접 찾습니다. 결과가 많으면 전체 확인(review_all)으로 끝까지 본 뒤 고릅니다.
- 맨 위에 가장 눈여겨볼 것을 두세 줄로 먼저 적고(새로 올라온 것, 마감이 가장 가까운 것), 그 아래에 건수를 밝히는 한 문장과 표를 둡니다
  (공고는 이름·지역·소관·신청 마감, 과제는 이름·수행기관·부처·기간·연구비). 표에는 15건까지 싣습니다.
- 맡긴 내용에 맞는 자료가 없으면 관계없는 자료로 채우지 않습니다.{quiet}
- 인사, 맺음말, 되묻기는 쓰지 않습니다.'''


_NO_NEWS = re.compile(r'새\s*소식[은이]?\s*(없음|없습니다)|새로\s*(뜬|올라온|등록된|나온)[^.\n|]{0,24}(없음|없습니다)')


def is_quiet(answer: str) -> bool:
    """모델이 알릴 것이 없다고 답했는가. 시킨 대로 한 줄만 쓴 경우와, 자료를 하나도 가리키지 않고 표 없이 짧게 "새로 뜬 것 없음"이라고 쓴 경우."""
    text = (answer or '').strip()
    if re.sub(r'[\s.*`"\']', '', text) == QUIET.replace(' ', ''):
        return True
    return len(text) < 600 and '|' not in text and not re.search(r'\[\d{1,3}', text) and bool(_NO_NEWS.search(text[:300]))


def nothing_new(task: dict[str, Any], answer: str, cited: list[str]) -> bool:
    """새 것이 있을 때만 알리는 일에서, 이번 결과를 알리지 않아도 되는가."""
    if task.get('notify') != 'new':                    # 할 때마다 알리는 일은 "새 소식 없음"도 그대로 올린다
        return False
    seen = set(task.get('seen') or [])
    return is_quiet(answer) or (bool(task.get('last_ok')) and bool(cited) and all(url in seen for url in cited))


def remember(task: dict[str, Any], cited: list[str]) -> list[str]:
    """이번에 보여 준 자료를 더한 "이미 보여 준 자료" 목록(오래된 것부터 밀려난다)."""
    return list(dict.fromkeys([*(task.get('seen') or []), *cited]))[-MAX_SEEN:]


def from_interests(interests: list[dict[str, Any]], at: str, mail: bool, paused: bool) -> dict[str, Any] | None:
    """예전 아침 브리핑의 주제들을 맡긴 일 하나로 옮긴다."""
    lines = [f'{number}. {item["topic"]} — 찾는 것: {item.get("kind") or "공고"}' + (f', 조건: {item["conditions"]}' if item.get('conditions') else '')
             for number, item in enumerate(interests, 1) if isinstance(item, dict) and item.get('topic')]
    if not lines:
        return None
    return clean({'title': '아침 브리핑', 'what': '다음 주제마다 새 소식을 찾아, 주제별로 굵은 제목과 표로 정리한다. ' + ' '.join(lines),
                  'time': at, 'notify': 'always', 'mail': mail, 'paused': paused}, default_time=at)


def shown(task: dict[str, Any], running: bool = False) -> dict[str, Any]:
    """화면에 보내는 일 하나(기억해 둔 주소 목록 같은 것은 빼고)."""
    last = task.get('last') or {}
    return {**{key: task.get(key) for key in ('id', 'title', 'what', 'days', 'time', 'notify', 'mail', 'paused')}, 'when': when_text(task),
            'running': running, 'last': {key: last.get(key) for key in ('day', 'status', 'at', 'note')} if last else None,
            'feedback': {entry['day']: entry['value'] for entry in task.get('feedback') or []}}


def note(tasks: list[dict[str, Any]], limit: int, mail_ok: bool, default_time: str, now: datetime | None = None,
         notes: list[dict[str, Any]] | None = None) -> str:
    """비서 대화에서 모델에 덧붙이는 안내: 여기가 어떤 대화인지, 지금 맡은 일이 무엇인지."""
    now = now or datetime.now()
    listing = '\n'.join(
        f'- task_id {task["id"]}: "{task["title"]}" · {when_text(task)} · {"새 것이 있을 때만 알림" if task["notify"] == "new" else "할 때마다 알림"}'
        f'{" · 메일로도 보냄" if task.get("mail") else ""}{" · 멈춰 둠" if task.get("paused") else ""}\n  내용: {task["what"]}' for task in tasks) or '- (아직 없음)'
    return f'''

여기는 사용자의 비서 대화입니다. 지금은 {now.strftime("%Y-%m-%d")}({DAY_NAMES[DAYS[now.weekday()]]}요일) {now.strftime("%H:%M")}입니다.
- 사용자가 계속 챙겨 달라고 하는 일은 save_task 로 맡아 두고, 무엇을 언제 알려 줄지 한두 문장으로 확인해 줍니다. 시각을 말하지 않았으면 {default_time} 입니다.
- 맡아 둔 일의 결과는 이 대화에 "[맡긴 일 실행]"으로 시작하는 차례 뒤에 올라옵니다. 사용자가 그 결과를 보고 바꿔 달라고 하면 그 일의 what 을 고쳐 저장합니다.
- 한 번만 찾아 달라는 말이나 보통 질문에는 평소처럼 도구로 찾아 답합니다. 일을 저장하지 않습니다.
- 맡을 수 있는 일은 {limit}개까지입니다.{"" if mail_ok else " 이 계정에는 메일 주소가 없어 결과를 메일로는 보낼 수 없습니다."}
- task_id 는 답에 쓰지 않고 일의 이름으로 말합니다.
- 아래 목록이 지금 실제로 맡아 둔 일의 전부입니다. 앞선 대화에서 저장했다고 한 일이라도 목록에 없으면 사용자가 지운 것이니, 다시 맡기면 새로 저장합니다.
  저장했다거나 고쳤다고 말하려면 이번 차례에 save_task 를 불러 성공해야 합니다.

- 사용자가 앞으로도 계속 적용할 자기 사정이나 취향을 말하면 save_note 로 메모해 둡니다. 한 가지 일에만 해당하면 메모가 아니라 그 일의 what 을 고칩니다.
  메모는 사용자가 화면에서 볼 수 있습니다. 답할 때 메모를 반영하되 note_id 는 답에 쓰지 않습니다.

지금 맡아 둔 일:
{listing}

지금 적어 둔 메모:
{notes_text(notes or [], ids=True) or "- (아직 없음)"}'''


class Desk:
    """비서 대화에서만 모델에 주는 도구(맡긴 일 저장·지우기). load/save/remove 는 그 계정의 일을 읽고 쓰는 함수다."""

    def __init__(self, load: Callable[[], list[dict[str, Any]]], save: Callable[[dict[str, Any]], None], remove: Callable[[str], None],
                 limit: int = 10, mail_ok: bool = False, default_time: str = '10:00',
                 notes: Callable[[], list[dict[str, Any]]] | None = None, keep_notes: Callable[[list[dict[str, Any]]], None] | None = None, earliest: str = ''):
        self.load, self.save, self.remove, self.limit, self.mail_ok, self.default_time = load, save, remove, limit, mail_ok, default_time
        self.earliest = earliest                             # 그날 자료 갱신이 도는 시각(minutes_late 참고)
        self.notes, self.keep_notes = notes, keep_notes      # 메모를 읽고 쓰는 함수(없으면 메모 도구를 주지 않는다)

    def specs(self) -> list[dict[str, Any]]:
        return SPECS + (NOTE_SPECS if self.notes and self.keep_notes else [])

    def _note(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        notes, wanted = self.notes(), str(arguments.get('note_id') or '').strip()
        try:
            if name == 'delete_note':
                found = next((entry for entry in notes if entry['id'] == wanted), None)
                if found is None:
                    raise TaskError('그 note_id 의 메모가 없습니다')
                self.keep_notes([entry for entry in notes if entry['id'] != wanted])
                return ToolResult(json.dumps({'deleted': found['text']}, ensure_ascii=False), summary={'note': found['text'][:60], 'deleted': True})
            saved, entry = put_note(notes, arguments.get('text'), wanted)
        except TaskError as exc:
            listing = notes_text(notes, ids=True) or '없음'
            return ToolResult(f'{exc}. 지금 적어 둔 메모:\n{listing}', is_error=True)
        self.keep_notes(saved)
        return ToolResult(json.dumps({'noted': entry['text'], 'note_id': entry['id']}, ensure_ascii=False), summary={'note': entry['text'][:60]})

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult | None:
        if name not in NAMES:
            return None
        if name in ('save_note', 'delete_note'):
            return self._note(name, arguments) if self.notes and self.keep_notes else None
        known = self.load()
        wanted = str(arguments.get('task_id') or '').strip()
        found = next((task for task in known if task['id'] == wanted), None)
        listing = ', '.join(f'{task["id"]}("{task["title"]}")' for task in known) or '없음'
        if wanted and found is None:
            return ToolResult(f'그 task_id 의 일이 없습니다. 지금 맡아 둔 일: {listing}', is_error=True)
        if name == 'delete_task':
            if found is None:
                return ToolResult(f'지울 일의 task_id 를 주세요. 지금 맡아 둔 일: {listing}', is_error=True)
            self.remove(found['id'])
            return ToolResult(json.dumps({'deleted': found['title']}, ensure_ascii=False), summary={'task': found['title'], 'deleted': True})
        if found is None and len(known) >= self.limit:
            return ToolResult(f'맡을 수 있는 일은 {self.limit}개까지입니다. 지금 맡아 둔 일: {listing}. 하나를 지우거나 고치겠느냐고 사용자에게 물으세요.', is_error=True)
        try:
            task = clean(arguments, found, self.default_time)
        except TaskError as exc:
            return ToolResult(f'{exc}. 고쳐서 다시 호출하세요.', is_error=True)
        remark = ''
        if task['mail'] and not self.mail_ok:
            task['mail'], remark = False, ' 이 계정에는 메일 주소가 없어 메일로는 보내지 못합니다(대화에만 올립니다). 사용자에게 그렇게 알리세요.'
        self.save(task)
        late = minutes_late(task, datetime.now(), self.earliest)
        if not task['paused'] and late is not None and late >= 0 and (task.get('last') or {}).get('day') != datetime.now().strftime('%Y-%m-%d'):
            remark += ' 오늘 할 시각이 이미 지나서, 잠시 뒤 첫 결과가 이 대화에 올라옵니다. 사용자에게 그렇게 알리세요.'
        result = {'saved': task['title'], 'task_id': task['id'], 'schedule': when_text(task), 'notify': task['notify'], 'mail': task['mail'],
                  'paused': task['paused'], 'note': '저장했습니다. 무엇을 언제 알려 줄지 한두 문장으로 확인해 주세요.' + remark}
        return ToolResult(json.dumps(result, ensure_ascii=False), summary={'task': task['title'], 'when': when_text(task)})
