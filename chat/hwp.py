"""한글 문서(HWP·HWPX)의 원본 고치기: rhwp 엔진(https://github.com/edwardkim/rhwp)의 명령을 불러 쓴다.

엔진은 별도 프로세스로, 임시 폴더 안에서, 빈 환경과 제한 시간을 두고 돌린다. 문서에서 온 글은 인자 목록으로만 넘기고
셸을 거치지 않는다. 고치기 한 건마다 명령을 한 번 부르고(한 번에 0.05초쯤), 실패한 건은 건너뛰고 계속한다.

좌표: 표 번호와 칸의 행·열은 엔진의 export-tables 와 같다(본문의 표, 0 부터). 문단 번호 p 는 문서의 모든 구역을
이어서 0 부터 센 것이고, 엔진에 넘길 때 (구역, 구역 안 문단)으로 바꾼다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .editing import MAX_LINES, OPS, EditError, _clean, _integer, _lines

TIMEOUT = 60.0
_engine_path = ''
_PARAGRAPH = re.compile(r'^--- 문단 (\d+)\.(\d+) --- cc=\d+, text_len=(\d+), controls=(\d+)')


def configure(path: str) -> None:
    """설정의 [documents] rhwp(엔진 실행 파일 경로). 비워 두면 PATH 에서 찾는다."""
    global _engine_path
    _engine_path = path or ''


def engine() -> str | None:
    if _engine_path:
        return _engine_path if os.access(_engine_path, os.X_OK) else None
    return shutil.which('rhwp')


def available() -> bool:
    return engine() is not None


def _run(work: Path, *arguments: str) -> tuple[int, str, str]:
    program = engine()
    if program is None:
        raise EditError('서버에 한글 문서 엔진(rhwp)이 없어 HWP·HWPX 는 고칠 수 없습니다')
    try:
        done = subprocess.run([program, *arguments], cwd=str(work), capture_output=True, timeout=TIMEOUT, check=False,
                              env={'PATH': '/usr/bin:/bin', 'HOME': str(work), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
    except subprocess.TimeoutExpired:
        raise EditError('한글 문서를 처리하는 데 시간이 너무 오래 걸립니다') from None
    return done.returncode, done.stdout.decode('utf-8', 'replace'), done.stderr.decode('utf-8', 'replace')


def _json(work: Path, *arguments: str) -> dict[str, Any]:
    code, out, err = _run(work, *arguments, '--json')
    if code != 0:
        raise EditError(_message(err) or '한글 문서를 읽지 못했습니다(손상되었거나 암호가 걸려 있습니다)')
    try:
        value = json.loads(out)
    except ValueError:
        raise EditError('한글 문서 엔진의 응답을 읽지 못했습니다') from None
    if not isinstance(value, dict):
        raise EditError('한글 문서 엔진의 응답을 읽지 못했습니다')
    return value


def _message(stderr: str) -> str:
    """엔진이 낸 오류 글에서 사람이 읽을 줄만 고른다."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip().startswith('오류:')]
    return ' '.join(line[3:].strip() for line in lines)[:300]


def _paragraphs(work: Path, name: str) -> list[dict[str, Any]]:
    """문서의 모든 문단(구역 순서대로). 엔진의 구조 덤프에서 문단 머리줄과 글을 읽는다."""
    code, out, err = _run(work, 'dump', name)
    if code != 0:
        raise EditError(_message(err) or '한글 문서를 읽지 못했습니다(손상되었거나 암호가 걸려 있습니다)')
    found: list[dict[str, Any]] = []
    lines = out.splitlines()
    for index, line in enumerate(lines):
        match = _PARAGRAPH.match(line)
        if not match:
            continue
        text = ''
        if index + 1 < len(lines) and lines[index + 1].lstrip().startswith('텍스트:'):
            raw = lines[index + 1].split('텍스트:', 1)[1].strip()
            text = raw[1:-1] if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"' else ''
        found.append({'section': int(match.group(1)), 'paragraph': int(match.group(2)), 'length': int(match.group(3)),
                      'controls': int(match.group(4)), 'text': text})
    return found


