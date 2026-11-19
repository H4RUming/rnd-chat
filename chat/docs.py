"""문서(DOCX)와 엑셀(XLSX) 만들기.

문서는 '블록' 목록으로 받는다. 모델이 양식을 보고 그대로 옮길 수 있도록 표의 칸 합치기(colspan·rowspan)를 지원한다.

  {"type": "heading", "text": "…", "level": 1}
  {"type": "paragraph", "text": "…", "align": "left|center|right", "bold": false}
  {"type": "bullets" | "numbered", "items": ["…", …]}
  {"type": "table", "rows": [[칸, …], …], "header_rows": 1, "widths": [3, 5, …]}
      칸은 글 또는 {"text": "…", "colspan": 2, "rowspan": 1, "bold": true, "align": "center", "shade": true}
  {"type": "page_break"}

글 안의 **굵게** 는 굵은 글씨가 된다. 마크다운으로 쓴 답은 markdown_blocks 로 블록이 된다.
모델이 준 값은 그대로 믿지 않고 크기와 형식을 확인한다.
"""
from __future__ import annotations

import io
import re
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = '맑은 고딕'
MAX_BLOCKS, MAX_ROWS, MAX_COLUMNS, MAX_TEXT, MAX_SHEETS = 2000, 1000, 40, 8000, 12
ALIGN = {'left': WD_ALIGN_PARAGRAPH.LEFT, 'center': WD_ALIGN_PARAGRAPH.CENTER, 'right': WD_ALIGN_PARAGRAPH.RIGHT}
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)')
_LIST = re.compile(r'^\s*([-*•]|\d+[.)])\s+')
_RULE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')
_NUMBER = re.compile(r'^-?\d{1,3}(,\d{3})+(\.\d+)?$|^-?\d+(\.\d+)?$')
_ILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')         # XML 에 넣을 수 없는 제어 문자


class DocumentError(ValueError):
    """모델이나 사용자가 준 문서 내용이 형식에 맞지 않을 때. 메시지는 모델에게 그대로 돌려준다."""


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return _ILLEGAL.sub('', '' if value is None else str(value))[:limit]


def safe_filename(name: Any, extension: str, fallback: str = '문서') -> str:
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', _text(name, 120))
    stem = re.sub(r'\.(docx|xlsx|pdf|hwpx?|doc)$', '', ' '.join(stem.split()).strip('. '), flags=re.I)
    return f'{stem or fallback}.{extension}'


# ── 마크다운 → 블록 ──────────────────────────────────────────────────────
def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def markdown_blocks(text: str) -> list[dict[str, Any]]:
    """채팅의 답(마크다운)을 문서 블록으로. 링크는 '글(주소)'로 푼다."""
    text = _LINK.sub(lambda match: f'{match.group(1)}({match.group(2)})', text.replace('\r', ''))
    lines, blocks, i = text.split('\n'), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.strip().startswith('```'):
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                body.append(lines[i])
                i += 1
            i += 1
            blocks.append({'type': 'paragraph', 'text': '\n'.join(body), 'mono': True})
        elif re.match(r'^#{1,6}\s', line):
            blocks.append({'type': 'heading', 'text': line.lstrip('#').strip().rstrip('#').strip(), 'level': min(len(line) - len(line.lstrip('#')), 3)})
            i += 1
        elif re.match(r'^\s*(-{3,}|\*{3,}|_{3,})\s*$', line):
            i += 1
        elif '|' in line and i + 1 < len(lines) and _RULE.match(lines[i + 1]):
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            blocks.append({'type': 'table', 'rows': rows, 'header_rows': 1})
        elif _LIST.match(line):
            kind, items = 'numbered' if re.match(r'^\s*\d+[.)]\s+', line) else 'bullets', []
            while i < len(lines) and _LIST.match(lines[i]):
                items.append(_LIST.sub('', lines[i]))
                i += 1
            blocks.append({'type': kind, 'items': items})
        else:
            body = []
            while (i < len(lines) and lines[i].strip() and not re.match(r'^(#{1,6}\s|\s*```)', lines[i]) and not _LIST.match(lines[i])
                   and not ('|' in lines[i] and i + 1 < len(lines) and _RULE.match(lines[i + 1]))):
                body.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            blocks.append({'type': 'paragraph', 'text': '\n'.join(body)})
    return blocks


def markdown_tables(text: str) -> list[dict[str, Any]]:
    """답 안의 표들을 엑셀 시트로. 시트 이름은 표 바로 앞의 제목이나 문단에서 딴다."""
    sheets, label = [], ''
    for block in markdown_blocks(text):
        if block['type'] == 'table':
            rows = block['rows']
            sheets.append({'name': label or f'표 {len(sheets) + 1}', 'columns': rows[0], 'rows': rows[1:]})
            label = ''
        elif block['type'] == 'heading':
            label = _BOLD.sub(r'\1', block['text']).strip()
    return sheets


