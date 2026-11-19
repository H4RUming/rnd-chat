"""첨부한 문서의 원본을 그대로 두고 내용만 고친다(HWP·HWPX 는 rhwp 엔진, DOCX 는 python-docx).

문서를 새로 그리지 않으므로 양식의 모양(글꼴, 표, 여백, 쪽 배치)이 그대로 남는다. 모델에는 '개요'(문단 번호와
표의 칸 좌표)를 보여 주고, 모델은 그 좌표로 고칠 곳을 가리킨다.

고치기(edit) 한 건의 모양:
  {"op": "set_cell", "table": 3, "row": 1, "col": 2, "text": "…"}        표의 칸 내용을 바꾼다
  {"op": "replace_text", "find": "…", "replace": "…"}                    문서 전체에서 글을 바꾼다
  {"op": "set_paragraph", "paragraph": 12, "text": "…"}                  문단의 글을 바꾼다(빈 문단 채우기 포함)
  {"op": "add_paragraphs", "after": 12, "lines": ["…", "…"]}            문단 뒤에 새 문단을 넣는다
  {"op": "add_row", "table": 3, "row": 4}                               표의 그 행 아래에 같은 모양의 빈 행을 넣는다
  {"op": "fill_field", "name": "성명", "value": "…"}                     누름틀(한글 문서의 입력 칸)에 값을 넣는다

좌표는 모두 0 부터 센다. 한 건이 실패해도 나머지는 계속하고, 건마다 결과를 돌려준다.
"""
from __future__ import annotations

import copy
import io
import re
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import _Cell
from docx.text.paragraph import Paragraph

EDITABLE = ('hwp', 'hwpx', 'docx')
MAX_EDITS, MAX_TEXT, MAX_LINES = 400, 4000, 60
OUTLINE_CHARS = 14000            # 모델에 한 번에 보여 주는 개요의 상한(글자)
CELL_CHARS = 60                  # 개요에서 칸·문단 하나를 보여 주는 길이
_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
OPS = ('set_cell', 'replace_text', 'set_paragraph', 'add_paragraphs', 'add_row', 'fill_field')


class EditError(ValueError):
    """고칠 수 없을 때. 메시지는 모델에게 그대로 돌려준다(무엇을 고쳐 다시 부르면 되는지 알 수 있게 쓴다)."""


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    return _ILLEGAL.sub('', '' if value is None else str(value))[:limit]


def _integer(edit: dict[str, Any], key: str) -> int:
    value = edit.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EditError(f'{key} 는 0 이상의 정수여야 합니다')
    try:
        number = int(value)
    except ValueError:
        raise EditError(f'{key} 는 0 이상의 정수여야 합니다') from None
    if number < 0:
        raise EditError(f'{key} 는 0 이상의 정수여야 합니다')
    return number


def _lines(edit: dict[str, Any]) -> list[str]:
    lines = edit.get('lines')
    if isinstance(lines, str):
        lines = lines.split('\n')
    if not isinstance(lines, list) or not lines or len(lines) > MAX_LINES:
        raise EditError(f'lines 는 글 1~{MAX_LINES}줄의 목록이어야 합니다')
    return [_clean(line).replace('\n', ' ') for line in lines]


def _short(text: str, limit: int = CELL_CHARS) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit] + '…'


