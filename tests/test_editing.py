"""첨부 문서의 원본 고치기(워드)와 출처 번호 매기기."""
import io
import json
import os
import shutil
import unittest

from docx import Document
from docx.oxml.ns import qn

from chat import docs, editing, hwp
from chat.sources import Sources, cited_numbers, renumber, _snippet


def form() -> bytes:
    """표 하나(가로·세로로 합친 칸 포함)와 문단 몇 개가 있는 신청서."""
    document = Document()
    document.add_paragraph('참가 신청서')
    table = document.add_table(rows=4, cols=3)
    table.cell(0, 0).merge(table.cell(0, 2)).text = '신청 개요'
    table.cell(1, 0).merge(table.cell(2, 0)).text = '기업'
    table.cell(1, 1).text, table.cell(2, 1).text, table.cell(3, 0).text = '기업명', '대표자', '사업 내용'
    guide = table.cell(1, 2).paragraphs[0].add_run('ㅇㅇ기업')
    guide.italic = True
    document.add_paragraph('□ 추진 계획')
    paragraph = document.add_paragraph()
    for part in ('신청일: 20', '26년 ', '00월 00일'):                # 한 문장이 여러 조각(run)으로 나뉜 경우
        paragraph.add_run(part)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class DocxEditingTests(unittest.TestCase):
    def test_outline_shows_coordinates_and_merged_cells(self):
        outline = editing.outline(form(), 'docx')
        self.assertEqual((outline['paragraphs'], len(outline['tables'])), (3, 1))
        cells = {(cell['row'], cell['col']): cell for cell in outline['tables'][0]['cells']}
        self.assertEqual((cells[0, 0]['colSpan'], cells[1, 0]['rowSpan'], cells[1, 2]['text']), (3, 2, 'ㅇㅇ기업'))
        self.assertNotIn((0, 1), cells)                               # 가려진 칸은 나오지 않는다
        self.assertNotIn((2, 0), cells)
        text = editing.outline_text(outline)
        self.assertIn('p0: 참가 신청서', text)
        self.assertIn('[표 0] 4행×3열', text)
        self.assertIn('r1: c0(2행×1열 합침) "기업" | c1 "기업명" | c2 "ㅇㅇ기업"', text)
        self.assertLess(text.index('p0:'), text.index('[표 0]'))       # 문서에 놓인 순서대로
        self.assertLess(text.index('[표 0]'), text.index('p1: □ 추진 계획'))
        short = editing.outline_text(outline, limit=60)
        self.assertIn('inspect_document', short)                       # 잘리면 이어 보는 법을 알려 준다
        with self.assertRaises(editing.EditError):
            editing.outline_text(outline, table=5)
        with self.assertRaises(editing.EditError):
            editing.outline(b'not a document', 'docx')

    def test_edits_change_only_what_was_asked(self):
        result, report = editing.apply(form(), 'docx', [
            {'op': 'set_cell', 'table': 0, 'row': 1, 'col': 2, 'text': '(주)한빛'},
            {'op': 'set_cell', 'table': 0, 'row': 2, 'col': 2, 'text': '홍길동\n(공동) 김가온'},
            {'op': 'set_cell', 'table': 0, 'row': 2, 'col': 0, 'text': 'x'},              # 합쳐진 칸에 가려진 자리
            {'op': 'set_cell', 'table': 0, 'row': 9, 'col': 0, 'text': 'x'},
            {'op': 'replace_text', 'find': '2026년 00월 00일', 'replace': '2026년 11월 18일'},
            {'op': 'replace_text', 'find': '없는 글', 'replace': 'x'},
            {'op': 'set_paragraph', 'paragraph': 0, 'text': '2027년 참가 신청서'},
            {'op': 'add_paragraphs', 'after': 1, 'lines': ['1단계: 설계', '2단계: 시제품']},
            {'op': 'add_row', 'table': 0, 'row': 3},
            {'op': 'fill_field', 'name': '성명', 'value': 'x'},
            {'op': 'explode'},
        ])
        self.assertEqual([entry['ok'] for entry in report], [True, True, False, False, True, False, True, True, True, False, False])
        self.assertIn('r1c0 에 쓰세요', report[2]['error'])            # 어디에 써야 하는지 알려 준다
        self.assertEqual((report[0]['old'], report[0]['new'], report[4]['replaced']), ('ㅇㅇ기업', '(주)한빛', 1))
        document = Document(io.BytesIO(result))
        table = document.tables[0]
        self.assertEqual(table.cell(1, 2).text, '(주)한빛')
        self.assertIsNone(table.cell(1, 2).paragraphs[0].runs[0].italic)                 # 안내문의 기울임은 물려받지 않는다
        self.assertEqual([p.text for p in table.cell(2, 2).paragraphs], ['홍길동', '(공동) 김가온'])
        self.assertEqual((table.cell(0, 0).text, table.cell(1, 1).text, len(table.rows)), ('신청 개요', '기업명', 5))   # 나머지는 그대로
        self.assertEqual([p.text for p in document.paragraphs], ['2027년 참가 신청서', '□ 추진 계획', '1단계: 설계', '2단계: 시제품', '신청일: 2026년 11월 18일'])
        self.assertEqual(len(table._tbl.findall(qn('w:tr'))[4].findall(qn('w:tc'))), 3)
        with self.assertRaises(editing.EditError):                    # 하나도 못 고쳤으면 파일을 만들지 않는다
            editing.apply(form(), 'docx', [{'op': 'replace_text', 'find': '없는 글', 'replace': 'x'}])
        for bad in (None, [], 'x', [1], [{'op': 'set_cell'}] * (editing.MAX_EDITS + 1)):
            with self.assertRaises(editing.EditError):
                editing.apply(form(), 'docx', bad)
        with self.assertRaises(editing.EditError):
            editing.apply(b'%PDF-1.4', 'pdf', [{'op': 'replace_text', 'find': 'a', 'replace': 'b'}])