# ── DOCX ────────────────────────────────────────────────────────────────
def _runs(paragraph, text: str, bold: bool = False, mono: bool = False, size: float | None = None) -> None:
    text = text.replace('`', '')
    position = 0
    for match in _BOLD.finditer(text):
        if match.start() > position:
            _run(paragraph, text[position:match.start()], bold, mono, size)
        _run(paragraph, match.group(1), True, mono, size)
        position = match.end()
    if position < len(text) or not text:
        _run(paragraph, text[position:], bold, mono, size)


def _run(paragraph, text: str, bold: bool, mono: bool, size: float | None) -> None:
    for index, piece in enumerate(text.split('\n')):
        run = paragraph.add_run(piece)
        if index:
            run._r.insert(0, OxmlElement('w:br'))
        run.bold = bold or None
        if size:
            run.font.size = Pt(size)
        if mono:
            run.font.name = 'Consolas'


def _shade(cell, color: str = 'EDEDED') -> None:
    shading = OxmlElement('w:shd')
    shading.set(qn('w:val'), 'clear')
    shading.set(qn('w:color'), 'auto')
    shading.set(qn('w:fill'), color)
    cell._tc.get_or_add_tcPr().append(shading)


def _layout(rows: list[Any]) -> tuple[list[tuple[int, int, int, int, dict[str, Any]]], int]:
    """칸 합치기를 풀어 (행, 열, 세로 칸 수, 가로 칸 수, 칸) 목록과 전체 열 수를 낸다."""
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise DocumentError(f'표의 rows 는 1~{MAX_ROWS}개의 행이어야 합니다')
    taken: set[tuple[int, int]] = set()
    placed, width = [], 0
    for r, row in enumerate(rows):
        if not isinstance(row, list):
            raise DocumentError('표의 각 행은 칸의 목록이어야 합니다')
        c = 0
        for cell in row:
            cell = cell if isinstance(cell, dict) else {'text': cell}
            while (r, c) in taken:
                c += 1
            try:
                across, down = max(1, int(cell.get('colspan') or 1)), max(1, int(cell.get('rowspan') or 1))
            except (TypeError, ValueError):
                raise DocumentError('colspan 과 rowspan 은 정수여야 합니다') from None
            down = min(down, len(rows) - r)
            if c + across > MAX_COLUMNS:
                raise DocumentError(f'표의 열은 {MAX_COLUMNS}개까지입니다')
            for dr in range(down):
                for dc in range(across):
                    taken.add((r + dr, c + dc))
            placed.append((r, c, down, across, cell))
            c += across
            width = max(width, c)
    return placed, width


def _table(document, block: dict[str, Any]) -> None:
    rows = block.get('rows')
    placed, width = _layout(rows)
    table = document.add_table(rows=len(rows), cols=width)
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    try:
        header_rows = max(0, int(block.get('header_rows') or 0))
    except (TypeError, ValueError):
        header_rows = 0
    for r, c, down, across, spec in placed:
        cell = table.cell(r, c)
        if down > 1 or across > 1:
            cell = cell.merge(table.cell(r + down - 1, c + across - 1))
        paragraph = cell.paragraphs[0]
        header = r < header_rows
        _runs(paragraph, _text(spec.get('text')), bold=bool(spec.get('bold')) or header, size=9.5)
        paragraph.alignment = ALIGN.get(str(spec.get('align') or ('center' if header else 'left')), WD_ALIGN_PARAGRAPH.LEFT)
        if spec.get('shade') or header:
            _shade(cell)
    widths = block.get('widths')
    if isinstance(widths, list) and len(widths) == width and all(isinstance(value, (int, float)) and value > 0 for value in widths):
        total, usable = float(sum(widths)), 17.0                     # A4 에서 여백을 뺀 폭(cm)
        table.autofit = False
        for row in table.rows:
            for index, cell in enumerate(row.cells):
                cell.width = Cm(usable * widths[index] / total)
    document.add_paragraph().paragraph_format.space_after = Pt(2)