# ── 개요(모델이 고칠 곳을 가리키는 지도) ────────────────────────────────────
def outline_text(outline: dict[str, Any], limit: int = OUTLINE_CHARS, table: int | None = None, start: int = 0) -> str:
    """개요를 모델이 읽을 글로. table 을 주면 그 표만 전부, start 를 주면 그 문단부터 보여 준다."""
    tables = {item['index']: item for item in outline['tables']}
    lines: list[str] = []
    if table is not None:
        if table not in tables:
            raise EditError(f'표 {table} 는 없습니다(표는 0~{len(tables) - 1})')
        lines += _table_lines(tables[table], full=True)
    else:
        for entry in outline['body']:
            if entry['type'] == 'paragraph':
                if entry['index'] >= start:
                    lines.append(f'p{entry["index"]}: {_short(entry["text"], 110) or "(빈 문단)"}')
            elif entry['type'] == 'table' and (not start or entry.get('after', 0) >= start):
                lines += _table_lines(tables[entry['index']], full=False)
        placed = {entry['index'] for entry in outline['body'] if entry['type'] == 'table'}
        for index, item in tables.items():                       # 다른 표의 칸 안에 든 표
            if index not in placed and not start:
                lines += _table_lines(item, full=False)
        if outline.get('fields') and not start:
            lines.append('누름틀(fill_field 로 채우는 입력 칸): ' + ', '.join(
                f'{field["name"]}' + (f'(안내: {_short(field["guide"], 30)})' if field.get('guide') else '') for field in outline['fields'][:80]))
    text, shown = '', 0
    for line in lines:
        if len(text) + len(line) + 1 > limit:
            break
        text += line + '\n'
        shown += 1
    if shown < len(lines):
        text += (f'… (개요가 길어 여기서 줄였습니다. 남은 {len(lines) - shown}줄은 inspect_document 로 '
                 'table 번호나 from_paragraph 를 주어 이어서 보세요.)\n')
    return text.rstrip()


def _table_lines(table: dict[str, Any], full: bool) -> list[str]:
    where = f', {table["inside"]} 안' if table.get('inside') else ''
    lines = [f'[표 {table["index"]}] {table["rows"]}행×{table["cols"]}열{where}' + ('' if table.get('editable', True) else ' — 칸 좌표로는 고칠 수 없음(replace_text 사용)')]
    rows: dict[int, list[str]] = {}
    for cell in table['cells']:
        span = ''
        if cell.get('colSpan', 1) > 1 or cell.get('rowSpan', 1) > 1:
            span = f'({cell.get("rowSpan", 1)}행×{cell.get("colSpan", 1)}열 합침)'
        text = cell['text'] if full else _short(cell['text'])
        rows.setdefault(cell['row'], []).append(f'c{cell["col"]}{span} "{text}"' + (' +표' if cell.get('nested') else ''))
    for row in sorted(rows):
        lines.append(f'  r{row}: ' + ' | '.join(rows[row]))
    return lines


# ── DOCX ────────────────────────────────────────────────────────────────────
def _docx_grid(tbl) -> tuple[list[dict[str, Any]], int, int]:
    """표의 칸을 격자 좌표로. 합쳐진 칸은 왼쪽 위(앵커)에 한 번만 나온다."""
    cells: list[dict[str, Any]] = []
    open_merges: dict[int, dict[str, Any]] = {}          # 열 → 세로로 이어지는 중인 앵커
    row_index, width = -1, 0
    for row_index, tr in enumerate(tbl.findall(qn('w:tr'))):
        col = 0
        properties = tr.find(qn('w:trPr'))
        before = properties.find(qn('w:gridBefore')) if properties is not None else None
        if before is not None:
            col = int(before.get(qn('w:val')) or 0)
        for tc in tr.findall(qn('w:tc')):
            tc_properties = tc.find(qn('w:tcPr'))
            span_node = tc_properties.find(qn('w:gridSpan')) if tc_properties is not None else None
            merge_node = tc_properties.find(qn('w:vMerge')) if tc_properties is not None else None
            span = max(1, int(span_node.get(qn('w:val')) or 1)) if span_node is not None else 1
            continued = merge_node is not None and merge_node.get(qn('w:val')) != 'restart'
            if continued and col in open_merges:
                open_merges[col]['rowSpan'] += 1
            else:
                cell = {'row': row_index, 'col': col, 'rowSpan': 1, 'colSpan': span, 'tc': tc}
                cells.append(cell)
                if merge_node is not None:
                    open_merges[col] = cell
                else:
                    open_merges.pop(col, None)
            col += span
        width = max(width, col)
    return cells, row_index + 1, width


def _tc_text(tc) -> str:
    return '\n'.join(Paragraph(p, None).text for p in tc.findall(qn('w:p'))).strip()


