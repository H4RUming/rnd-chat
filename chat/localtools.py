"""모델이 쓰는 이 서버 안의 도구: 첨부한 문서 고치기·들여다보기, 문서(DOCX)와 엑셀(XLSX) 새로 만들기.

MCP 서버의 도구와 함께 모델에 보이지만 실행은 여기서 한다. 만든 파일은 그 사용자의 보관함에 들어가고,
화면에는 파일 카드로 나타난다. 자료 서버가 돌려준 결과에서 출처를 모으는 일(observe)도 여기서 맡는다.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from . import docs, editing, hwp
from .files import FileError, FileStore, Previews, locate, pages_with, pdf_pages, render_page
from .sources import Sources
from .tools import ToolResult

MAX_CHANGES_KEPT = 300           # 파일 카드의 '바뀐 내용'에 싣는 건수
EDIT_PAGES_SHOWN = 3             # 고친 뒤 모델에 확인용으로 보여 주는 쪽 수
_CELL = {'anyOf': [{'type': 'string'}, {'type': 'number'}, {'type': 'object', 'properties': {
    'text': {'type': 'string'}, 'colspan': {'type': 'integer'}, 'rowspan': {'type': 'integer'}, 'bold': {'type': 'boolean'},
    'align': {'type': 'string', 'enum': ['left', 'center', 'right']}, 'shade': {'type': 'boolean'}}}]}
SPECS = [
    {'type': 'function', 'function': {
        'name': 'edit_document',
        'description': '''사용자가 첨부한 문서(HWP·HWPX·DOCX)의 원본을 그대로 두고 내용만 고쳐, 같은 형식의 파일로 돌려준다. 양식을 채우거나
문구를 바꿔 달라는 요청에 쓴다. 문서를 새로 그리지 않으므로 양식의 모양(글꼴·표·여백)이 그대로 남는다.

edits 는 고칠 내용의 목록이고, 좌표는 첨부 설명의 "개요"에 적힌 것을 쓴다(모두 0 부터 센다):
- {"op": "set_cell", "table": 3, "row": 1, "col": 2, "text": "…"}   표의 칸 내용을 바꾼다. 합쳐진 칸은 개요에 나온 좌표(왼쪽 위)에 쓴다.
- {"op": "replace_text", "find": "…", "replace": "…"}               문서 전체에서 글을 바꾼다. find 는 문서에 있는 그대로 적는다.
- {"op": "set_paragraph", "paragraph": 12, "text": "…"}             문단(p번호)의 글을 통째로 바꾼다. 빈 문단을 채울 때도 쓴다.
- {"op": "add_paragraphs", "after": 12, "lines": ["…", "…"]}       문단 뒤에 새 문단들을 넣는다(서술형 항목을 길게 쓸 때).
- {"op": "add_row", "table": 3, "row": 4}                          표의 그 행 아래에 같은 모양의 빈 행을 넣는다.
- {"op": "fill_field", "name": "성명", "value": "…"}                누름틀(개요 끝에 이름이 나열된 입력 칸)에 값을 넣는다.

쓰는 법:
- 항목 이름이 적힌 칸(예: "성명")은 건드리지 않고, 그 옆이나 아래의 값 칸에 쓴다. 예시·안내 문구("ㅇㅇ", "yyyy.mm.dd" 등)가 든 칸은 실제 값으로 바꾼다.
- 값은 사용자가 알려 준 정보와 도구로 찾은 자료만 쓴다. 모르는 칸은 그대로 둔다(지어내지 않는다).
- 한 번에 여러 건을 보낸다. 결과에는 건별 성공·실패와, 고친 쪽의 그림이 함께 온다. 그림을 보고 잘못 들어간 곳·칸을 넘친 곳이 있으면
  돌려받은 새 file_id 로 다시 고친다.''',
        'parameters': {'type': 'object', 'properties': {
            'file_id': {'type': 'string', 'description': '고칠 문서의 file_id(첨부 설명이나 앞선 결과에 적혀 있다)'},
            'edits': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'op': {'type': 'string', 'enum': list(editing.OPS)}, 'table': {'type': 'integer'}, 'row': {'type': 'integer'}, 'col': {'type': 'integer'},
                'text': {'type': 'string'}, 'find': {'type': 'string'}, 'replace': {'type': 'string'}, 'paragraph': {'type': 'integer'},
                'after': {'type': 'integer'}, 'lines': {'type': 'array', 'items': {'type': 'string'}}, 'name': {'type': 'string'},
                'value': {'type': 'string'}, 'section': {'type': 'integer'}}, 'required': ['op']}},
            'filename': {'type': 'string', 'description': '돌려줄 파일 이름(확장자 없이). 생략하면 원래 이름에 "(작성본)"을 붙인다'}},
            'required': ['file_id', 'edits']}}},
    {'type': 'function', 'function': {
        'name': 'inspect_document',
        'description': '''첨부한 문서(또는 고친 문서)를 더 자세히 본다. 개요가 길어 잘렸을 때 이어서 보거나, 표 하나의 칸 내용을 전부 보거나,
특정 쪽의 모양을 그림으로 확인할 때 쓴다. table 을 주면 그 표의 모든 칸을, from_paragraph 를 주면 그 문단부터의 개요를,
pages(쪽 번호 목록, 1 부터)를 주면 그 쪽들의 그림을 돌려준다.''',
        'parameters': {'type': 'object', 'properties': {
            'file_id': {'type': 'string'}, 'table': {'type': 'integer'}, 'from_paragraph': {'type': 'integer'},
            'pages': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['file_id']}}},
    {'type': 'function', 'function': {
        'name': 'create_document',
        'description': '''문서를 새로 만들어 사용자에게 파일로 준다(기본은 워드 DOCX, format 을 "hwpx" 로 주면 한글 문서). 찾은 내용을 문서·보고서로
정리해 달라고 할 때, 그리고 양식이 PDF 로만 있어 원본을 고칠 수 없을 때(그 양식의 모양을 따라 새로 만든다) 쓴다. HWP·HWPX·DOCX 양식을
채울 때는 이 도구가 아니라 edit_document 를 쓴다. 사용자가 "한글 파일", "hwp" 로 달라고 하면 format 을 "hwpx" 로 준다(한글 문서에는 글과 표의
구조만 담기고 굵게·음영·열 너비는 빠진다).

내용은 blocks(차례대로 놓이는 블록 목록)로 준다:
- {"type": "heading", "text": "…", "level": 1~3}
- {"type": "paragraph", "text": "…", "align": "left|center|right", "bold": false}
- {"type": "bullets" | "numbered", "items": ["…"]}
- {"type": "table", "rows": [[칸, …], …], "header_rows": 0, "widths": [열 너비 비율…]}
    칸은 글이거나 {"text": "…", "colspan": 2, "rowspan": 1, "bold": true, "align": "center", "shade": true}.
    합쳐진 칸은 왼쪽 위 칸 하나만 적고 colspan/rowspan 으로 범위를 준다(가려지는 칸은 적지 않는다).
- {"type": "page_break"}
글 안의 **굵게** 는 굵은 글씨가 된다.

PDF 양식을 따라 만들 때: 양식의 제목, 항목 순서, 표의 행·열과 합쳐진 칸, 항목 이름을 그대로 옮긴다. 항목 이름 칸은 shade 로
구분한다. 채울 내용은 사용자가 준 정보와 도구로 찾은 자료만 쓰고, 모르는 칸은 비워 두거나 "(작성 필요)"로 둔다.
간단한 정리 문서는 blocks 대신 markdown(마크다운 글)으로 줘도 된다.''',
        'parameters': {'type': 'object', 'properties': {
            'filename': {'type': 'string', 'description': '파일 이름(확장자 없이). 예: "AI 반도체 과제 현황"'},
            'title': {'type': 'string', 'description': '문서 맨 위 가운데에 놓일 제목(없으면 생략)'},
            'format': {'type': 'string', 'enum': ['docx', 'hwpx'], 'description': '파일 형식. 기본 docx, 한글 문서는 hwpx'},
            'blocks': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'type': {'type': 'string', 'enum': ['heading', 'paragraph', 'bullets', 'numbered', 'table', 'page_break']},
                'text': {'type': 'string'}, 'level': {'type': 'integer'}, 'align': {'type': 'string'}, 'bold': {'type': 'boolean'},
                'items': {'type': 'array', 'items': {'type': 'string'}},
                'rows': {'type': 'array', 'items': {'type': 'array', 'items': _CELL}},
                'header_rows': {'type': 'integer'}, 'widths': {'type': 'array', 'items': {'type': 'number'}}}, 'required': ['type']}},
            'markdown': {'type': 'string', 'description': 'blocks 대신 쓸 수 있는 마크다운 글'}},
            'required': ['filename']}}},
    {'type': 'function', 'function': {
        'name': 'create_spreadsheet',
        'description': '''엑셀 파일(XLSX)을 만들어 사용자에게 파일로 준다. 목록·집계·비교표를 엑셀로 달라고 할 때 쓴다.
시트마다 columns(열 이름)와 rows(행들, 각 행은 columns 와 같은 순서의 값 목록)를 준다. 숫자는 따옴표 없이 숫자로 주면
엑셀에서 계산할 수 있다. 주소(url)는 링크가 된다. 도구로 찾은 자료를 빠짐없이 옮기고 지어낸 값을 넣지 않는다.''',
        'parameters': {'type': 'object', 'properties': {
            'filename': {'type': 'string', 'description': '파일 이름(확장자 없이)'},
            'sheets': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'name': {'type': 'string'}, 'columns': {'type': 'array', 'items': {'type': 'string'}},
                'rows': {'type': 'array', 'items': {'type': 'array', 'items': {'anyOf': [{'type': 'string'}, {'type': 'number'}, {'type': 'null'}]}}}},
                'required': ['columns', 'rows']}}},
            'required': ['filename', 'sheets']}}},
]
NAMES = tuple(spec['function']['name'] for spec in SPECS)
CREATED_NOTE = '파일이 사용자 화면에 카드로 표시되었습니다(미리보기·내려받기 가능). 답에서는 무엇을 담았는지 짧게 설명하고, 내려받기 주소를 지어내지 마세요.'


def _change(entry: dict[str, Any]) -> dict[str, Any]:
    """화면의 '바뀐 내용'에 싣는 한 줄(긴 글은 줄인다)."""
    kept = {key: entry[key] for key in ('op', 'ok', 'table', 'row', 'col', 'paragraph', 'after', 'name', 'find', 'replaced', 'added', 'error') if key in entry}
    for key in ('old', 'new', 'replace'):
        if key in entry:
            kept[key] = str(entry[key])[:240]
    return kept


class LocalTools:
    """한 사용자의 한 요청에 묶인 도구 모음."""

    def __init__(self, store: FileStore, user: str, previews: Previews | None = None, sources: Sources | None = None, extra: Any = None):
        self.store, self.user = store, user
        self.previews = previews or Previews(store)
        self.sources = sources or Sources()
        self.extra = extra                               # 이 대화에서만 더 주는 도구(비서 대화의 맡긴 일 도구). specs() 와 call() 이 있다

    def specs(self) -> list[dict[str, Any]]:
        return SPECS + (self.extra.specs() if self.extra else [])

    def observe(self, name: str, arguments: dict[str, Any], text: str, is_error: bool) -> tuple[str, dict[str, Any]]:
        """자료 서버의 결과에서 출처를 등록한다. (번호를 넣은 결과 글, 화면용 요약)"""
        return self.sources.observe(name, arguments, text, is_error)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult | None:
        """우리 도구가 아니면 None(그러면 MCP 쪽에서 찾는다)."""
        if name not in NAMES:
            return await self.extra.call(name, arguments) if self.extra else None
        arguments = dict(arguments)
        for key in ('blocks', 'edits', 'sheets', 'pages'):   # 모델이 목록을 JSON 글로 감싸 보내는 일이 있다: 풀어서 받는다
            if isinstance(arguments.get(key), str):
                try:
                    arguments[key] = json.loads(arguments[key])
                except ValueError:
                    return ToolResult(f'{key} 는 JSON 글이 아니라 목록(배열) 그대로 보내야 합니다. 고쳐서 다시 호출하세요.', is_error=True)
        try:
            if name == 'edit_document':
                return await self._edit(arguments)
            if name == 'inspect_document':
                return await self._inspect(arguments)
            if name == 'create_document':
                blocks = arguments.get('blocks')
                if not blocks and isinstance(arguments.get('markdown'), str) and arguments['markdown'].strip():
                    blocks = docs.markdown_blocks(arguments['markdown'])
                if arguments.get('format') == 'hwpx' and hwp.available():
                    data, kind = await asyncio.to_thread(hwp.build, blocks, str(arguments.get('title') or '')), 'hwpx'
                else:
                    data, kind = docs.build_docx(blocks, str(arguments.get('title') or '')), 'docx'
            else:
                data, kind = docs.build_xlsx(arguments.get('sheets')), 'xlsx'
        except (docs.DocumentError, editing.EditError) as exc:
            return ToolResult(f'{exc}. 고쳐서 다시 호출하세요.', is_error=True)
        except FileError as exc:
            return ToolResult(str(exc), is_error=True)
        meta = self.store.save(self.user, docs.safe_filename(arguments.get('filename'), kind), data, kind, origin='generated')
        note = {'created': meta['name'], 'file_id': meta['id'], 'size_bytes': meta['size'], 'note': CREATED_NOTE}
        return ToolResult(json.dumps(note, ensure_ascii=False), files=[meta], summary={'created': meta['name']})

    def _document(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        found = self.store.get(self.user, str(arguments.get('file_id') or ''))
        if found is None:
            raise editing.EditError('그 file_id 의 문서가 없습니다. 첨부 설명이나 앞선 도구 결과에 적힌 file_id 를 그대로 쓰세요')
        return found

    async def _edit(self, arguments: dict[str, Any]) -> ToolResult:
        meta, data = self._document(arguments)
        if meta['kind'] not in editing.EDITABLE:
            raise editing.EditError(f'{meta["kind"].upper()} 파일은 원본을 고칠 수 없습니다. PDF 양식이면 create_document 로 같은 모양의 문서를 새로 만드세요')
        result, report = await asyncio.to_thread(editing.apply, data, meta['kind'], arguments.get('edits'))
        failed = [entry for entry in report if not entry.get('ok')]
        stem = arguments.get('filename') or (meta['name'].rsplit('.', 1)[0] + ('' if meta.get('origin') == 'edited' else ' (작성본)'))
        saved = self.store.save(self.user, docs.safe_filename(stem, meta['kind']), result, meta['kind'], origin='edited',
                                parent=meta.get('root') or meta['id'], root=meta.get('root') or meta['id'], previous=meta['id'],
                                changes=[_change(entry) for entry in report[:MAX_CHANGES_KEPT]], applied=len(report) - len(failed), failed=len(failed))
        note: dict[str, Any] = {'saved': saved['name'], 'file_id': saved['id'], 'applied': len(report) - len(failed), 'failed': failed[:40]}
        overflow = [entry for entry in report if entry.get('overflow')]
        if overflow:
            note['overflow'] = [{key: entry.get(key) for key in ('table', 'row', 'col', 'new', 'overflow')} for entry in overflow[:20]]
        images: list[tuple[str, str]] = []
        try:                                             # 고친 쪽을 그림으로 돌려줘 모델이 직접 확인하게 한다
            pdf = await self.previews.pdf(self.user, saved, result)
            pages = await asyncio.to_thread(pdf_pages, pdf)
            texts = [str(entry.get('new') or entry.get('replace') or '') for entry in report if entry.get('ok')]
            # 바뀐 내용마다 몇 쪽에 있는지 적어 둔다(미리보기에서 눌러 바로 찾아가게)
            changes = [_change(entry) for entry in report[:MAX_CHANGES_KEPT]]
            where = await asyncio.to_thread(locate, pdf, [str(change.get('new') or change.get('replace') or '').split('\n')[0] if change.get('ok') else '' for change in changes])
            for change, page in zip(changes, where):
                if page:
                    change['page'] = page
            saved = self.store.update(self.user, saved['id'], pages=pages, changes=changes) or saved
            shown = await asyncio.to_thread(pages_with, pdf, texts, EDIT_PAGES_SHOWN)
            for page in shown:
                images.append(('image/jpeg', base64.b64encode(await asyncio.to_thread(render_page, pdf, page, 1200)).decode()))
            note['pages_shown'] = shown
            note['total_pages'] = pages
        except FileError as exc:
            note['preview'] = f'쪽 그림을 만들지 못했습니다({exc}). 내용은 저장되었습니다.'
        note['note'] = ('고친 문서가 사용자 화면에 카드로 표시되었습니다. 함께 온 쪽 그림으로 값이 제자리에 들어갔는지, 칸을 넘치지 않는지 확인하세요. '
                        '고칠 곳이 남았으면 위 file_id 로 edit_document 를 다시 부르고, 끝났으면 무엇을 채웠고 무엇을 비워 두었는지 답하세요.')
        return ToolResult(json.dumps(note, ensure_ascii=False), images=images, files=[saved],
                          summary={'saved': saved['name'], 'applied': len(report) - len(failed), 'failed': len(failed)})

    async def _inspect(self, arguments: dict[str, Any]) -> ToolResult:
        meta, data = self._document(arguments)
        images: list[tuple[str, str]] = []
        parts: list[str] = []
        pages = arguments.get('pages')
        if isinstance(pages, list) and pages:
            pdf = await self.previews.pdf(self.user, meta, data)
            total = await asyncio.to_thread(pdf_pages, pdf)
            wanted = [page for page in dict.fromkeys(pages) if isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= total][:4]
            if not wanted:
                raise editing.EditError(f'pages 는 1~{total} 사이의 쪽 번호 목록이어야 합니다')
            for page in wanted:
                images.append(('image/jpeg', base64.b64encode(await asyncio.to_thread(render_page, pdf, page, 1200)).decode()))
            parts.append(f'{meta["name"]} 의 {", ".join(map(str, wanted))}쪽 그림을 첨부했습니다(전체 {total}쪽).')
        if meta['kind'] in editing.EDITABLE and (not parts or 'table' in arguments or 'from_paragraph' in arguments):
            outline = await asyncio.to_thread(editing.outline, data, meta['kind'])
            table = arguments.get('table') if isinstance(arguments.get('table'), int) else None
            start = arguments.get('from_paragraph') if isinstance(arguments.get('from_paragraph'), int) else 0
            parts.append(editing.outline_text(outline, table=table, start=max(0, start)))
        elif not parts:
            parsed = await self.previews.parsed(self.user, meta, data)
            parts.append(f'{meta["name"]} (전체 {parsed["pages"]}쪽)에서 추출한 글:\n{parsed["text"]}')
        return ToolResult('\n\n'.join(parts), images=images, summary={'inspected': meta['name'], 'pages': len(images)})
