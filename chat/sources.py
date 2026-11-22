"""출처 모으기: 도구가 돌려준 자료에서 공고·과제·보고서를 골라 번호를 매긴다.

모델은 답에서 근거가 된 자료를 [번호] 로 가리키고, 화면은 그 부분을 형광펜으로 칠해 마우스를 올리면 자료 요약을,
누르면 원문을 새 탭으로 보여 준다. 번호는 대화 하나에서 이어지고(같은 주소는 같은 번호), 주소는 도구 결과에
있던 것만 쓴다. 모델이 쓴 주소는 출처로 삼지 않는다.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlsplit

MAX_SOURCES = 900                    # 대화 하나에 쌓아 두는 출처 수(답의 [번호] 는 세 자리까지다)
TIDY_OVER = 300                      # 이보다 많이 쌓이면 답이 가리키지 않는 출처를 덜어 낸다(다음 질문의 검색이 번호를 받을 자리)
MAX_SUMMARY_ITEMS = 8                # 도구 한 번의 결과에서 화면에 보여 주는 항목 수
BIZINFO_NOTICE = 'https://www.bizinfo.go.kr/sii/siia/selectSIIA200Detail.do?pblancId={}'
_NOTICE_ID = re.compile(r'PBLN_\d{6,20}')
SITES = {'www.ntis.go.kr': 'NTIS', 'ntis.go.kr': 'NTIS', 'www.bizinfo.go.kr': '기업마당', 'bizinfo.go.kr': '기업마당',
         'scienceon.kisti.re.kr': 'ScienceON'}
# 카드에 보여 줄 값: (이름표, 자료의 키들). 앞에서부터 있는 것만, 몇 개까지만 싣는다.
FACTS = [('수행기관', ('organization',)), ('부처', ('ministry',)), ('사업', ('program',)), ('연구기간', ('total_period',)),
         ('정부연구비', ('government_funds',)), ('기준년도', ('year',)), ('연구책임자', ('manager',)),
         ('소관', ('agency',)), ('수행', ('implementing_agency',)), ('지역', ('region',)), ('접수', ('application_start', 'application_end')),
         ('상태', ('status',)), ('분야', ('category',)), ('학술지', ('journal',)), ('저자', ('authors',)), ('발행', ('publisher',))]
MAX_FACTS = 5
_EMPTY = ('', 'null', 'none', 'nan', '-')              # 원자료에 빈 값이 글자로 들어 있는 경우
_SNIPPET_KEYS = ('summary', 'overview', 'description', 'snippet', 'abstract')
# 같은 질문 안에서 다시 나온 검색 결과는 이것만 남긴다(자세한 내용은 앞선 결과에 같은 번호로 있다)
_REPEAT_KEYS = ('notice_id', 'project_number', 'result_id', 'report_id', 'title', 'application_end', 'status', 'year', 'relevance', 'mentions', 'matched_in')
_MARKER = re.compile(r'\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]')
_MARKUP = re.compile(r'</?(?:br|p|div|span|b|i|u|strong|em|li|ul|ol|table|tr|td|th|a|h[1-6])\b[^<>]{0,80}>|[|¦]|-{3,}|^#{1,6}\s|\*\*', re.M | re.I)      # 표·제목 기호와 태그(카드에는 글만 보인다)


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or '').lower()
    return SITES.get(host) or (host[4:] if host.startswith('www.') else host)


def _http(value: Any) -> str:
    """http(s) 주소면 그대로, 아니면 빈 글."""
    if isinstance(value, str) and len(value) <= 2000:
        parts = urlsplit(value.strip())
        if parts.scheme in ('http', 'https') and parts.hostname:
            return value.strip()
    return ''


def _won(value: Any) -> str:
    try:
        amount = int(float(str(value).replace(',', '')))
    except (TypeError, ValueError):
        return str(value)
    if amount >= 100_000_000:
        return f'{amount / 100_000_000:,.1f}억 원'.replace('.0억', '억')
    if amount >= 10_000:
        return f'{amount // 10_000:,}만 원'
    return f'{amount:,}원'


def _facts(item: dict[str, Any]) -> list[list[str]]:
    facts = []
    for label, keys in FACTS:
        values = [str(item[key]).strip() for key in keys if item.get(key) not in (None, '', [], {}) and str(item[key]).strip().lower() not in _EMPTY]
        if not values:
            continue
        text = ' ~ '.join(values) if len(keys) > 1 else values[0]
        if keys[0].endswith('_funds'):
            text = _won(item[keys[0]])
        facts.append([label, text[:80]])
        if len(facts) >= MAX_FACTS:
            break
    return facts


def _snippet(item: dict[str, Any]) -> str:
    for key in _SNIPPET_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in _EMPTY and value.strip() != str(item.get('title', '')).strip():
            plain = ' '.join(_MARKUP.sub(' ', html.unescape(html.unescape(value)).replace('\x00', ' ')).split())      # 원자료에 &amp;lt; 처럼 두 번 바뀐 글자가 있다
            if len(plain) >= 8:
                return plain[:280]
    return ''


def _address(item: dict[str, Any]) -> str:
    """자료 하나가 가리키는 원문 주소. 공고는 주소가 없어도 공고 번호로 만들 수 있다."""
    url = _http(item.get('url')) or _http(item.get('project_url'))
    if not url and isinstance(item.get('notice_id'), str) and _NOTICE_ID.fullmatch(item['notice_id']):
        url = BIZINFO_NOTICE.format(item['notice_id'])
    return url


class Sources:
    """대화 하나의 출처 목록. items 는 대화에 그대로 저장된다(번호 n 은 1부터). 보통은 지워지지 않지만, 오래 이어지는
    비서 대화는 답이 가리키지 않는 출처를 덜어 내고(prune) 빈 번호를 다시 쓴다."""

    def __init__(self, items: list[dict[str, Any]] | None = None, limit: int = MAX_SOURCES):
        self.items: list[dict[str, Any]] = items if items is not None else []
        self.limit = limit
        self._by_url = {item['url']: item for item in self.items}
        self.fresh: list[dict[str, Any]] = []            # 이번 차례에 새로 생긴 것(화면에 바로 보낸다)
        self._unnumbered = 0                             # 꽉 차서 번호를 주지 못한 자료 수(도구 결과 하나 안에서)
        self._shown: set[int] = set()                    # 이번 질문의 검색 결과로 이미 자세히 넘긴 번호
        self._repeats = 0                                # 그래서 짧게 줄여 넘긴 자료 수(도구 결과 하나 안에서)

    def register(self, url: str, title: str, item: dict[str, Any] | None = None, tool: str = '') -> int:
        """주소에 번호를 준다(이미 있으면 그 번호). 꽉 찼으면 0."""
        known = self._by_url.get(url)
        if known is not None:
            if item and not known.get('snippet'):            # 목록에서 먼저 본 자료를 상세로 다시 보면 요약을 채운다
                known['snippet'] = _snippet(item)
            return known['n']
        if len(self.items) >= self.limit:
            return 0
        number = len(self.items) + 1
        if self.items and max(item['n'] for item in self.items) != len(self.items):      # 덜어 낸 자리가 있으면 가장 작은 빈 번호를 쓴다
            used = {item['n'] for item in self.items}
            number = next(candidate for candidate in range(1, len(used) + 2) if candidate not in used)
        source = {'n': number, 'url': url, 'title': ' '.join(str(title).split())[:200] or url, 'site': _site(url),
                  'facts': _facts(item or {}), 'snippet': _snippet(item or {}), 'tool': tool}
        self.items.append(source)
        self._by_url[url] = source
        self.fresh.append(source)
        return source['n']

    def prune(self, keep: set[int]) -> None:
        """keep 에 없는 번호의 출처를 덜어 낸다(목록은 그 자리에서 고친다: 대화가 같은 목록을 들고 있다)."""
        self.items[:] = [item for item in self.items if item['n'] in keep]
        self._by_url = {item['url']: item for item in self.items}

    def take_fresh(self) -> list[dict[str, Any]]:
        fresh, self.fresh = self.fresh, []
        return fresh

    def observe(self, tool: str, arguments: dict[str, Any], text: str, is_error: bool) -> tuple[str, dict[str, Any]]:
        """도구 결과를 보고 출처를 등록한다. 돌려주는 것: (번호를 적어 넣은 결과 글, 화면용 요약)."""
        if is_error:
            return text, {'error': ' '.join(text.split())[:300]}
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        summary: dict[str, Any] = {}
        if not isinstance(data, (dict, list)):
            return text, summary
        found: list[dict[str, Any]] = []
        self._unnumbered = self._repeats = 0
        self._walk(data, tool, found, depth=0)
        if isinstance(data, dict):
            if not found and _http(arguments.get('url')) and isinstance(data.get('title'), str):     # 링크를 연 결과
                number = self.register(_http(arguments['url']), data['title'], data, tool)
                if number:
                    data = {'cite': number, **data}
                    found.append({'n': number, 'title': data['title']})
            for key in ('total', 'count'):
                if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool):
                    summary[key] = int(data[key])
            groups = data.get('groups') or data.get('counts')
            if isinstance(groups, list):
                summary['groups'] = len(groups)
        if self._unnumbered and isinstance(data, dict):
            # 실측: 번호를 못 받은 모델이 공고 번호를 [111253] 처럼 지어 적어 링크가 사라졌다
            data['cite_note'] = (f'출처 번호가 다 차서 {self._unnumbered}건에는 cite 가 없습니다. 그 자료를 답에 쓸 때는 번호를 지어내지 말고 '
                                 '그 자료의 url 을 [원문](주소) 모양의 링크로 적으세요.')
        if self._repeats and isinstance(data, dict):
            data['seen_before_note'] = (f'seen_before 가 참인 {self._repeats}건은 이번 질문의 앞선 검색 결과에 같은 cite 번호로 자세히 실려 있어 '
                                        '여기서는 이름만 실었습니다.')
        if found or self._unnumbered:
            if found:
                summary['items'] = found[:MAX_SUMMARY_ITEMS]
                summary['more'] = max(0, len(found) - MAX_SUMMARY_ITEMS)
            text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))       # 번호(cite)를 넣은 채로, 공백 없이
        return text, summary

    def _walk(self, node: Any, tool: str, found: list[dict[str, Any]], depth: int) -> None:
        if depth > 4:
            return
        if isinstance(node, list):
            for child in node:
                self._walk(child, tool, found, depth + 1)
        elif isinstance(node, dict):
            title = node.get('title')
            url = _address(node) if isinstance(title, str) and title.strip() else ''
            if url:
                own = bool(_http(node.get('url'))) or 'notice_id' in node
                # 논문·특허처럼 제 주소가 없고 과제 주소만 있는 자료는 그 과제를 출처로 삼는다(제목도 과제 이름으로)
                number = self.register(url, title, node, tool) if own else self.register(
                    url, node.get('project_title') or title, {key: node[key] for key in ('ministry', 'program', 'organization') if key in node}, tool)
                if number:
                    node_items = list(node.items())
                    node.clear()
                    node['cite'] = number                    # 모델이 이 자료를 가리킬 번호(맨 앞에 둔다)
                    node.update(node_items)
                    if not any(entry['n'] == number for entry in found):
                        found.append({'n': number, 'title': ' '.join(title.split())[:120]})
                    if tool.startswith('search_'):
                        # 주제를 나눠 여러 번 검색하면 같은 공고가 거듭 나온다(실측: 검색 5번의 382건 가운데 206건만 서로 다른 공고).
                        # 모두 다시 실으면 질문 하나에 넘길 수 있는 양을 검색이 다 쓴다.
                        if number in self._shown:
                            kept = {key: node[key] for key in _REPEAT_KEYS if key in node}
                            node.clear()
                            node.update(cite=number, **kept, seen_before=True)
                            self._repeats += 1
                            return
                        self._shown.add(number)
                else:                                        # 꽉 찼다: 번호 대신 링크로 적을 수 있게 주소를 남긴다
                    self._unnumbered += 1
                    node.setdefault('url', url)
            for key, child in list(node.items()):
                if isinstance(child, (dict, list)) and key not in ('links', 'attachments'):
                    self._walk(child, tool, found, depth + 1)

    def note(self, cited: set[int]) -> str:
        """앞선 답에서 이미 쓰인 번호를 모델에 알려 주는 글(이어지는 질문에서 같은 번호로 가리킬 수 있게)."""
        lines = [f'[{item["n"]}] {item["title"]} — {item["url"]}' for item in self.items if item['n'] in cited][-60:]
        return ('\n\n이 대화의 앞선 답에서 쓰인 출처 번호(같은 자료는 같은 번호로 가리킵니다):\n' + '\n'.join(lines)) if lines else ''


def cited_numbers(text: str) -> set[int]:
    return {int(number) for match in _MARKER.finditer(text or '') for number in match.group(1).split(',')}


def renumber(text: str, items: list[dict[str, Any]]) -> tuple[str, list[tuple[int, dict[str, Any]]]]:
    """답의 [번호] 를 처음 나온 순서대로 1, 2, 3 … 으로 바꾼다(화면에 보이는 번호와 같게).

    돌려주는 것: (바꾼 글, [(새 번호, 자료), …]). 목록에 없는 번호는 그대로 둔다.
    """
    known = {item['n']: item for item in items}
    order: dict[int, int] = {}
    for match in _MARKER.finditer(text or ''):
        for number in (int(part) for part in match.group(1).split(',')):
            if number in known and number not in order:
                order[number] = len(order) + 1

    def swap(match: re.Match) -> str:
        return '[' + ', '.join(str(order.get(int(part), int(part))) for part in match.group(1).split(',')) + ']'
    return _MARKER.sub(swap, text or ''), [(shown, known[number]) for number, shown in order.items()]