ENGINE = os.environ.get('RHWP', '') or shutil.which('rhwp') or ''


@unittest.skipUnless(ENGINE and os.access(ENGINE, os.X_OK), 'rhwp engine is not installed (set RHWP=/path/to/rhwp to run these)')
class HwpEditingTests(unittest.TestCase):
    """한글 문서는 rhwp 엔진으로 고친다. 시험 문서도 엔진으로 만든다(남의 양식을 저장소에 두지 않는다)."""

    def setUp(self):
        hwp.configure(ENGINE)
        self.addCleanup(hwp.configure, '')
        self.form = hwp.build([{'type': 'heading', 'text': '신청 개요', 'level': 1}, {'type': 'paragraph', 'text': '□ 추진 **계획**'}, {'type': 'paragraph', 'text': ''},
                               {'type': 'table', 'rows': [[{'text': '신청서', 'colspan': 2}], ['기업명', 'ㅇㅇ기업'], ['대표자', '']]}], '참가 신청서')

    def test_new_document_keeps_structure_and_merged_cells(self):
        outline = editing.outline(self.form, 'hwpx')
        text = editing.outline_text(outline)
        self.assertEqual(text.splitlines()[:4], ['p0: 참가 신청서', 'p1: 신청 개요', 'p2: □ 추진 계획', 'p3: (빈 문단)'])
        self.assertIn('[표 0] 3행×2열', text)
        self.assertIn('r0: c0(1행×2열 합침) "신청서"', text)
        self.assertIn('r1: c0 "기업명" | c1 "ㅇㅇ기업"', text)
        self.assertTrue(hwp.to_pdf(self.form, 'hwpx').startswith(b'%PDF-'))               # 엔진의 조판으로 쪽을 그린다
        with self.assertRaises(docs.DocumentError):
            hwp.build([{'type': 'unknown'}])
        with self.assertRaises(editing.EditError):
            editing.outline(b'PK\x03\x04 not a document', 'hwpx')

    def test_edits_change_the_original_and_report_each_step(self):
        result, report = editing.apply(self.form, 'hwpx', [
            {'op': 'set_cell', 'table': 0, 'row': 1, 'col': 1, 'text': '(주)한빛'},
            {'op': 'set_cell', 'table': 0, 'row': 2, 'col': 1, 'text': '홍길동\n(공동) 김가온'},
            {'op': 'set_cell', 'table': 0, 'row': 0, 'col': 1, 'text': 'x'},              # 합쳐진 칸에 가려진 자리
            {'op': 'set_cell', 'table': 0, 'row': 1, 'col': 1, 'text': '--dry-run -o /tmp/x'},    # 옵션처럼 생긴 글도 글로만 들어간다
            {'op': 'replace_text', 'find': '추진 계획', 'replace': '추진 일정'},
            {'op': 'replace_text', 'find': '없는 글', 'replace': 'x'},
            {'op': 'set_paragraph', 'paragraph': 3, 'text': '2027년 상반기'},
            {'op': 'set_paragraph', 'paragraph': 4, 'text': 'x'},                          # 표가 든 문단
            {'op': 'add_paragraphs', 'after': 2, 'lines': ['1단계', '2단계']},
            {'op': 'add_row', 'table': 0, 'row': 2},
            {'op': 'fill_field', 'name': '성명', 'value': 'x'},
            {'op': 'explode'}])
        self.assertEqual([entry['ok'] for entry in report], [True, True, False, True, True, False, True, False, True, True, False, False])
        self.assertIn('앵커', report[2]['error'])                      # 엔진이 어디에 써야 하는지 알려 준다
        self.assertEqual((report[0]['old'], report[0]['new'], report[4]['replaced'], report[8]['added']), ('ㅇㅇ기업', '(주)한빛', 1, 2))
        text = editing.outline_text(editing.outline(result, 'hwpx'))
        for expected in ('p2: □ 추진 일정', 'p3: 1단계', 'p4: 2단계', 'p5: 2027년 상반기', '[표 0] 4행×2열', 'c1 "--dry-run -o /tmp/x"', 'c1 "홍길동 (공동) 김가온"'):
            self.assertIn(expected, text)
        self.assertEqual(editing.outline_text(editing.outline(self.form, 'hwpx')).count('ㅇㅇ기업'), 1)   # 원본은 그대로다
        with self.assertRaises(editing.EditError):
            editing.apply(self.form, 'hwpx', [{'op': 'replace_text', 'find': '없는 글', 'replace': 'x'}])

    def test_without_the_engine_documents_are_refused_with_a_reason(self):
        hwp.configure('/nonexistent/rhwp')
        self.assertFalse(hwp.available())
        with self.assertRaises(editing.EditError) as caught:
            editing.outline(self.form, 'hwpx')
        self.assertIn('rhwp', str(caught.exception))