def _docx_tables(document) -> list[dict[str, Any]]:
    """문서의 모든 표(다른 표 안에 든 것까지)를 나오는 순서대로. index 가 표 번호다."""
    tables = []
    nodes = list(document.element.body.iter(qn('w:tbl')))
    numbers = {id(node): index for index, node in enumerate(nodes)}
    for index, tbl in enumerate(nodes):
        cells, rows, cols = _docx_grid(tbl)
        inside = ''
        parent_tc = tbl.getparent()
        if parent_tc is not None and parent_tc.tag == qn('w:tc'):
            parent_tbl = parent_tc.getparent().getparent()
            parent_cells, _, _ = _docx_grid(parent_tbl)
            anchor = next((cell for cell in parent_cells if cell['tc'] is parent_tc), None)
            inside = f'표 {numbers[id(parent_tbl)]} 의 r{anchor["row"]}c{anchor["col"]}' if anchor else f'표 {numbers[id(parent_tbl)]}'
        tables.append({'index': index, 'rows': rows, 'cols': cols, 'inside': inside, 'node': tbl, 'cells': [
            {'row': cell['row'], 'col': cell['col'], 'rowSpan': cell['rowSpan'], 'colSpan': cell['colSpan'], 'text': _tc_text(cell['tc']),
             'nested': bool(cell['tc'].findall(qn('w:tbl'))), 'tc': cell['tc']} for cell in cells]})
    return tables


def _docx_outline(document) -> dict[str, Any]:
    tables = _docx_tables(document)
    numbers = {id(table['node']): table['index'] for table in tables}
    body, paragraph = [], 0
    for child in document.element.body:
        if child.tag == qn('w:p'):
            body.append({'type': 'paragraph', 'index': paragraph, 'text': Paragraph(child, None).text})
            paragraph += 1
        elif child.tag == qn('w:tbl'):
            body.append({'type': 'table', 'index': numbers[id(child)], 'after': paragraph})
    public = [{key: value for key, value in table.items() if key != 'node'} | {
        'cells': [{key: value for key, value in cell.items() if key != 'tc'} for cell in table['cells']]} for table in tables]
    return {'kind': 'docx', 'paragraphs': paragraph, 'tables': public, 'body': body, 'fields': []}


def _plain(run_properties) -> None:
    """안내문의 색·기울임을 실제 값이 물려받지 않게 지운다."""
    if run_properties is None:
        return
    for name in ('w:color', 'w:i', 'w:iCs', 'w:highlight'):
        for node in run_properties.findall(qn(name)):
            run_properties.remove(node)


def _set_paragraph_text(p, text: str, keep_style: bool = False, bold: bool | None = None) -> None:
    """문단의 글을 바꾼다. 문단 모양과 첫 글자의 글자 모양은 그대로 둔다."""
    paragraph = Paragraph(p, None)
    for node in list(p):
        if node.tag in (qn('w:hyperlink'), qn('w:smartTag'), qn('w:fldSimple'), qn('w:ins'), qn('w:del')):
            p.remove(node)
    runs = paragraph.runs
    if runs:
        first = runs[0]
        for run in runs[1:]:
            p.remove(run._r)
    else:
        first = paragraph.add_run()
        mark = p.find(qn('w:pPr'))
        mark_properties = mark.find(qn('w:rPr')) if mark is not None else None
        if mark_properties is not None:                  # 빈 문단은 문단 표시에 글자 모양이 적혀 있다
            first._r.insert(0, copy.deepcopy(mark_properties))
    first.text = text
    properties = first._r.find(qn('w:rPr'))
    if not keep_style:
        _plain(properties)
    if bold is False and properties is not None:
        for name in ('w:b', 'w:bCs'):
            for node in properties.findall(qn(name)):
                properties.remove(node)