def to_pdf(data: bytes, kind: str) -> bytes:
    """한글 문서를 엔진의 조판으로 PDF 로 그린다(미리보기용). 고칠 때와 같은 엔진이라 고친 결과가 그대로 보인다."""
    with tempfile.TemporaryDirectory(prefix='rndchat-hwp-') as folder:
        work, name = Path(folder), f'in.{kind}'
        (work / name).write_bytes(data)
        code, _, err = _run(work, 'export-pdf', name, '-o', 'out.pdf')
        result = work / 'out.pdf'
        if code != 0 or not result.is_file() or result.stat().st_size < 100:
            raise EditError(_message(err) or '한글 문서를 PDF 로 그리지 못했습니다')
        return result.read_bytes()


def _cells(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{'row': int(cell.get('row', 0)), 'col': int(cell.get('col', 0)), 'rowSpan': int(cell.get('rowSpan', 1)), 'colSpan': int(cell.get('colSpan', 1)),
             'text': str(cell.get('text') or ''), 'nested': bool(cell.get('nested'))} for cell in cells if isinstance(cell, dict)]


def outline(data: bytes, kind: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='rndchat-hwp-') as folder:
        work, name = Path(folder), f'in.{kind}'
        (work / name).write_bytes(data)
        tables_raw = _json(work, 'export-tables', name).get('tables') or []
        fields_raw = _json(work, 'fields', name).get('fields') or []
        paragraphs = _paragraphs(work, name)
    tables, where = [], {}
    for table in tables_raw:
        if not isinstance(table, dict):
            continue
        tables.append({'index': int(table.get('index', len(tables))), 'rows': int(table.get('rows', 0)), 'cols': int(table.get('cols', 0)), 'inside': '',
                       'cells': _cells(table.get('cells') or [])})
        where.setdefault((int(table.get('section', 0)), int(table.get('paragraph', 0))), []).append(tables[-1]['index'])
    body = []
    for index, paragraph in enumerate(paragraphs):
        text = paragraph['text'] or ('(표·그림이 든 문단)' if paragraph['controls'] else '')
        body.append({'type': 'paragraph', 'index': index, 'text': text})
        for table in where.get((paragraph['section'], paragraph['paragraph']), []):
            body.append({'type': 'table', 'index': table, 'after': index})
    fields = [{'name': str(field.get('name') or ''), 'guide': str(field.get('guide') or field.get('memo') or ''), 'value': str(field.get('value') or '')}
              for field in fields_raw if isinstance(field, dict) and field.get('name')]
    return {'kind': kind, 'paragraphs': len(paragraphs), 'tables': tables, 'body': body, 'fields': fields}