class SourceTests(unittest.TestCase):
    def test_card_summary_drops_table_marks_and_tags(self):
        raw = '¦ ¦ --- ¦ &amp;lt;강원 AI 헬스케어 조성사업&amp;gt;<br>아태 AI **글로벌** 교차실증<BR/>수행계획서 | 작성안내'
        self.assertEqual(_snippet({'title': '공고', 'summary': raw}), '<강원 AI 헬스케어 조성사업> 아태 AI 글로벌 교차실증 수행계획서 작성안내')
        self.assertEqual(_snippet({'title': '공고', 'summary': '| --- | --- |'}), '')

    def test_tool_results_get_numbers_and_cards(self):
        sources = Sources()
        found = {'total': 2689, 'items': [
            {'project_number': '271', 'title': '분산형 양자컴퓨팅', 'organization': '고려대학교', 'government_funds': 230570000,
             'total_period': '2024-05-01 ~ 2027-01-31', 'summary': '고전통신 기반 분산형 양자알고리즘', 'url': 'https://www.ntis.go.kr/project/pjtInfo.do?pjtId=271'},
            {'title': '주소가 없는 자료'}, {'title': '나쁜 주소', 'url': 'javascript:alert(1)'}]}
        text, summary = sources.observe('search_projects', {}, json.dumps(found, ensure_ascii=False), False)
        self.assertEqual(json.loads(text)['items'][0]['cite'], 1)                        # 모델이 볼 결과에 번호가 들어간다
        self.assertNotIn('cite', json.loads(text)['items'][1])
        self.assertEqual((summary['total'], summary['items']), (2689, [{'n': 1, 'title': '분산형 양자컴퓨팅'}]))
        card = sources.items[0]
        self.assertEqual((card['site'], card['facts'][0], ['정부연구비', '2.3억 원'] in card['facts']), ('NTIS', ['수행기관', '고려대학교'], True))
        self.assertEqual(sources.take_fresh(), [card])
        self.assertEqual(sources.take_fresh(), [])
        # 같은 주소는 같은 번호, 공고는 번호로 주소를 만든다, 논문은 과제를 출처로 삼는다
        notice = {'results': [{'notice_id': 'PBLN_000000000127965', 'title': '청년 창업 지원', 'region': '대구'}]}
        paper = {'items': [{'title': '어떤 논문', 'project_title': '분산형 양자컴퓨팅', 'project_url': 'https://www.ntis.go.kr/project/pjtInfo.do?pjtId=271'}]}
        text, _ = sources.observe('search_history', {}, json.dumps(notice, ensure_ascii=False), False)
        self.assertEqual((json.loads(text)['results'][0]['cite'], sources.items[1]['url'].endswith('pblancId=PBLN_000000000127965'), sources.items[1]['site']), (2, True, '기업마당'))
        text, _ = sources.observe('search_results', {}, json.dumps(paper, ensure_ascii=False), False)
        self.assertEqual((json.loads(text)['items'][0]['cite'], len(sources.items)), (1, 2))
        text, summary = sources.observe('open_link', {'url': 'https://example.org/a'}, json.dumps({'title': '안내 페이지', 'text': '본문'}), False)
        self.assertEqual((json.loads(text)['cite'], sources.items[2]['site']), (3, 'example.org'))
        self.assertEqual(sources.observe('x', {}, '글자 결과', False), ('글자 결과', {}))
        self.assertEqual(sources.observe('x', {}, '실패했습니다', True)[1], {'error': '실패했습니다'})
        self.assertIn('[2] 청년 창업 지원', sources.note({2}))
        self.assertEqual(sources.note(set()), '')

    def test_numbers_are_shown_in_order_of_first_use(self):
        items = [{'n': 2, 'title': 'b', 'url': 'u2'}, {'n': 9, 'title': 'a', 'url': 'u9'}]
        self.assertEqual(cited_numbers('가[9] 나[2, 9] 다'), {2, 9})
        text, cited = renumber('가[9] 나[2][9] 다[77] [링크](http://x)', items)
        self.assertEqual(text, '가[1] 나[2][1] 다[77] [링크](http://x)')
        self.assertEqual([(number, source['n']) for number, source in cited], [(1, 9), (2, 2)])


if __name__ == '__main__':
    unittest.main()