def build_docx(blocks: Any, title: str = '') -> bytes:
    if not isinstance(blocks, list) or not blocks:
        raise DocumentError('blocks 는 비어 있지 않은 목록이어야 합니다')
    if len(blocks) > MAX_BLOCKS:
        raise DocumentError(f'blocks 는 {MAX_BLOCKS}개까지입니다')
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(2)
    section.top_margin = section.bottom_margin = Cm(2)
    for style_name in ('Normal', 'Heading 1', 'Heading 2', 'Heading 3', 'Title', 'List Bullet', 'List Number'):
        style = document.styles[style_name]
        style.font.name = FONT
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), FONT)      # 한글에도 같은 글꼴
    document.styles['Normal'].font.size = Pt(10.5)
    if _text(title).strip():
        heading = document.add_paragraph()
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _runs(heading, _text(title, 300).strip(), bold=True, size=16)
        heading.paragraph_format.space_after = Pt(12)
    for block in blocks:
        if not isinstance(block, dict):
            raise DocumentError('각 블록은 {"type": …} 모양의 객체여야 합니다')
        kind = block.get('type')
        if kind == 'heading':
            try:
                level = min(max(int(block.get('level') or 1), 1), 3)
            except (TypeError, ValueError):
                level = 1
            paragraph = document.add_heading(level=level)
            _runs(paragraph, _text(block.get('text')))
            paragraph.alignment = ALIGN.get(str(block.get('align') or 'left'))
        elif kind == 'paragraph':
            paragraph = document.add_paragraph()
            _runs(paragraph, _text(block.get('text')), bold=bool(block.get('bold')), mono=bool(block.get('mono')))
            paragraph.alignment = ALIGN.get(str(block.get('align') or 'left'))
        elif kind in ('bullets', 'numbered'):
            items = block.get('items')
            if not isinstance(items, list):
                raise DocumentError(f'{kind} 블록에는 items 목록이 있어야 합니다')
            for item in items[:MAX_ROWS]:
                _runs(document.add_paragraph(style='List Bullet' if kind == 'bullets' else 'List Number'), _text(item))
        elif kind == 'table':
            _table(document, block)
        elif kind == 'page_break':
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        else:
            raise DocumentError(f'모르는 블록 종류입니다: {kind!r} (heading, paragraph, bullets, numbered, table, page_break 중 하나)')
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ── XLSX ────────────────────────────────────────────────────────────────
def _value(value: Any) -> Any:
    """숫자처럼 보이는 글은 숫자로(천 단위 쉼표 포함). 0 으로 시작하거나 15자리를 넘는 번호는 글 그대로 둔다."""
    if isinstance(value, bool) or value is None:
        return '' if value is None else str(value)
    if isinstance(value, (int, float)):
        return value
    text = _BOLD.sub(r'\1', _text(value, 4000)).strip()
    plain = text.replace(',', '')
    if _NUMBER.match(text) and len(plain.lstrip('-').split('.')[0]) <= 15 and not (plain.lstrip('-').startswith('0') and not plain.lstrip('-').startswith('0.') and plain.lstrip('-') != '0'):
        return float(plain) if '.' in plain else int(plain)
    return text


def build_xlsx(sheets: Any) -> bytes:
    if not isinstance(sheets, list) or not sheets or len(sheets) > MAX_SHEETS:
        raise DocumentError(f'sheets 는 1~{MAX_SHEETS}개의 시트여야 합니다')
    workbook = Workbook()
    workbook.remove(workbook.active)
    edge = Side(style='thin', color='D0D0D0')
    used: set[str] = set()
    for number, sheet in enumerate(sheets, 1):
        if not isinstance(sheet, dict):
            raise DocumentError('각 시트는 {"name", "columns", "rows"} 모양의 객체여야 합니다')
        columns, rows = sheet.get('columns'), sheet.get('rows')
        if not isinstance(columns, list) or not columns or len(columns) > MAX_COLUMNS:
            raise DocumentError(f'시트의 columns 는 1~{MAX_COLUMNS}개의 열 이름이어야 합니다')
        if not isinstance(rows, list) or len(rows) > 20000 or not all(isinstance(row, list) for row in rows):
            raise DocumentError('시트의 rows 는 행의 목록(각 행은 값의 목록)이어야 합니다')
        name = re.sub(r'[\[\]:*?/\\]', ' ', _text(sheet.get('name'), 60)).strip()[:31] or f'시트{number}'
        while name in used:
            name = f'{name[:28]}_{number}'
        used.add(name)
        page = workbook.create_sheet(name)
        for c, column in enumerate(columns, 1):
            cell = page.cell(row=1, column=c)
            cell.value, cell.data_type = _BOLD.sub(r'\1', _text(column, 200)), 's'
            cell.font, cell.fill = Font(name=FONT, bold=True), PatternFill('solid', fgColor='EDEDED')
            cell.alignment, cell.border = Alignment(horizontal='center', vertical='center', wrap_text=True), Border(bottom=edge, top=edge, left=edge, right=edge)
        widths = [len(str(column)) for column in columns]
        for r, row in enumerate(rows, 2):
            for c, raw in enumerate(row[:len(columns)], 1):
                value = _value(raw)
                cell = page.cell(row=r, column=c)
                cell.value = value
                if isinstance(value, str):
                    cell.data_type = 's'                              # '=' 로 시작하는 글이 수식으로 실행되지 않게 글로 못박는다
                    if re.match(r'^https?://\S+$', value):
                        cell.hyperlink, cell.font = value, Font(name=FONT, color='1D5FBF', underline='single')
                    else:
                        cell.font = Font(name=FONT)
                    cell.alignment = Alignment(vertical='top', wrap_text=len(value) > 40)
                else:
                    cell.font, cell.number_format = Font(name=FONT), '#,##0' if isinstance(value, int) and abs(value) >= 10000 else 'General'
                cell.border = Border(bottom=edge, top=edge, left=edge, right=edge)
                widths[c - 1] = max(widths[c - 1], min(len(str(value)), 60))
        for c, width in enumerate(widths, 1):                         # 한글은 폭이 넓어 넉넉히 잡는다
            page.column_dimensions[get_column_letter(c)].width = min(max(width * 1.7 + 2, 8), 70)
        page.freeze_panes = 'A2'
        if rows:
            page.auto_filter.ref = f'A1:{get_column_letter(len(columns))}{len(rows) + 1}'
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