def build(blocks: Any, title: str = '') -> bytes:
    """블록 목록(docs.build_docx 와 같은 모양)으로 한글 문서(HWPX)를 새로 만든다. 글과 표의 구조만 담는다(굵게·음영·열 너비는 없다)."""
    from . import docs
    if not isinstance(blocks, list) or not blocks:
        raise docs.DocumentError('blocks 는 비어 있지 않은 목록이어야 합니다')
    if len(blocks) > docs.MAX_BLOCKS:
        raise docs.DocumentError(f'blocks 는 {docs.MAX_BLOCKS}개까지입니다')
    plain = lambda value: docs._text(value).replace('**', '').replace('\n', ' ')
    spec: list[dict[str, Any]] = []
    merges: list[tuple[int, int, int, int, int]] = []    # (표 번호, 행, 열, 끝 행, 끝 열)
    tables = 0
    for block in blocks:
        if not isinstance(block, dict):
            raise docs.DocumentError('각 블록은 {"type": …} 모양의 객체여야 합니다')
        kind = block.get('type')
        if kind == 'heading':
            try:
                level = min(max(int(block.get('level') or 1), 1), 3)
            except (TypeError, ValueError):
                level = 1
            spec.append({'type': 'heading', 'level': level, 'text': plain(block.get('text'))})
        elif kind == 'paragraph':
            spec.append({'type': 'paragraph', 'text': plain(block.get('text'))})
        elif kind in ('bullets', 'numbered'):
            items = block.get('items')
            if not isinstance(items, list):
                raise docs.DocumentError(f'{kind} 블록에는 items 목록이 있어야 합니다')
            for number, item in enumerate(items[:docs.MAX_ROWS], start=1):
                spec.append({'type': 'paragraph', 'text': (f'{number}. ' if kind == 'numbered' else '• ') + plain(item)})
        elif kind == 'table':
            placed, width = docs._layout(block.get('rows'))
            grid = [['' for _ in range(width)] for _ in block['rows']]
            for row, col, down, across, cell in placed:
                grid[row][col] = plain(cell.get('text'))
                if down > 1 or across > 1:
                    merges.append((tables, row, col, row + down - 1, col + across - 1))
            spec.append({'type': 'table', 'rows': grid, 'cell_align': 'left'})
            tables += 1
        elif kind != 'page_break':
            raise docs.DocumentError(f'모르는 블록 종류입니다: {kind!r} (heading, paragraph, bullets, numbered, table, page_break 중 하나)')
    with tempfile.TemporaryDirectory(prefix='rndchat-hwp-') as folder:
        work = Path(folder)
        document = {'version': '1', 'blocks': spec}
        if docs._text(title).strip():
            document['title'] = docs._text(title, 300).strip()
        (work / 'spec.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        code, _, err = _run(work, 'scaffold', 'spec.json', '-o', 'step0.hwpx', '--json')
        if code != 0 or not (work / 'step0.hwpx').is_file():
            raise EditError(_message(err) or '한글 문서를 만들지 못했습니다')
        session = _Session(work, 'hwpx', (work / 'step0.hwpx').read_bytes())
        for table, row, col, end_row, end_col in merges:
            session.edit('merge-cells', '--table', str(table), '--row', str(row), '--col', str(col), '--end-row', str(end_row), '--end-col', str(end_col))
        return session.data()


class _Session:
    """고치는 동안의 작업 폴더. 명령이 성공할 때마다 결과 파일이 다음 명령의 입력이 된다."""

    def __init__(self, work: Path, kind: str, data: bytes):
        self.work, self.kind, self.step = work, kind, 0
        self.current = f'step0.{kind}'
        (work / self.current).write_bytes(data)

    def edit(self, *arguments: str) -> dict[str, Any]:
        """edit 하위 명령 하나. 실패하면 EditError(지금 파일은 그대로)."""
        self.step += 1
        target = f'step{self.step}.{self.kind}'
        code, out, err = _run(self.work, 'edit', arguments[0], self.current, *arguments[1:], '-o', target, '--json')
        if code != 0:
            raise EditError(_message(err) or f'엔진이 이 고치기를 거부했습니다(종료 코드 {code})')
        try:
            result = json.loads(out)
        except ValueError:
            result = {}
        if (self.work / target).is_file():               # 바뀐 것이 없으면 엔진이 파일을 만들지 않는다
            (self.work / self.current).unlink(missing_ok=True)
            self.current = target
        return result if isinstance(result, dict) else {}

    def paragraph(self, index: int) -> dict[str, Any]:
        paragraphs = _paragraphs(self.work, self.current)
        if index >= len(paragraphs):
            raise EditError(f'문단 p{index} 는 없습니다(문단은 p0~p{len(paragraphs) - 1})')
        return paragraphs[index]

    def data(self) -> bytes:
        return (self.work / self.current).read_bytes()


def _set_cell(session: _Session, edit: dict[str, Any]) -> dict[str, Any]:
    table, row, col = (str(_integer(edit, key)) for key in ('table', 'row', 'col'))
    lines = _clean(edit.get('text')).replace('\t', ' ').split('\n')[:MAX_LINES]
    where = ('--table', table, '--row', row, '--col', col)
    result = session.edit('set-cell', *where, '--text', lines[0], *(('--keep-style',) if edit.get('keep_style') else ()))
    for number, line in enumerate(lines[1:], start=1):   # 여러 줄: 칸 안의 문단을 갈라 가며 한 줄씩 넣는다
        session.edit('split-paragraph-in-cell', *where, '--cell-para', str(number - 1), '--offset', str(len(lines[number - 1])))
        if line:
            session.edit('insert-text-in-cell', *where, '--cell-para', str(number), '--offset', '0', '--text', line)
    entry = {'old': str(result.get('oldText') or ''), 'new': '\n'.join(lines)}
    if result.get('overflow'):                           # 값이 칸 폭을 넘친다(엔진의 어림 계산)
        entry['overflow'] = '값이 칸 너비를 넘칩니다. 더 짧게 줄이거나 그림으로 확인하세요'
    return entry


def _replace(session: _Session, edit: dict[str, Any]) -> dict[str, Any]:
    find, replace = _clean(edit.get('find')), _clean(edit.get('replace')).replace('\n', ' ')
    if not find:
        raise EditError('find 가 비어 있습니다')
    count = int(session.edit('replace-text', '--find', find, '--replace', replace).get('replacedCount') or 0)
    if not count:
        raise EditError(f'문서에 "{find[:40]}" 이(가) 없습니다. 개요에 보이는 글을 그대로(띄어쓰기까지) 적으세요')
    return {'replaced': count}


def _fill(session: _Session, edit: dict[str, Any]) -> dict[str, Any]:
    name, value = _clean(edit.get('name'), 200), _clean(edit.get('value')).replace('\n', ' ')
    if not name:
        raise EditError('name(누름틀 이름)이 비어 있습니다')
    (session.work / 'fields.json').write_text(json.dumps({name: value}, ensure_ascii=False), encoding='utf-8')
    result = session.edit('fill-fields', '--data', '@fields.json')
    if result.get('notFound'):
        raise EditError(f'누름틀 "{name}" 이(가) 없습니다. 개요 끝의 누름틀 이름을 그대로 쓰세요')
    entry = {'new': value}
    if result.get('ambiguous'):
        entry['note'] = f'같은 이름의 누름틀이 여럿이라 첫 번째만 채웠습니다. 나머지는 "{name}[1]" 처럼 순번을 붙여 채우세요'
    return entry


def _set_paragraph(session: _Session, edit: dict[str, Any]) -> dict[str, Any]:
    paragraph, text = session.paragraph(_integer(edit, 'paragraph')), _clean(edit.get('text')).replace('\n', ' ')
    if paragraph['controls']:
        raise EditError('이 문단에는 표나 그림이 들어 있어 통째로 바꿀 수 없습니다. replace_text 로 글만 바꾸세요')
    at = ('--section', str(paragraph['section']), '--para', str(paragraph['paragraph']))
    if paragraph['length']:
        session.edit('delete-text', *at, '--offset', '0', '--count', str(paragraph['length']))
    if text:
        session.edit('insert-text', *at, '--offset', '0', '--text', text)
    return {'old': paragraph['text'], 'new': text}


def _add_paragraphs(session: _Session, edit: dict[str, Any]) -> dict[str, Any]:
    anchor, lines = session.paragraph(_integer(edit, 'after')), _lines(edit)
    for offset, line in enumerate(lines, start=1):
        at = ('--section', str(anchor['section']), '--para', str(anchor['paragraph'] + offset))
        session.edit('insert-paragraph', *at)             # 앞 문단의 모양을 물려받은 빈 문단
        if line:
            session.edit('insert-text', *at, '--offset', '0', '--text', line)
            if not edit.get('keep_style'):               # 제목 아래에 본문을 쓸 때 제목의 굵은 글씨까지 따라오지 않게
                session.edit('apply-char-format', *at, '--props', '{"bold":false}')
    return {'added': len(lines), 'new': '\n'.join(lines)}


def apply(data: bytes, kind: str, edits: list[dict[str, Any]]) -> tuple[bytes, list[dict[str, Any]]]:
    handlers = {'set_cell': _set_cell, 'replace_text': _replace, 'fill_field': _fill, 'set_paragraph': _set_paragraph, 'add_paragraphs': _add_paragraphs}
    report = []
    with tempfile.TemporaryDirectory(prefix='rndchat-hwp-') as folder:
        session = _Session(Path(folder), kind, data)
        for edit in edits:
            op = edit.get('op')
            entry = {'op': op, **{key: edit[key] for key in ('table', 'row', 'col', 'paragraph', 'after', 'name') if key in edit}}
            if op in ('replace_text',):
                entry.update(find=_clean(edit.get('find'), 200), replace=_clean(edit.get('replace'), 200))
            try:
                if op == 'add_row':
                    session.edit('insert-row', '--table', str(_integer(edit, 'table')), '--row', str(_integer(edit, 'row')), '--below')
                    entry['new_row'] = _integer(edit, 'row') + 1
                elif op in handlers:
                    entry.update(handlers[op](session, edit))
                else:
                    raise EditError(f'모르는 op 입니다: {op!r}. 쓸 수 있는 것: {", ".join(OPS)}')
                entry['ok'] = True
            except EditError as exc:
                entry.update(ok=False, error=str(exc))
            report.append(entry)
        return session.data(), report