def _docx_set_cell(tables: list[dict[str, Any]], edit: dict[str, Any]) -> dict[str, Any]:
    index, row, col = _integer(edit, 'table'), _integer(edit, 'row'), _integer(edit, 'col')
    if index >= len(tables):
        raise EditError(f'표 {index} 는 없습니다(표는 0~{len(tables) - 1})')
    table = tables[index]
    cell = next((item for item in table['cells'] if item['row'] == row and item['col'] == col), None)
    if cell is None:
        cover = next((item for item in table['cells'] if item['row'] <= row < item['row'] + item['rowSpan']
                      and item['col'] <= col < item['col'] + item['colSpan']), None)
        if cover:
            raise EditError(f'표 {index} 의 r{row}c{col} 은 합쳐진 칸에 가려져 있습니다. 앵커인 r{cover["row"]}c{cover["col"]} 에 쓰세요')
        raise EditError(f'표 {index} 에 r{row}c{col} 칸이 없습니다({table["rows"]}행×{table["cols"]}열)')
    tc, text = cell['tc'], _clean(edit.get('text'))
    old = _tc_text(tc)
    paragraphs = tc.findall(qn('w:p'))
    lines = text.split('\n')[:MAX_LINES]
    _set_paragraph_text(paragraphs[0], lines[0], bool(edit.get('keep_style')))
    if not tc.findall(qn('w:tbl')):                      # 칸 안에 표가 있으면 첫 문단만 건드린다
        for extra in paragraphs[1:]:
            tc.remove(extra)
        last = paragraphs[0]
        for line in lines[1:]:
            clone = copy.deepcopy(paragraphs[0])
            _set_paragraph_text(clone, line, True)
            last.addnext(clone)
            last = clone
    else:
        _set_paragraph_text(paragraphs[0], ' '.join(lines), bool(edit.get('keep_style')))
    cell['text'] = text
    return {'old': old, 'new': text}


def _all_paragraphs(document):
    yield from document.element.body.iter(qn('w:p'))
    for section in document.sections:
        for part in (section.header, section.footer):
            if not part.is_linked_to_previous:
                yield from part._element.iter(qn('w:p'))


def _docx_replace(document, edit: dict[str, Any]) -> dict[str, Any]:
    find, replace = _clean(edit.get('find')), _clean(edit.get('replace'))
    if not find:
        raise EditError('find 가 비어 있습니다')
    count = 0
    for p in _all_paragraphs(document):
        runs = Paragraph(p, None).runs
        texts = [run.text for run in runs]
        full = ''.join(texts)
        if find not in full:
            continue
        starts, position = [], 0
        for text in texts:
            starts.append(position)
            position += len(text)
        hits, cursor = [], full.find(find)
        while cursor >= 0:
            hits.append(cursor)
            cursor = full.find(find, cursor + len(find))
        for start in reversed(hits):                     # 뒤에서부터 바꿔야 앞의 위치가 밀리지 않는다
            end = start + len(find)
            first = max(index for index, offset in enumerate(starts) if offset <= start)
            last = max(index for index, offset in enumerate(starts) if offset < end)
            head, tail = texts[first][:start - starts[first]], texts[last][end - starts[last]:]
            if first == last:
                texts[first] = head + replace + tail
            else:
                texts[first] = head + replace
                for middle in range(first + 1, last):
                    texts[middle] = ''
                texts[last] = tail
            count += 1
        for run, text in zip(runs, texts):
            if run.text != text:
                run.text = text
    if not count:
        raise EditError(f'문서에 "{_short(find, 40)}" 이(가) 없습니다. 개요에 보이는 글을 그대로(띄어쓰기까지) 적으세요')
    return {'replaced': count}


def _body_paragraphs(document) -> list:
    return document.element.body.findall(qn('w:p'))


def _docx_paragraph(document, edit: dict[str, Any], key: str = 'paragraph'):
    index = _integer(edit, key)
    paragraphs = _body_paragraphs(document)
    if index >= len(paragraphs):
        raise EditError(f'문단 p{index} 는 없습니다(문단은 p0~p{len(paragraphs) - 1})')
    return paragraphs[index]


def _docx_add_row(tables: list[dict[str, Any]], edit: dict[str, Any]) -> dict[str, Any]:
    index, row = _integer(edit, 'table'), _integer(edit, 'row')
    if index >= len(tables):
        raise EditError(f'표 {index} 는 없습니다(표는 0~{len(tables) - 1})')
    rows = tables[index]['node'].findall(qn('w:tr'))
    if row >= len(rows):
        raise EditError(f'표 {index} 에 r{row} 행이 없습니다({len(rows)}행)')
    clone = copy.deepcopy(rows[row])
    for tc in clone.findall(qn('w:tc')):
        for nested in tc.findall(qn('w:tbl')):
            tc.remove(nested)
        paragraphs = tc.findall(qn('w:p'))
        for extra in paragraphs[1:]:
            tc.remove(extra)
        if paragraphs:
            _set_paragraph_text(paragraphs[0], '', True)
    rows[row].addnext(clone)
    return {'new_row': row + 1}


def _docx_apply(data: bytes, edits: list[dict[str, Any]]) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        document = Document(io.BytesIO(data))
    except Exception:
        raise EditError('워드 문서를 열 수 없습니다(손상되었거나 암호가 걸려 있습니다)') from None
    report = []
    for edit in edits:
        entry = {'op': edit.get('op'), **{key: edit[key] for key in ('table', 'row', 'col', 'paragraph', 'after', 'name') if key in edit}}
        try:
            tables = _docx_tables(document)              # 행을 넣으면 좌표가 바뀌므로 건마다 다시 센다
            op = edit.get('op')
            if op == 'set_cell':
                entry.update(_docx_set_cell(tables, edit))
            elif op == 'replace_text':
                entry.update(find=_clean(edit.get('find'), 200), replace=_clean(edit.get('replace'), 200), **_docx_replace(document, edit))
            elif op == 'set_paragraph':
                p = _docx_paragraph(document, edit)
                old, text = Paragraph(p, None).text, _clean(edit.get('text')).replace('\n', ' ')
                _set_paragraph_text(p, text, bool(edit.get('keep_style')))
                entry.update(old=old, new=text)
            elif op == 'add_paragraphs':
                p, last = _docx_paragraph(document, edit, 'after'), None
                last = p
                lines = _lines(edit)
                for line in lines:
                    clone = copy.deepcopy(p)
                    _set_paragraph_text(clone, line, False, bold=None if edit.get('keep_style') else False)
                    last.addnext(clone)
                    last = clone
                entry.update(added=len(lines), new='\n'.join(lines))
            elif op == 'add_row':
                entry.update(_docx_add_row(tables, edit))
            elif op == 'fill_field':
                raise EditError('워드 문서에는 누름틀이 없습니다. set_cell 이나 replace_text 를 쓰세요')
            else:
                raise EditError(f'모르는 op 입니다: {op!r}. 쓸 수 있는 것: {", ".join(OPS)}')
            entry['ok'] = True
        except EditError as exc:
            entry.update(ok=False, error=str(exc))
        report.append(entry)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), report


# ── 공통 입구 ────────────────────────────────────────────────────────────────
def check_edits(edits: Any) -> list[dict[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise EditError('edits 는 고칠 내용의 목록이어야 합니다(하나 이상)')
    if len(edits) > MAX_EDITS:
        raise EditError(f'한 번에 고칠 수 있는 것은 {MAX_EDITS}건까지입니다. 나누어 부르세요')
    if not all(isinstance(edit, dict) for edit in edits):
        raise EditError('edits 의 각 항목은 {"op": …} 모양의 객체여야 합니다')
    return edits


def outline(data: bytes, kind: str) -> dict[str, Any]:
    if kind == 'docx':
        try:
            return _docx_outline(Document(io.BytesIO(data)))
        except EditError:
            raise
        except Exception:
            raise EditError('워드 문서를 열 수 없습니다(손상되었거나 암호가 걸려 있습니다)') from None
    if kind in ('hwp', 'hwpx'):
        from . import hwp
        return hwp.outline(data, kind)
    raise EditError('고칠 수 있는 문서는 HWP, HWPX, DOCX 입니다')


def apply(data: bytes, kind: str, edits: Any) -> tuple[bytes, list[dict[str, Any]]]:
    """고친 문서(같은 형식)와 건별 결과. 하나도 성공하지 못하면 EditError."""
    edits = check_edits(edits)
    if kind == 'docx':
        result, report = _docx_apply(data, edits)
    elif kind in ('hwp', 'hwpx'):
        from . import hwp
        result, report = hwp.apply(data, kind, edits)
    else:
        raise EditError('고칠 수 있는 문서는 HWP, HWPX, DOCX 입니다')
    if not any(entry.get('ok') for entry in report):
        errors = '; '.join(dict.fromkeys(entry.get('error', '') for entry in report))[:600]
        raise EditError(f'고친 것이 하나도 없습니다: {errors}')
    return result, report
