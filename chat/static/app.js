// 채팅 화면. 모델의 답과 도구 결과는 신뢰할 수 없는 글이므로 innerHTML 을 쓰지 않고 DOM 노드로만 그린다.
'use strict';

const API_HEADERS = {'Content-Type': 'application/json', 'X-Requested-With': 'rnd-chat'};
const SVG = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);
const root = document.documentElement;
const state = {conversation: null, conversations: [], busy: false, controller: null, limit: 4000, name: '', attachments: [], uploading: 0,
               sources: new Map(),           // sources: 이 대화에서 도구가 찾은 자료(번호 → 자료). 답의 [번호] 가 가리킨다.
               view: null, title: '', kind: '', desk: null};     // view: 메뉴에서 연 화면(search | tasks | docs | tools …). 없으면 대화를 보고 있다. desk: 비서의 상태
const KIND = {pdf: 'PDF', docx: '워드 문서', xlsx: '엑셀', hwp: '한글 문서', hwpx: '한글 문서'};
const hasTable = (text) => /\|[^\n]*\n\s*\|?\s*:?-{2,}/.test(text || '');
const TOOLS = {
  search: '공고 검색', fetch: '공고 본문 읽기', search_notices: '진행 중 공고 검색', get_notice: '공고 상세 확인', read_document: '첨부 문서 읽기',
  open_link: '링크 열기', view_link: '링크 화면 보기', search_history: '공고 이력 검색', count_history: '공고 이력 집계',
  get_history_notice: '이력 공고 상세 확인', read_history_document: '이력 첨부 읽기', view_history_document: '이력 첨부 보기',
  history_status: '이력 자료 범위 확인', search_projects: 'NTIS 과제 검색', count_projects: 'NTIS 과제 집계', get_project: 'NTIS 과제 상세 확인',
  search_results: 'NTIS 성과 검색', count_results: 'NTIS 성과 집계', search_reports: 'NTIS 연구보고서 검색', lookup_term: 'NTIS 용어사전 조회',
  save_task: '맡긴 일 저장', delete_task: '맡긴 일 지우기', save_note: '메모 적기', delete_note: '메모 지우기', lookup_class_code: 'NTIS 분류코드 조회', ntis_status: 'NTIS 연결 확인', create_document: '워드 문서 만들기', create_spreadsheet: '엑셀 파일 만들기',
  edit_document: '문서 고치기', inspect_document: '문서 들여다보기', review_all: '전체 결과 확인', related_terms: 'NTIS 가까운 검색어',
};
const SERVERS = {bizinfo: '진행 중 공고', history: '공고 이력', ntis: 'NTIS 과제·성과', linkreader: '링크·첨부 읽기'};
const ARGS = {
  query: '검색어', query_text: '검색어', limit: '개수', offset: '건너뛰기', year_from: '시작 연도', year_to: '끝 연도', ministry: '부처', organization: '기관',
  region: '지역', category: '분야', group_by: '묶는 기준', notice_id: '공고 번호', project_number: '과제 번호', url: '주소', kind: '종류', file_id: '파일',
  filename: '파일 이름', edits: '고칠 곳', sheets: '시트', blocks: '내용', markdown: '내용', status: '상태', sort: '정렬', search_in: '찾는 범위', table: '표',
  pages: '쪽', title: '제목', what: '할 일', days: '요일', time: '시각', notify: '알림', mail: '메일', paused: '멈춤', task_id: '일', note_id: '메모', text: '내용', from_paragraph: '시작 문단', attachment_id: '첨부', publisher: '발행처', program: '사업', id: '번호', agency: '소관',
  and_text: '함께 있는 말', criteria: '조건', tool: '검색 도구', arguments: '검색 조건', words: '낱말',
};

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === 'class') node.className = value; else if (key === 'text') node.textContent = value; else node.setAttribute(key, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function icon(name) {
  const svg = document.createElementNS(SVG, 'svg'), use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.setAttribute('aria-hidden', 'true');
  svg.append(use);
  return svg;
}

function iconButton(name, label, handler) {
  const button = el('button', {type: 'button', class: 'icon', 'aria-label': label, title: label}, icon(name));
  button.addEventListener('click', handler);
  return button;
}

function seal() {
  const mark = icon('logo');
  mark.classList.add('logo');
  return mark;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (response.status === 401) { location.assign('/login'); throw new Error('login'); }
  return response;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);            // https 나 localhost 에서만 된다
  } catch {
    const area = el('textarea', {class: 'offscreen'});     // 내부망 http 주소에서는 옛 방식으로
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
}

function flash(button, name) {                             // 복사 버튼을 잠깐 체크 표시로
  button.replaceChildren(icon('check'));
  setTimeout(() => button.replaceChildren(icon(name)), 1200);
}

// ── 마크다운을 DOM 으로 ────────────────────────────────────────────────
function safeUrl(url) {
  try {
    const parsed = new URL(url, location.origin);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null;
  } catch { return null; }
}

function link(text, url) {
  const href = safeUrl(url);
  return href ? el('a', {href, target: '_blank', rel: 'noopener noreferrer', text}) : document.createTextNode(text);
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\((?:[^()\s]|\([^()\s]*\))+\))|(https?:\/\/[^\s<>()"']+[^\s<>()"'.,;:!?])/g;
const CITE = /((?:\[\d{1,3}(?:\s*,\s*\d{1,3})*\])+)(?!\()/g;       // [3] · [3][5] · [3, 5]  (마크다운 링크 [글](주소) 는 아니다)

function plain(parent, text) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    const token = match[0];
    if (match[1]) parent.append(el('code', {text: token.slice(1, -1)}));
    else if (match[2]) parent.append(plain(el('strong'), token.slice(2, -2)));
    else if (match[3]) { const cut = token.indexOf(']('); parent.append(link(token.slice(1, cut), token.slice(cut + 2, -1))); }
    else parent.append(link(token, token));
    last = match.index + token.length;
  }
  if (last < text.length) parent.append(text.slice(last));
  return parent;
}

function sentenceStart(text) {
  // 형광펜을 시작할 자리: 번호 바로 앞 문장의 처음. 굵은 글·링크 한가운데나 날짜("2026. 12. 17.")에서는 끊지 않는다.
  const spans = [...text.matchAll(INLINE)].map((match) => [match.index, match.index + match[0].length]);
  let start = 0;
  for (const match of text.matchAll(/[.!?](\s+)(?=\S)/g)) {
    const at = match.index;
    if (text[at] === '.' && /\d/.test(text[at - 1] || '')) continue;
    if (spans.some(([from, to]) => at >= from && at < to)) continue;
    start = at + 1 + match[1].length;
  }
  return start;
}

// 도구가 매긴 번호는 대화 전체에서 이어지므로 크고 들쭉날쭉하다. 화면에는 답마다 처음 나온 순서대로 1, 2, 3 … 으로 보여 준다.
let shownAs = new Map();
const shown = (number) => String(shownAs.get(number) || number);

function citedNumbers(text) {                               // 답에서 가리킨 자료 번호(처음 나온 순서)
  const numbers = [];
  for (const match of (text || '').matchAll(CITE)) {
    for (const digits of match[1].match(/\d+/g)) if (state.sources.has(Number(digits)) && !numbers.includes(Number(digits))) numbers.push(Number(digits));
  }
  return numbers;
}

function refChip(number) {
  const source = state.sources.get(number);
  return el('a', {class: 'ref', href: safeUrl(source.url) || '#', target: '_blank', rel: 'noopener noreferrer', 'data-n': String(number), text: shown(number),
                  'aria-label': `출처 ${shown(number)}: ${source.title}`});
}

function inline(parent, text) {
  // 출처 번호가 붙은 문장은 형광펜으로 칠하고 번호를 단다. 모르는 번호는 글 그대로 둔다.
  let last = 0;
  for (const match of text.matchAll(CITE)) {
    const numbers = [...new Set(match[1].match(/\d+/g).map(Number))].filter((number) => state.sources.has(number));
    if (!numbers.length) continue;
    const segment = text.slice(last, match.index), cut = sentenceStart(segment);
    plain(parent, segment.slice(0, cut));
    const marked = segment.slice(cut).replace(/\s+$/, '');
    if (marked.trim()) parent.append(plain(el('span', {class: 'hl', tabindex: '0', role: 'link', 'data-n': numbers.join(' ')}), marked));
    for (const number of numbers) parent.append(refChip(number));
    last = match.index + match[0].length;
  }
  plain(parent, text.slice(last));
  return parent;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
const isRule = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
const LIST = /^\s*([-*•]|\d+[.)])\s+/;

// 글자 폭으로 센 길이: 한글·한자는 두 칸, 나머지는 한 칸(화면 글꼴이 고정폭이라 칸 수가 곧 너비다).
const columns = (text) => { let n = 0; for (const ch of text) n += ch.codePointAt(0) >= 0x1100 ? 2 : 1; return n; };

function markdown(text) {
  shownAs = new Map(citedNumbers(text).map((number, index) => [number, index + 1]));
  const fragment = document.createDocumentFragment();
  const lines = text.replace(/\r/g, '').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.trim().startsWith('```')) {                         // 코드 블록
      const language = line.trim().slice(3).trim(), body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith('```'); i++) body.push(lines[i]);
      i++;
      const code = body.join('\n');
      const copy = iconButton('copy', '코드 복사', () => { copyText(code); flash(copy, 'copy'); });
      fragment.append(el('div', {class: 'code'}, el('div', {class: 'code-head'}, el('span', {text: language || '코드'}), copy),
                         el('pre', null, el('code', {text: code}))));
    } else if (/^#{1,6}\s/.test(line)) {                          // 제목
      const level = Math.min(line.match(/^#+/)[0].length + 2, 5);
      fragment.append(inline(el('h' + level), line.replace(/^#+\s*/, '').replace(/\s*#+\s*$/, '')));
      i++;
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {       // 가로줄
      fragment.append(el('hr')); i++;
    } else if (line.includes('|') && i + 1 < lines.length && isRule(lines[i + 1])) {   // 표
      const head = el('tr');
      for (const cell of cells(line)) head.append(inline(el('th'), cell));
      const body = el('tbody');
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) {
        const row = el('tr');
        for (const cell of cells(lines[i])) row.append(inline(el('td', columns(cell) <= 26 ? {class: 'tight'} : null), cell));   // 짧은 칸(날짜·금액)은 줄을 바꾸지 않는다
        body.append(row);
      }
      fragment.append(el('div', {class: 'table'}, el('table', null, el('thead', null, head), body)));
    } else if (/^\s*>/.test(line)) {                              // 인용
      const quote = el('blockquote');
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) {
        const content = lines[i].replace(/^\s*>\s?/, '');
        if (content.trim()) quote.append(inline(el('p'), content));
      }
      fragment.append(quote);
    } else if (LIST.test(line)) {                                 // 목록(들여쓰기로 한 단계 중첩)
      const ordered = /^\s*\d+[.)]\s+/.test(line), list = el(ordered ? 'ol' : 'ul'), base = line.search(/\S/);
      let item = null, nested = null;
      for (; i < lines.length && LIST.test(lines[i]); i++) {
        const content = lines[i].replace(LIST, '');
        if (lines[i].search(/\S/) > base + 1 && item) {
          if (!nested) { nested = el(/^\s*\d+[.)]\s+/.test(lines[i]) ? 'ol' : 'ul'); item.append(nested); }
          nested.append(inline(el('li'), content));
        } else {
          item = inline(el('li'), content);
          nested = null;
          list.append(item);
        }
      }
      fragment.append(list);
    } else {                                                       // 문단
      const paragraph = el('p');
      let first = true;
      for (; i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*>|\s*```)/.test(lines[i]) && !LIST.test(lines[i])
             && !(lines[i].includes('|') && i + 1 < lines.length && isRule(lines[i + 1])); i++) {
        if (!first) paragraph.append(el('br'));
        inline(paragraph, lines[i]);
        first = false;
      }
      fragment.append(paragraph);
    }
  }
  return fragment;
}

// ── 출처 카드(형광펜에 마우스를 올리면 뜬다) ───────────────────────────
const card = {timer: 0, number: null, scope: null};

function light(scope, number, on) {
  if (!scope) return;
  for (const node of scope.querySelectorAll('[data-n]')) if (node.dataset.n.split(' ').includes(String(number))) node.classList.toggle('on', on);
}

function showCard(anchor, number, x, y) {
  const source = state.sources.get(number), box = $('sourcecard');
  if (!source) return;
  const label = (anchor.closest('.msg') && anchor.closest('.msg').querySelector(`.ref[data-n="${number}"]`) || {}).textContent || '';
  clearTimeout(card.timer);
  if (card.number !== null) light(card.scope, card.number, false);
  card.number = number;
  card.scope = anchor.closest('.msg');
  light(card.scope, number, true);
  const facts = el('dl', {class: 'card-facts'});
  for (const [label, value] of source.facts || []) facts.append(el('dt', {text: label}), el('dd', {text: value, title: value}));
  const href = safeUrl(source.url) || '#';
  box.replaceChildren(...[                                 // 없는 부분(null)은 걸러 낸다 — 그대로 넘기면 'null' 이라는 글자로 찍힌다
    el('div', {class: 'card-top'}, el('span', {text: source.site || '자료'}), label ? el('span', {class: 'ref', text: label}) : null),
    el('div', {class: 'card-title', text: source.title}),
    (source.facts || []).length ? facts : null,
    source.snippet ? el('p', {class: 'card-snippet', text: source.snippet}) : null,
    el('a', {class: 'card-open', href, target: '_blank', rel: 'noopener noreferrer'}, el('span', {text: href.replace(/^https?:\/\//, '')}), el('span', {text: '새 탭에서 열기'}), icon('out'))].filter(Boolean));
  box.hidden = false;
  // 가리킨 줄 바로 아래(자리가 없으면 위)에, 화면 밖으로 나가지 않게 놓는다
  const rects = [...anchor.getClientRects()];
  const rect = rects.find((item) => y >= item.top - 2 && y <= item.bottom + 2) || rects[0] || anchor.getBoundingClientRect();
  const width = box.offsetWidth, height = box.offsetHeight, margin = 12;
  const left = Math.max(margin, Math.min((x === undefined ? rect.left : x) - 36, innerWidth - width - margin));
  let top = rect.bottom + 8;
  if (top + height > innerHeight - margin) top = Math.max(margin, rect.top - height - 8);
  box.style.left = left + 'px';
  box.style.top = top + 'px';
}

function hideCard(now) {
  clearTimeout(card.timer);
  card.timer = setTimeout(() => {
    $('sourcecard').hidden = true;
    if (card.number !== null) light(card.scope, card.number, false);
    card.number = null;
  }, now ? 0 : 180);
}

function citeTarget(event) {
  const node = event.target.closest ? event.target.closest('[data-n]') : null;
  return node && node.closest('#thread') ? node : null;
}

function bindCitations() {
  const thread = $('thread'), box = $('sourcecard');
  thread.addEventListener('pointerover', (event) => {
    const node = citeTarget(event);
    if (node && event.pointerType !== 'touch') showCard(node, Number(node.dataset.n.split(' ')[0]), event.clientX, event.clientY);
  });
  thread.addEventListener('pointerout', (event) => { if (citeTarget(event)) hideCard(); });
  thread.addEventListener('focusin', (event) => { const node = citeTarget(event); if (node) showCard(node, Number(node.dataset.n.split(' ')[0])); });
  thread.addEventListener('focusout', (event) => { if (citeTarget(event)) hideCard(); });
  thread.addEventListener('click', (event) => {
    const node = citeTarget(event);
    if (!node || !node.classList.contains('hl') || event.target.closest('a')) return;
    const number = Number(node.dataset.n.split(' ')[0]), source = state.sources.get(number);
    if (matchMedia('(hover: none)').matches && card.number !== number) return showCard(node, number, event.clientX, event.clientY);   // 터치 화면: 먼저 카드를 보여 준다
    const href = source && safeUrl(source.url);
    if (href) window.open(href, '_blank', 'noopener,noreferrer');
  });
  thread.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || !event.target.classList || !event.target.classList.contains('hl')) return;
    const source = state.sources.get(Number(event.target.dataset.n.split(' ')[0])), href = source && safeUrl(source.url);
    if (href) window.open(href, '_blank', 'noopener,noreferrer');
  });
  box.addEventListener('pointerenter', () => clearTimeout(card.timer));
  box.addEventListener('pointerleave', () => hideCard());
  $('scroll').addEventListener('scroll', () => { if (!box.hidden) hideCard(true); }, {passive: true});
  document.addEventListener('pointerdown', (event) => { if (!box.hidden && !box.contains(event.target) && !citeTarget(event)) hideCard(true); });
}

function renderSources(view) {
  const numbers = citedNumbers(view.text);
  shownAs = new Map(numbers.map((number, index) => [number, index + 1]));
  if (view.sourceBox) view.sourceBox.remove();
  view.sourceBox = null;
  if (!numbers.length) return;
  const LIMIT = 5, box = el('div', {class: 'sources'}, el('span', {class: 'sources-label', text: `출처 ${numbers.length}`}));
  const chip = (number) => {
    const source = state.sources.get(number);
    return el('a', {class: 'source', href: safeUrl(source.url) || '#', target: '_blank', rel: 'noopener noreferrer', 'data-n': String(number)},
              el('span', {class: 'ref', text: shown(number)}), el('span', {text: source.title}));
  };
  for (const number of numbers.slice(0, LIMIT)) box.append(chip(number));
  if (numbers.length > LIMIT) {
    const more = el('button', {type: 'button', class: 'more', text: `외 ${numbers.length - LIMIT}건`});
    more.addEventListener('click', () => more.replaceWith(...numbers.slice(LIMIT).map(chip)));
    box.append(more);
  }
  view.sourceBox = box;
  view.files.before(box);
}

// ── 대화 목록 ───────────────────────────────────────────────────────────
function groupOf(updated) {
  const day = 86400000, start = new Date(); start.setHours(0, 0, 0, 0);
  const age = start.getTime() - updated * 1000;
  if (age <= 0) return '오늘';
  if (age <= day) return '어제';
  if (age <= 7 * day) return '지난 7일';
  if (age <= 30 * day) return '지난 30일';
  return '이전';
}

function renderList() {
  const list = $('list');
  list.replaceChildren();
  let group = null;
  for (const item of state.conversations) {
    if (item.kind === 'assistant') continue;                 // 비서 대화는 메뉴의 비서로 연다
    const name = groupOf(item.updated);
    if (name !== group) { list.append(el('p', {class: 'group', text: name})); group = name; }
    list.append(conversationRow(item));
  }
  for (const button of document.querySelectorAll('.side-nav [data-view]')) {
    button.classList.toggle('current', button.dataset.view === state.view);
  }
  $('nav-desk').classList.toggle('current', onDesk());
}

function conversationRow(item) {
  const open = el('button', {type: 'button', class: 'open', title: item.title, text: item.title || '대화'});
  open.addEventListener('click', () => openConversation(item.id));
  const row = el('div', {class: 'item' + (item.id === state.conversation && !state.view ? ' current' : '')}, open);
  const rename = iconButton('edit', '제목 바꾸기', () => {
    const input = el('input', {class: 'rename', maxlength: '80', 'aria-label': '대화 제목'});
    input.value = item.title;
    let finished = false;
    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const title = input.value.trim();
      if (save && title && title !== item.title) {
        const response = await api('/api/conversations/' + encodeURIComponent(item.id), {method: 'PATCH', headers: API_HEADERS, body: JSON.stringify({title})});
        if (response.ok) { item.title = (await response.json()).title; if (item.id === state.conversation) setTitle(item.title); }
      }
      renderList();
    };
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') finish(true); else if (event.key === 'Escape') finish(false); });
    input.addEventListener('blur', () => finish(true));
    row.replaceChildren(input);
    input.focus();
    input.select();
  });
  const remove = iconButton('trash', '대화 지우기', async () => {
    if (!confirm(`"${item.title}" 대화를 지울까요?`)) return;
    await api('/api/conversations/' + encodeURIComponent(item.id), {method: 'DELETE', headers: API_HEADERS});
    if (state.conversation === item.id) newConversation();
    loadList();
  });
  row.append(el('span', {class: 'row-actions'}, rename, remove));
  return row;
}

async function loadList() {
  state.conversations = (await (await api('/api/conversations')).json()).conversations;
  renderList();
}

// ── 대화 화면 ───────────────────────────────────────────────────────────
function setTitle(title) {                                 // 메뉴에서 연 화면을 보는 동안에는 그 화면의 이름이 제목이다
  if (title !== undefined) state.title = title || '';
  const shown = state.view ? VIEWS[state.view].title : state.title;
  $('title').textContent = shown || '새 대화';
  document.title = shown ? `${shown} · R&D 검색` : 'R&D 검색';
}

function nearBottom() {
  const box = $('scroll');
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
}

function scrollDown(force) {
  const box = $('scroll');
  if (force || nearBottom()) box.scrollTop = box.scrollHeight;
}

function clearThread() {
  $('thread').replaceChildren();
  document.body.classList.add('empty');
  hideCard(true);
}

function size(bytes) {
  return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + 'MB' : Math.max(1, Math.round(bytes / 1024)) + 'KB';
}

const fileUrl = (file, rest) => '/api/files/' + encodeURIComponent(file.id) + (rest || '');

function thumbnail(file, name) {                            // 첫 쪽 그림. 못 그리면 아이콘으로 남는다.
  const image = el('img', {class: name || '', src: fileUrl(file, '/pages/1?size=thumb'), alt: '', loading: 'lazy'});
  image.addEventListener('error', () => image.replaceWith(icon(file.kind === 'xlsx' ? 'sheet' : 'file')));
  return image;
}

function fileCard(file) {                                   // 올린 문서, 모델이 고치거나 만든 파일
  const view = el('button', {type: 'button', class: 'thumb', 'aria-label': `${file.name} 미리보기`, title: '미리보기'},
                  thumbnail(file), el('span', {class: 'kind', text: file.kind.toUpperCase()}));
  view.addEventListener('click', () => openViewer(file));
  const detail = [KIND[file.kind] || file.kind, file.pages ? `${file.pages}쪽` : '', size(file.size)].filter(Boolean).join(' · ');
  const main = el('div', {class: 'file-main'}, el('strong', {text: file.name, title: file.name}), el('span', {class: 'file-detail', text: detail}));
  if (file.origin === 'edited') {
    main.append(el('span', {class: 'file-badge', text: `원본에 ${file.applied}곳 고침`}));
    if (file.failed) main.append(el('span', {class: 'file-badge warn', text: `${file.failed}곳은 고치지 못함`}));
  }
  const preview = el('button', {type: 'button', class: 'pill'}, icon('eye'), el('span', {text: file.origin === 'edited' ? '확인하기' : '미리보기'}));
  preview.addEventListener('click', () => openViewer(file));
  main.append(el('div', {class: 'file-actions'}, preview,
                 el('a', {class: 'pill', href: fileUrl(file), download: file.name}, icon('download'), el('span', {text: '내려받기'}))));
  const node = el('div', {class: 'filecard ' + file.kind}, view, main);
  node.dataset.id = file.id;
  return node;
}

function addFile(view, file) {                               // 같은 문서를 다시 고친 것이면 앞의 카드를 갈아 끼운다
  const earlier = file.previous && [...view.files.children].find((node) => node.dataset.id === file.previous);
  if (earlier) earlier.replaceWith(fileCard(file)); else view.files.append(fileCard(file));
}

function addUser(text, files) {
  document.body.classList.remove('empty');
  const node = el('article', {class: 'msg user'});
  if (files && files.length) node.append(el('div', {class: 'files'}, ...files.map(fileCard)));
  node.append(el('div', {class: 'bubble', text}));
  $('thread').append(node);
  return node;
}

function addAuto(auto, index) {                                    // 맡긴 일을 한 차례: 말풍선 대신 일의 이름과 날짜를 이름표로 보인다
  document.body.classList.remove('empty');
  const day = /^\d{4}-(\d{2})-(\d{2})/.exec(auto.day || '');
  $('thread').append(el('div', {class: 'auto', 'data-at': String(index)}, icon('clock'), el('strong', {text: auto.title || '맡긴 일'}), el('span', {text: day ? `${Number(day[1])}월 ${Number(day[2])}일` : ''})));
  return null;
}

function addAssistant(message, index) {
  document.body.classList.remove('empty');
  message = message || {};
  const body = el('div', {class: 'body'}), meta = el('span', {class: 'meta-info'}), actions = el('div', {class: 'actions'}), made = el('div', {class: 'files'});
  const node = el('article', {class: 'msg assistant'}, seal(),
                  el('div', {class: 'content'}, el('div', {class: 'meta'}, el('strong', {text: 'R&D 검색'}), meta), body, made, actions));
  const view = {node, body, meta, actions, files: made, text: message.content || '', steps: null, stepList: null, sourceBox: null,
                index: index === undefined ? null : index};
  $('thread').append(node);
  for (const step of message.steps || []) { addStep(view, step); finishStep(view, step); }
  if (message.steps && message.steps.length) finishSteps(view, false);
  for (const file of message.files || []) made.append(fileCard(file));
  if (message.seconds) meta.textContent = metaText(message.seconds, (message.steps || []).length);
  if (view.text) { body.append(markdown(view.text)); renderSources(view); addActions(view); }
  return view;
}

const metaText = (seconds, steps) => `${Number(seconds).toFixed(1)}초` + (steps ? ` · 작업 ${steps}단계` : '');

function exportLink(view, format, name, label) {
  const href = `/api/conversations/${encodeURIComponent(state.conversation)}/export?index=${view.index}&format=${format}`;
  return el('a', {class: 'icon', href, download: '', 'aria-label': label, title: label}, icon(name));
}

function addActions(view) {
  const copy = iconButton('copy', '답 복사', () => {
    const numbers = citedNumbers(view.text), order = new Map(numbers.map((number, index) => [number, index + 1]));
    const body = view.text.replace(CITE, (whole) => whole.replace(/\d+/g, (digits) => order.get(Number(digits)) || digits));   // 화면에 보이는 번호로 맞춘다
    const cited = numbers.map((number) => `[${order.get(number)}] ${state.sources.get(number).title} — ${state.sources.get(number).url}`);
    copyText(body + (cited.length ? '\n\n출처\n' + cited.join('\n') : ''));
    flash(copy, 'copy');
  });
  view.actions.replaceChildren(copy);
  if (state.conversation && view.index !== null) {
    view.actions.append(exportLink(view, 'docx', 'file', '워드 문서로 저장'));
    if (hasTable(view.text)) view.actions.append(exportLink(view, 'xlsx', 'sheet', '표를 엑셀로 저장'));
  }
  for (const old of $('thread').querySelectorAll('.regenerate')) old.remove();     // 다시 생성은 마지막 답에만
  const made = (state.kind === 'brief' && view.index === 1) || view.auto;      // 브리핑 글 자체는 다시 생성하지 않는다(브리핑 화면의 지금 받기로 새로 만든다)
  if (state.conversation && view.node === $('thread').lastElementChild && !made) {
    const again = iconButton('refresh', '다시 생성', () => regenerate(view));
    again.classList.add('regenerate');
    view.actions.append(again);
  }
}

// ── 작업 단계(모델이 어떤 도구를 어떻게 썼는지) ─────────────────────────
function brief(value) {
  if (Array.isArray(value)) return `${value.length}건`;
  if (value && typeof value === 'object') {              // 검색 조건처럼 한 겹 안에 든 값은 풀어서 보여 준다
    const inner = Object.entries(value).filter(([, item]) => ['string', 'number', 'boolean'].includes(typeof item) && item !== '').map(([key, item]) => `${ARGS[key] || key} ${item}`).join(', ');
    return inner ? (inner.length > 160 ? inner.slice(0, 160) + '…' : inner) : '{…}';
  }
  const text = TOOLS[value] || String(value);          // 도구 이름이 값으로 오면(전체 확인의 검색 도구) 한글 이름으로
  return text.length > 120 ? text.slice(0, 120) + '…' : text;
}

function resultText(summary, isError) {
  summary = summary || {};
  if (isError || summary.error) return '실패';
  if (summary.saved) return `${summary.applied}곳 고침` + (summary.failed ? ` · ${summary.failed}곳 실패` : '');
  if (summary.created) return '파일 만듦';
  if (summary.task) return summary.deleted ? '지움' : (summary.when || '저장');
  if (summary.note) return summary.deleted ? '지움' : '적음';
  if (summary.inspected) return summary.pages ? `${summary.pages}쪽 확인` : '확인';
  if (typeof summary.checked === 'number') return `${summary.checked.toLocaleString()}건 확인 · ${(summary.matched || 0).toLocaleString()}건 해당`;
  const found = (summary.items || []).length + (summary.more || 0);
  if (typeof summary.total === 'number') return `${summary.total.toLocaleString()}건` + (found && found < summary.total ? ` 중 ${found}건` : '');
  if (summary.groups) return `${summary.groups}개 묶음`;
  if (found) return `${found}건`;
  return '완료';
}

function addStep(view, event) {
  if (!view.steps) {
    view.stepList = el('ol');
    view.steps = el('details', {class: 'steps', open: ''},
                    el('summary', null, el('span', {class: 'spinner'}), el('span', {class: 'steps-label', text: '작업 중'}), icon('right')), view.stepList);
    view.steps.querySelector('summary svg').classList.add('caret');
    view.body.before(view.steps);
  }
  const args = Object.entries(event.arguments || event.args || {}).filter(([, value]) => value !== '' && value !== 0 && value !== null && value !== false);
  const headline = args.filter(([key]) => !['limit', 'offset', 'file_id', 'blocks', 'sheets', 'markdown', 'search_in', 'sort'].includes(key)).slice(0, 3).map(([, value]) => brief(value)).join(' · ');
  const list = el('dl');
  for (const [key, value] of args) list.append(el('dt', {text: ARGS[key] || key}), el('dd', {text: brief(value)}));
  const step = el('details', {class: 'step running'},
                  el('summary', null, el('span', {class: 'step-name', text: TOOLS[event.name] || event.name}),
                     headline ? el('span', {class: 'step-args', text: headline}) : null, el('span', {class: 'step-result'})),
                  el('div', {class: 'step-body'}, list));
  view.stepList.append(el('li', null, step));
  view.steps.querySelector('.steps-label').textContent = `${TOOLS[event.name] || event.name} 중`;
}

function finishStep(view, event) {
  const step = view.stepList && view.stepList.lastElementChild && view.stepList.lastElementChild.firstElementChild;
  if (!step) return;
  const summary = event.summary || {};
  step.className = 'step ' + (event.is_error ? 'error' : 'done');
  step.querySelector('.step-result').textContent = resultText(summary, event.is_error);
  const body = step.querySelector('.step-body');
  if (summary.error) body.append(el('p', {class: 'error', text: summary.error}));
  if ((summary.items || []).length) {
    const found = el('ul');
    for (const item of summary.items) {
      const source = state.sources.get(item.n);
      found.append(el('li', null, source ? link(item.title, source.url) : el('span', {text: item.title})));
    }
    if (summary.more) found.append(el('li', {class: 'note', text: `외 ${summary.more}건`}));
    body.append(found);
  }
}

function finishSteps(view, stopped) {
  if (!view.steps) return;
  const count = view.stepList.childElementCount;
  for (const item of view.stepList.querySelectorAll('.running')) item.className = 'step ' + (stopped ? 'stopped' : 'done');
  const mark = view.steps.querySelector('.spinner');
  if (mark) mark.replaceWith(icon(stopped ? 'alert' : 'check'));
  view.steps.querySelector('.steps-label').textContent = `작업 ${count}단계` + (stopped ? ' (중단)' : '');
  view.steps.removeAttribute('open');
}

async function openConversation(id) {
  if (state.busy) { if (id === state.conversation) setView(null); return; }
  const response = await api('/api/conversations/' + encodeURIComponent(id));
  if (!response.ok) return loadList();
  const item = await response.json();
  state.conversation = item.id;
  state.kind = item.kind || '';
  state.view = null;
  document.body.classList.remove('paged');
  state.sources = new Map((item.sources || []).map((source) => [source.n, source]));
  setTitle(item.title);
  clearThread();
  let last = null, auto = false;
  item.messages.forEach((message, index) => {
    if (item.kind === 'brief' && index === 0) return;          // 브리핑의 머리 글은 사용자가 쓴 것이 아니다
    if (message.role === 'user') { auto = Boolean(message.auto); last = auto ? addAuto(message.auto, index) : addUser(message.content, message.files); return; }
    last = addAssistant(message, index);
    last.auto = auto;
  });
  if (last && last.node) addActions(last);
  deskChrome();
  if (item.kind === 'assistant' && state.desk) { state.deskShown = state.desk.delivered; deskSeen(state.desk.delivered); }      // 어디까지 그렸고 어디까지 봤는지
  closeSidebarOnMobile();
  renderList();
  scrollDown(true);
}

function newConversation() {
  if (state.busy) return;
  state.conversation = null;
  state.kind = '';
  state.view = null;
  document.body.classList.remove('paged');
  state.sources = new Map();
  setTitle('');
  clearThread();
  deskChrome();
  closeSidebarOnMobile();
  renderList();
  $('input').focus();
}

function setBusy(busy) {
  state.busy = busy;
  $('sendicon').setAttribute('href', busy ? '#i-stop' : '#i-up');
  $('send').setAttribute('aria-label', busy ? '중단' : '보내기');
  $('send').title = busy ? '중단' : '보내기';
  $('send').classList.toggle('stop', busy);
  updateSend();
}

function updateSend() {
  const length = $('input').value.trim().length;
  $('send').disabled = !state.busy && (!length || length > state.limit || state.uploading > 0);
  const counter = $('counter');
  counter.hidden = $('input').value.length < state.limit * 0.8;
  counter.textContent = `${$('input').value.length} / ${state.limit}`;
  counter.classList.toggle('over', $('input').value.length > state.limit);
}

async function regenerate(view) {
  if (state.busy || !state.conversation) return;
  view.node.remove();
  await run({conversation_id: state.conversation, regenerate: true});
}

async function ask(question) {
  question = question.trim();
  if (state.busy || !question || question.length > state.limit) return;
  const attached = state.attachments;
  state.attachments = [];
  renderAttachments();
  addUser(question, attached);
  await run({message: question, conversation_id: state.conversation, attachments: attached.map((file) => file.id)});
}

async function run(payload) {
  setBusy(true);
  const view = addAssistant();
  view.node.classList.add('pending');
  const started = performance.now(), clock = el('span', {class: 'clock'});
  view.body.append(el('p', {class: 'thinking'}, el('span', {class: 'spinner'}), '생각하는 중', clock));
  const ticking = setInterval(() => { if (clock.isConnected) clock.textContent = `${Math.floor((performance.now() - started) / 1000)}초`; }, 1000);
  scrollDown(true);
  let buffer = '', scheduled = false, calls = 0, finished = false;
  const paint = () => {
    scheduled = false;
    if (finished) return;
    const stick = nearBottom();
    view.body.replaceChildren(markdown(buffer));
    if (stick) scrollDown(true);
  };
  const fail = (message) => {
    finished = true;
    finishSteps(view, true);
    view.node.classList.remove('pending');
    view.node.classList.add('failed');
    view.body.replaceChildren(buffer ? markdown(buffer) : '', el('p', {class: 'error'}, icon('alert'), message));
  };
  state.controller = new AbortController();
  try {
    const response = await api('/api/chat', {method: 'POST', headers: API_HEADERS, body: JSON.stringify(payload), signal: state.controller.signal});
    if (!response.ok) return fail((await response.json().catch(() => ({}))).error || '요청이 실패했습니다.');
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let pending = '';
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      pending += decoder.decode(value, {stream: true});
      let cut;
      while ((cut = pending.indexOf('\n\n')) >= 0) {
        const chunk = pending.slice(0, cut);
        pending = pending.slice(cut + 2);
        if (!chunk.startsWith('data: ')) continue;
        const event = JSON.parse(chunk.slice(6));
        if (event.type === 'queued') view.body.replaceChildren(el('p', {class: 'thinking'}, el('span', {class: 'spinner'}), '앞선 질문을 처리하는 중이라 잠시 기다립니다'));
        else if (event.type === 'delta') {
          buffer += event.text;
          if (!scheduled) { scheduled = true; requestAnimationFrame(paint); }
        } else if (event.type === 'reset') {
          buffer = '';
          view.body.replaceChildren();
        } else if (event.type === 'sources') {
          for (const source of event.sources) state.sources.set(source.n, source);
        } else if (event.type === 'tool_call') {
          calls++;
          const stick = nearBottom();
          addStep(view, event);
          if (!buffer) view.body.replaceChildren();
          if (stick) scrollDown(true);
        } else if (event.type === 'file') {
          const stick = nearBottom();
          addFile(view, event.file);
          if (stick) scrollDown(true);
        } else if (event.type === 'tool_progress') {
          const running = view.stepList && view.stepList.lastElementChild && view.stepList.lastElementChild.querySelector('.step.running .step-result');
          if (running) running.textContent = `${event.checked.toLocaleString()} / ${event.total.toLocaleString()}건`;
        } else if (event.type === 'tool_result') {
          finishStep(view, event);
        } else if (event.type === 'answer') {
          finished = true;
          const stick = nearBottom();
          view.text = event.text || '';
          finishSteps(view, false);
          view.node.classList.add('fresh');
          view.body.replaceChildren(markdown(view.text || '(빈 답)'));
          renderSources(view);
          if (event.stopped === 'max_steps') view.body.append(el('p', {class: 'note', text: '조회 횟수 상한에 걸려 여기까지 찾은 내용으로 답했습니다.'}));
          if (event.stopped === 'length') view.body.append(el('p', {class: 'note', text: '답이 길이 상한에서 끊겼습니다.'}));
          view.node.classList.remove('pending');
          view.meta.textContent = metaText((performance.now() - started) / 1000, calls);
          if (stick) scrollDown(true);
        } else if (event.type === 'done') {
          state.conversation = event.conversation_id;
          view.index = event.index;
          setTitle(event.title);
          addActions(view);
        } else if (event.type === 'error') fail(event.message);
      }
    }
    if (!finished) fail('답을 끝까지 받지 못했습니다. 다시 시도해 주세요.');
  } catch (error) {
    if (error.name === 'AbortError') {
      finished = true;
      finishSteps(view, true);
      view.node.classList.remove('pending');
      view.body.replaceChildren(buffer ? markdown(buffer) : '', el('p', {class: 'note', text: '답을 중단했습니다. 이 질문은 대화에 저장되지 않았습니다.'}));
    } else if (error.message !== 'login') fail('연결이 끊겼습니다. 다시 시도해 주세요.');
  } finally {
    clearInterval(ticking);
    state.controller = null;
    setBusy(false);
    loadList().catch(() => {});
    $('input').focus();
  }
}

// ── 첨부 ────────────────────────────────────────────────────────────────
function attachedInfo(file) {
  const parts = [file.kind.toUpperCase(), file.pages ? `${file.pages}쪽` : ''];
  if (file.editable) parts.push('원본을 직접 고칩니다');
  else if (file.kind === 'pdf') parts.push('모양을 따라 새 문서로 만듭니다');
  else if (file.edit_error) parts.push('읽기만 됩니다');
  return parts.filter(Boolean).join(' · ');
}

function renderAttachments(message) {
  const box = $('attachments');
  box.replaceChildren();
  for (const file of state.attachments) {
    const remove = iconButton('x', '첨부 빼기', () => { state.attachments = state.attachments.filter((item) => item !== file); renderAttachments(); });
    const mini = thumbnail(file, 'mini');
    mini.addEventListener('click', () => openViewer(file));
    box.append(el('span', {class: 'attached'}, mini, el('span', {class: 'attached-text'}, el('span', {class: 'attached-name', text: file.name, title: file.name}),
                                                          el('span', {class: 'attached-info', text: attachedInfo(file)})), remove));
  }
  if (state.uploading) box.append(el('span', {class: 'attached busy'}, el('span', {class: 'spinner'}), el('span', {text: '올리는 중… 쪽 모양을 그리느라 몇 초 걸립니다'})));
  if (message) box.append(el('span', {class: 'attached error'}, icon('alert'), el('span', {text: message})));
  updateSend();
}

async function upload(file) {
  if (!file) return;
  if (state.attachments.length >= 3) return renderAttachments('첨부는 3개까지입니다.');
  if (!/\.(pdf|hwp|hwpx|docx)$/i.test(file.name)) return renderAttachments('HWP, HWPX, DOCX, PDF 파일만 첨부할 수 있습니다.');
  if (file.size > 15 * 1024 * 1024) return renderAttachments('파일이 너무 큽니다(15MB 이하).');
  state.uploading++;
  renderAttachments();
  let message = '';
  try {
    const form = new FormData();
    form.append('file', file, file.name);
    const response = await api('/api/files', {method: 'POST', headers: {'X-Requested-With': 'rnd-chat'}, body: form});
    const result = await response.json().catch(() => ({}));
    if (response.ok) state.attachments.push(result); else message = result.error || '파일을 올리지 못했습니다.';
  } catch (error) {
    if (error.message !== 'login') message = '파일을 올리지 못했습니다.';
  }
  state.uploading--;
  renderAttachments(message);
  $('input').focus();
}

// ── 문서 미리보기(쪽 그림, 원본과 비교, 바뀐 내용) ──────────────────────
const viewing = {file: null, pages: 0, original: false, observer: null};

function changeRow(change) {
  const where = {
    set_cell: () => `표 ${Number(change.table) + 1} · ${Number(change.row) + 1}행 ${Number(change.col) + 1}열`,
    replace_text: () => `글 바꾸기` + (change.replaced ? ` · ${change.replaced}곳` : ''),
    set_paragraph: () => `본문 ${Number(change.paragraph) + 1}번째 문단`,
    add_paragraphs: () => `본문 ${Number(change.after) + 1}번째 문단 뒤에 ${change.added || ''}줄 추가`,
    add_row: () => `표 ${Number(change.table) + 1} · ${Number(change.row) + 1}행 아래에 행 추가`,
    fill_field: () => `입력 칸 "${change.name}"`,
  }[change.op];
  const row = el(change.page ? 'button' : 'div', {class: 'change' + (change.ok ? '' : ' failed'), ...(change.page ? {type: 'button', title: `${change.page}쪽으로 가기`} : {})},
                 el('div', {class: 'where'}, el('span', {text: (where ? where() : change.op) + (change.ok ? '' : ' · 고치지 못함')}),
                    change.page ? el('span', {class: 'at', text: `${change.page}쪽`}) : null));
  if (change.page) row.addEventListener('click', () => goToPage(change.page));
  const before = change.op === 'replace_text' ? change.find : change.old, after = change.op === 'replace_text' ? change.replace : change.new;
  if (!change.ok) row.append(el('p', {text: change.error || ''}));
  else {
    if (before) row.append(el('p', null, el('span', {class: 'was', text: before})));
    if (after) row.append(el('p', null, el('span', {class: 'now', text: after})));
    if (change.overflow) row.append(el('p', {class: 'note', text: '칸보다 길어 줄이 넘어갈 수 있습니다.'}));
  }
  return row;
}

function pageUrl(number) {
  const file = viewing.original && viewing.file.root ? {id: viewing.file.root} : viewing.file;
  return fileUrl(file, `/pages/${number}?size=full`);
}

function currentPage() {
  const box = $('viewerpages'), top = box.getBoundingClientRect().top;
  let best = 1;
  for (const page of box.querySelectorAll('.page')) if (page.getBoundingClientRect().top - top < box.clientHeight / 2) best = Number(page.dataset.n);
  return best;
}

function goToPage(number) {
  const target = $('viewerpages').querySelector(`.page[data-n="${Math.max(1, Math.min(viewing.pages, number))}"]`);
  if (!target) return;
  target.scrollIntoView({block: 'start', behavior: 'smooth'});
  target.classList.remove('flash');
  void target.offsetWidth;
  target.classList.add('flash');                           // 찾아간 쪽을 잠깐 표시한다
}

function turnPage(delta) { goToPage(currentPage() + delta); }

function fillPages() {
  const box = $('viewerpages');
  if (viewing.observer) viewing.observer.disconnect();
  viewing.observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const image = entry.target.querySelector('img');
      if (!image.getAttribute('src')) image.src = pageUrl(Number(entry.target.dataset.n));
      viewing.observer.unobserve(entry.target);
    }
  }, {root: box, rootMargin: '900px 0px'});
  const keep = box.scrollTop;
  box.replaceChildren();
  for (let number = 1; number <= viewing.pages; number++) {
    const image = el('img', {alt: `${number}쪽`});
    const page = el('div', {class: 'page loading'}, image, el('span', {class: 'num', text: String(number)}));
    page.dataset.n = String(number);
    image.addEventListener('load', () => { page.classList.remove('loading'); image.classList.add('ready'); });
    image.addEventListener('error', () => { page.classList.remove('loading'); image.replaceWith(el('p', {class: 'notice', text: '이 쪽은 그리지 못했습니다.'})); });
    box.append(page);
    viewing.observer.observe(page);
  }
  box.scrollTop = keep;
  box.classList.toggle('original', viewing.original);
  $('viewerpage').textContent = `${currentPage()} / ${viewing.pages}`;
}

async function openViewer(file) {
  const dialog = $('viewer'), box = $('viewerpages');
  viewing.file = file; viewing.pages = 0; viewing.original = false;
  $('viewername').textContent = file.name;
  $('viewerinfo').textContent = '불러오는 중';
  $('viewerdownload').href = fileUrl(file);
  $('viewerdownload').setAttribute('download', file.name);
  for (const id of ['viewercompare', 'viewerchanges', 'viewerside', 'viewerpager']) $(id).hidden = true;
  box.replaceChildren(el('p', {class: 'notice'}, el('span', {class: 'spinner'})));
  if (!dialog.open) dialog.showModal();
  let info;
  try {
    info = await (await api(fileUrl(file, '/info'))).json();
  } catch { info = {error: '파일 정보를 받지 못했습니다.'}; }
  if (viewing.file !== file) return;                        // 그사이 다른 파일을 열었다
  if (info.error || info.preview_error || !info.pages) {
    $('viewerinfo').textContent = KIND[file.kind] || file.kind;
    box.replaceChildren(el('p', {class: 'notice', text: (info.error || info.preview_error || '쪽 모양을 그리지 못했습니다.') + ' 내려받아 확인해 주세요.'}));
    return;
  }
  viewing.file = Object.assign(file, info);
  viewing.pages = Math.min(info.pages, 60);
  $('viewerinfo').textContent = [KIND[info.kind] || info.kind, `${info.pages}쪽`, size(info.size)].join(' · ') + (info.pages > 60 ? ' · 앞 60쪽만 표시' : '');
  $('viewerpager').hidden = false;
  if (info.root) {
    $('viewercompare').hidden = false;
    $('viewercompare').setAttribute('aria-pressed', 'false');
    $('viewercompare').lastElementChild.textContent = '원본 보기';
  }
  if ((info.changes || []).length) {
    const side = $('viewerside');
    side.replaceChildren(el('h3', {text: `바뀐 내용 ${info.changes.length}건`}), ...info.changes.map(changeRow));
    side.hidden = matchMedia('(max-width: 820px)').matches;
    $('viewerchanges').hidden = false;
    $('viewerchanges').setAttribute('aria-pressed', String(!side.hidden));
  }
  box.scrollTop = 0;
  fillPages();
  box.focus();
}

function bindViewer() {
  const dialog = $('viewer'), box = $('viewerpages');
  $('viewerclose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { if (viewing.observer) viewing.observer.disconnect(); box.replaceChildren(); viewing.file = null; });
  $('viewerprev').addEventListener('click', () => turnPage(-1));
  $('viewernext').addEventListener('click', () => turnPage(1));
  $('viewercompare').addEventListener('click', () => {
    viewing.original = !viewing.original;
    $('viewercompare').setAttribute('aria-pressed', String(viewing.original));
    $('viewercompare').lastElementChild.textContent = viewing.original ? '원본 보는 중' : '원본 보기';
    fillPages();
  });
  $('viewerchanges').addEventListener('click', () => {
    $('viewerside').hidden = !$('viewerside').hidden;
    $('viewerchanges').setAttribute('aria-pressed', String(!$('viewerside').hidden));
  });
  let ticking = false;
  box.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; if (viewing.pages) $('viewerpage').textContent = `${currentPage()} / ${viewing.pages}`; });
  }, {passive: true});
  dialog.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowRight' || event.key === 'PageDown') { event.preventDefault(); turnPage(1); }
    else if (event.key === 'ArrowLeft' || event.key === 'PageUp') { event.preventDefault(); turnPage(-1); }
    else if (event.key.toLowerCase() === 'o' && !$('viewercompare').hidden) $('viewercompare').click();
  });
}

// ── 사이드바·메뉴·테마 ──────────────────────────────────────────────────
const mobile = () => matchMedia('(max-width: 820px)').matches;

function setSidebar(open) {
  if (mobile()) document.body.classList.toggle('side-open', open);
  else {
    if (open) delete root.dataset.sidebar; else root.dataset.sidebar = 'closed';
    try { localStorage.setItem('sidebar', open ? 'open' : 'closed'); } catch { /* 저장 못 해도 동작은 한다 */ }
  }
}

function closeSidebarOnMobile() { document.body.classList.remove('side-open'); }

function bindMenu(button, menu) {
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    const open = menu.hidden;
    for (const other of document.querySelectorAll('.menu')) other.hidden = true;
    for (const other of document.querySelectorAll('[aria-expanded]')) other.setAttribute('aria-expanded', 'false');
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  });
  menu.addEventListener('click', (event) => event.stopPropagation());
}

function applyTheme(theme) {
  if (theme === 'light' || theme === 'dark') root.dataset.theme = theme; else delete root.dataset.theme;
  try { if (theme === 'system') localStorage.removeItem('theme'); else localStorage.setItem('theme', theme); } catch { /* 무시 */ }
  $('themeicon').setAttribute('href', {light: '#i-sun', dark: '#i-moon', system: '#i-monitor'}[theme]);
  $('themelabel').textContent = '화면: ' + {light: '밝게', dark: '어둡게', system: '시스템 설정'}[theme];
  $('theme').dataset.theme = theme;
}

// ── 시작 ────────────────────────────────────────────────────────────────
$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.busy) { if (state.controller) state.controller.abort(); return; }
  const input = $('input'), question = input.value;
  if (!question.trim() || question.trim().length > state.limit) return;
  input.value = '';
  input.style.height = '';
  updateSend();
  ask(question);
});
$('input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!state.busy) $('composer').requestSubmit(); }
});
$('input').addEventListener('input', (event) => {
  event.target.style.height = '';
  event.target.style.height = Math.min(event.target.scrollHeight, 220) + 'px';
  updateSend();
});
$('attach').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', (event) => { upload(event.target.files[0]); event.target.value = ''; });

// 파일을 화면 어디에 끌어다 놓아도 첨부된다
let dragDepth = 0;
const hasFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes('Files');
addEventListener('dragenter', (event) => { if (hasFiles(event) && !$('viewer').open) { dragDepth++; $('drop').hidden = false; } });
addEventListener('dragleave', (event) => { if (hasFiles(event) && --dragDepth <= 0) { dragDepth = 0; $('drop').hidden = true; } });
addEventListener('dragover', (event) => { if (hasFiles(event)) event.preventDefault(); });
addEventListener('drop', (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  $('drop').hidden = true;
  if (!$('viewer').open) upload(event.dataTransfer.files[0]);
});

$('new').addEventListener('click', newConversation);
$('newtop').addEventListener('click', newConversation);
$('collapse').addEventListener('click', () => setSidebar(false));
$('expand').addEventListener('click', () => setSidebar(true));
$('backdrop').addEventListener('click', closeSidebarOnMobile);
$('tobottom').addEventListener('click', () => scrollDown(true));
$('scroll').addEventListener('scroll', () => { $('tobottom').hidden = nearBottom(); });
for (const button of document.querySelectorAll('.suggestions button')) {
  button.addEventListener('click', () => { if (button.dataset.action === 'attach') $('file').click(); else ask(button.dataset.q); });
  if (button.dataset.action === 'attach') {              // 양식 알약에 올리면 입력창의 첨부 단추가 움직여, 같은 일을 하는 곳을 알려 준다
    for (const [name, on] of [['pointerenter', true], ['focus', true], ['pointerleave', false], ['blur', false]]) button.addEventListener(name, () => $('attach').classList.toggle('nudge', on));
  }
}
bindMenu($('user'), $('usermenu'));
$('status').addEventListener('click', () => setView(state.view === 'tools' ? null : 'tools'));
$('nav-desk').addEventListener('click', () => openDesk().catch(() => {}));
$('tab-talk').addEventListener('click', () => { setView(null); watchDesk().catch(() => {}); });
for (const view of ['today', 'tasks', 'notes']) $('tab-' + view).addEventListener('click', () => setView(view));      // (DESK_VIEWS 는 아래에서 정해진다)
for (const button of document.querySelectorAll('.side-nav [data-view]')) button.addEventListener('click', () => setView(state.view === button.dataset.view ? null : button.dataset.view));
bindCitations();
bindViewer();
document.addEventListener('click', () => {
  for (const menu of document.querySelectorAll('.menu')) menu.hidden = true;
  for (const button of document.querySelectorAll('[aria-expanded]')) button.setAttribute('aria-expanded', 'false');
});
$('theme').addEventListener('click', () => applyTheme({system: 'light', light: 'dark', dark: 'system'}[$('theme').dataset.theme]));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeSidebarOnMobile(); hideCard(true);
    for (const menu of document.querySelectorAll('.menu')) menu.hidden = true;
    if (state.view && !$('viewer').open && !document.querySelector('.topic-form')) setView(null);
  }
  if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === 'k') { event.preventDefault(); setView('search'); }
  if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') { event.preventDefault(); newConversation(); }
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
  if (event.key === '/' && !typing && !$('viewer').open && !event.ctrlKey && !event.metaKey) { event.preventDefault(); $('input').focus(); }
});
applyTheme(root.dataset.theme || 'system');

async function loadStatus() {
  const me = await (await api('/api/me')).json();
  state.name = me.name;
  state.limit = me.max_question_chars;
  $('who').textContent = me.name;
  $('initial').textContent = me.name.slice(0, 1).toUpperCase();
  const down = me.servers.filter((server) => !server.ok).length;
  $('status').className = 'chip ' + (!me.servers.length ? 'wait' : down ? 'warn' : 'ok');
  $('statuslabel').textContent = !me.servers.length ? `${me.model} · 확인 중` : `${me.model} · 자료 ${me.servers.length - down}/${me.servers.length}`;
  state.admin = Boolean(me.admin);                           // 가입 신청을 검토하는 계정에게만 계정 메뉴가 보인다
  $('nav-accounts').hidden = !me.admin;
  $('accountmark').hidden = !me.signups;
  return me.servers.length;
}

// ── 메뉴에서 여는 화면 ──────────────────────────────────────────────────
// 대화 자리에 겹쳐 보인다. 같은 메뉴를 다시 누르거나 Esc 를 누르면 보던 대화로 돌아간다(답을 받는 중이어도 끊기지 않는다).
const VIEWS = {search: {title: '검색', open: openSearch}, today: {title: '비서', open: openDeskView}, tasks: {title: '비서', open: openDeskView}, notes: {title: '비서', open: openDeskView}, docs: {title: '문서', open: openDocs},
               tools: {title: '도구 상태', open: openTools}, accounts: {title: '계정', open: openAccounts}};

function setView(name) {
  state.view = name || null;
  document.body.classList.toggle('paged', Boolean(name));
  $('page').replaceChildren();
  setTitle();
  deskChrome();
  renderList();
  closeSidebarOnMobile();
  if (name) VIEWS[name].open($('page')); else if (!state.busy) $('input').focus();
}

function when(seconds) {                                   // 오늘이면 시각, 올해면 날짜, 그 전이면 연도까지
  const moment = new Date(seconds * 1000), now = new Date();
  if (moment.toDateString() === now.toDateString()) return `${String(moment.getHours()).padStart(2, '0')}:${String(moment.getMinutes()).padStart(2, '0')}`;
  const day = `${moment.getMonth() + 1}월 ${moment.getDate()}일`;
  return moment.getFullYear() === now.getFullYear() ? day : `${moment.getFullYear()}년 ${day}`;
}

function marked(parent, text, words) {                     // 찾은 낱말에 형광펜
  const low = text.toLowerCase();
  let at = 0;
  while (at < text.length) {
    let next = -1, size = 0;
    for (const word of words) {
      const found = low.indexOf(word, at);
      if (found >= 0 && (next < 0 || found < next)) { next = found; size = word.length; }
    }
    if (next < 0) break;
    if (next > at) parent.append(text.slice(at, next));
    parent.append(el('mark', {text: text.slice(next, next + size)}));
    at = next + size;
  }
  if (at < text.length) parent.append(text.slice(at));
  return parent;
}

function openSearch(page) {
  const input = el('input', {type: 'search', placeholder: '대화 검색', 'aria-label': '대화 검색', autocomplete: 'off', maxlength: '200'});
  const box = el('div', {class: 'results'});
  let ticket = 0, timer = 0;
  const show = async () => {
    const text = input.value.trim(), words = text.toLowerCase().split(/\s+/).filter(Boolean), mine = ++ticket;
    let items = state.conversations;                         // 찾는 말이 없으면 모든 대화를 최근 것부터
    if (text) {
      const response = await api('/api/search?q=' + encodeURIComponent(text));
      if (mine !== ticket || !response.ok) return;
      items = (await response.json()).results;
    }
    box.replaceChildren(...items.map((item) => {
      const row = el('button', {type: 'button', class: 'result' + (item.kind === 'brief' ? ' brief' : '')},
                     el('div', {class: 'result-top'}, marked(el('strong'), item.title || '대화', words), el('span', {class: 'result-side', text: when(item.updated)})));
      if (item.snippet) row.append(marked(el('p'), item.snippet, words));
      row.addEventListener('click', () => openConversation(item.id));
      return row;
    }));
    if (!items.length) box.append(el('p', {class: 'none', text: text ? '찾는 대화가 없습니다.' : '아직 대화가 없습니다.'}));
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(show, 140); });
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter' && box.firstElementChild && box.firstElementChild.click) box.firstElementChild.click(); });
  page.append(el('div', {class: 'sheet'}, el('label', {class: 'page-search'}, icon('search'), input), box));
  show();
  input.focus();
}

// ── 비서: 말로 일을 맡기는 대화 하나와 그 둘레의 화면들 ──
// 오늘: 그날 올라온 결과의 카드와 권하는 일 · 대화: 일을 맡기고 결과를 받는 곳 · 맡긴 일: 목록 · 메모: 비서가 적어 둔 것
let deskTimer = 0;
const DESK_VIEWS = ['today', 'tasks', 'notes'];
const onDesk = () => state.kind === 'assistant' && (!state.view || DESK_VIEWS.includes(state.view));

function deskSeen(stamp) {                                 // 마지막으로 본 결과의 시각(새 결과가 오면 메뉴에 점이 뜬다)
  try { if (stamp) localStorage.setItem('deskSeen', String(stamp)); return Number(localStorage.getItem('deskSeen') || 0); } catch { return Number(stamp || 0); }
}

function deskChrome() {                                    // 비서에서만 보이는 것들: 탭, 입력창 안내, 예시
  const desk = onDesk();
  document.body.classList.toggle('desk', desk);
  $('desktabs').hidden = !desk;
  for (const [id, view] of [['tab-today', 'today'], ['tab-talk', null], ['tab-tasks', 'tasks'], ['tab-notes', 'notes']]) $(id).setAttribute('aria-pressed', String(state.view === view));
  $('taskcount').textContent = state.desk && state.desk.tasks.length ? ` ${state.desk.tasks.length}` : '';
  $('input').placeholder = state.kind === 'assistant' ? '비서에게 맡길 일을 말해 보세요' : '무엇이든 물어보세요';
}

const typing = () => { const active = document.activeElement; return Boolean(active && active.closest && active.closest('#page') && /INPUT|TEXTAREA/.test(active.tagName)); };

function paintDesk() {                                     // 지금 보고 있는 비서 화면을 다시 그린다
  if (state.view === 'today') paintToday(); else if (state.view === 'tasks') paintTasks(); else if (state.view === 'notes' && !typing()) paintNotes();
}

async function watchDesk() {                                // 일을 하는 중이면 5초마다, 아니면 1분마다 상태를 본다
  clearTimeout(deskTimer);
  const info = await (await api('/api/assistant')).json();
  state.desk = info;
  $('nav-desk').hidden = !info.available;
  const reading = state.kind === 'assistant' && !state.view;
  if (state.kind === 'assistant' && state.deskShown !== undefined && info.delivered !== state.deskShown && !state.busy) {      // 화면에 그린 뒤에 새 결과가 올라왔다
    const view = state.view;
    await openConversation(info.conversation_id);
    if (view && DESK_VIEWS.includes(view)) setView(view);
  }
  const fresh = info.delivered > deskSeen(onDesk() ? info.delivered : 0);
  $('deskmark').hidden = !(info.running || fresh);
  $('deskmark').classList.toggle('busy', info.running);
  deskChrome();
  if (!reading) paintDesk();
  deskTimer = setTimeout(() => watchDesk().catch(() => {}), info.running ? 5000 : 60000);
}

async function openDesk() {
  await watchDesk();
  if (!state.desk.available) return;
  if (state.kind === 'assistant' && state.conversation === state.desk.conversation_id) return setView(null);
  await openConversation(state.desk.conversation_id);
  const today = state.desk.today;                           // 오늘 올라온 것이나 권할 것이 있으면 그것부터 보인다
  if (state.kind === 'assistant' && today && ((today.day === today.today && today.cards.length) || today.suggestions.length || today.errors.length)) setView('today');
}

async function deskAction(body) {
  const response = await api('/api/assistant', {method: 'POST', headers: API_HEADERS, body: JSON.stringify(body)});
  const reply = await response.json().catch(() => ({}));
  if (!response.ok) { alert(reply.error || '처리하지 못했습니다.'); return false; }
  state.desk = reply;
  deskChrome();
  if (state.view === 'notes') paintNotes(); else paintDesk();
  if (body.action === 'run') watchDesk().catch(() => {});
  return true;
}
const taskAction = (id, action) => deskAction({id, action});

function deskSheet(view) {                                  // 그 화면이 열려 있을 때만 그릴 자리를 준다
  const sheet = $('page').querySelector('.sheet');
  return sheet && state.view === view && state.desk ? sheet : null;
}

function openDeskView(page) {
  page.append(el('div', {class: 'sheet'}, el('p', {class: 'waiting'}, el('span', {class: 'spinner'}))));
  if (state.desk) paintDesk();
  watchDesk().catch(() => {});
}

// 오늘
const dayText = (day) => { const found = /^\d{4}-(\d{2})-(\d{2})/.exec(day || ''); return found ? `${Number(found[1])}월 ${Number(found[2])}일` : ''; };

function showResult(index) {                                // 카드에서 대화의 그 결과로 간다
  setView(null);
  const mark = $('thread').querySelector(`.auto[data-at="${Number(index)}"]`);
  if (mark) mark.scrollIntoView({block: 'start'});
}

function paintToday() {
  const sheet = deskSheet('today');
  if (!sheet) return;
  const info = state.desk.today, parts = [];
  for (const title of info.running) parts.push(el('div', {class: 'line'}, el('span', {class: 'spinner'}), el('span', {class: 'name', text: title}), el('span', {class: 'note', text: '하는 중'})));
  for (const failed of info.errors) {
    const again = el('button', {type: 'button', class: 'pill keep'}, icon('refresh'), el('span', {text: '다시 하기'}));
    again.addEventListener('click', () => { again.disabled = true; taskAction(failed.task, 'run'); });
    parts.push(el('article', {class: 'pulse bad'}, el('div', {class: 'pulse-top'}, el('strong', {text: failed.title}), el('span', {text: '실패'})),
                  el('p', {text: failed.note || '결과를 만들지 못했습니다.'}), el('div', {class: 'pulse-foot'}, again)));
  }
  if (info.cards.length) parts.push(el('h2', null, info.day === info.today ? '오늘' : dayText(info.day), el('span', {text: info.day === info.today ? dayText(info.day) : ''})));
  for (const card of info.cards) {
    const open = el('button', {type: 'button', class: 'pulse-open'}, el('div', {class: 'pulse-top'}, el('strong', {text: card.title})), el('p', {text: card.summary || '결과가 올라왔습니다.'}));
    open.addEventListener('click', () => showResult(card.index));
    const foot = el('div', {class: 'pulse-foot'});
    if (card.kept) {                                        // 반응은 그 일이 남아 있을 때만 받는다(다음에 할 때 반영한다)
      for (const [value, label] of [['up', '좋아요'], ['down', '별로예요']]) {
        const button = iconButton('thumb', label, () => deskAction({id: card.task, action: value, day: card.day}));
        button.classList.add(value);
        button.setAttribute('aria-pressed', String(card.feedback === value));
        foot.append(button);
      }
    }
    parts.push(el('article', {class: 'pulse'}, open, foot));
  }
  if (info.quiet.length) parts.push(el('p', {class: 'quiet', text: `새 소식 없음 · ${info.quiet.join(', ')}`}));
  if (info.suggestions.length) {
    parts.push(el('h2', null, '이것도 챙겨 드릴까요'));
    for (const item of info.suggestions) {
      const pill = (label, solid, action) => {
        const button = el('button', {type: 'button', class: 'pill keep' + (solid ? ' solid' : '')}, el('span', {text: label}));
        button.addEventListener('click', () => { button.disabled = true; deskAction({id: item.id, action}); });
        return button;
      };
      parts.push(el('div', {class: 'topic'}, el('div', {class: 'topic-main'}, el('strong', {text: item.topic}), el('span', {text: [item.kind, item.conditions].filter(Boolean).join(' · ')})),
                    el('span', {class: 'account-actions'}, pill('맡기기', true, 'suggest_accept'), pill('괜찮아요', false, 'suggest_dismiss'))));
    }
  }
  if (info.upcoming.length) {
    parts.push(el('h2', null, '오늘 예정'));
    for (const next of info.upcoming) parts.push(el('div', {class: 'line plain'}, el('span', {class: 'due', text: next.time}), el('span', {class: 'name', text: next.title})));
  }
  if (!parts.length) parts.push(el('p', {class: 'none', text: state.desk.tasks.length ? '오늘 올라온 것이 없습니다.' : '맡긴 일이 없습니다.'}));
  sheet.replaceChildren(...parts);
}

// 맡긴 일
function lastText(task) {
  if (task.running || (task.last && task.last.status === 'running')) return '하는 중';
  if (!task.last) return '아직 한 적 없음';
  const at = when(task.last.at);
  return {done: `${at} 올림`, quiet: `${at} 새 소식 없음`, error: `${at} 실패` + (task.last.note ? ` · ${task.last.note}` : '')}[task.last.status] || '';
}

function paintTasks() {
  const sheet = deskSheet('tasks'), info = state.desk;
  if (!sheet) return;
  const rows = info.tasks.map((task) => {
    const toggle = el('button', {type: 'button', class: 'switch', role: 'switch', 'aria-checked': String(!task.paused), 'aria-label': `${task.title} 켜기`});
    toggle.addEventListener('click', () => taskAction(task.id, task.paused ? 'resume' : 'pause'));
    const detail = [task.when, task.notify === 'new' ? '새 것이 있을 때만' : '할 때마다', task.mail ? '메일' : ''].filter(Boolean).join(' · ');
    const actions = el('span', {class: 'row-actions'}, iconButton('refresh', '지금 하기', () => taskAction(task.id, 'run')));
    if (info.mail) {
      const mail = iconButton('mail', task.mail ? '메일로 받지 않기' : '메일로도 받기', () => taskAction(task.id, task.mail ? 'mail_off' : 'mail_on'));
      mail.setAttribute('aria-pressed', String(Boolean(task.mail)));
      actions.append(mail);
    }
    actions.append(iconButton('trash', '맡긴 일 지우기', () => { if (confirm(`"${task.title}" 일을 지울까요?`)) taskAction(task.id, 'delete'); }));
    const last = el('span', {class: 'last' + (task.last && task.last.status === 'error' && !task.running ? ' bad' : ''), text: lastText(task)});
    return el('div', {class: 'topic task' + (task.paused ? ' off' : '')}, toggle,
              el('div', {class: 'topic-main'}, el('strong', {text: task.title}), el('span', {text: detail}), el('p', {class: 'task-what', text: task.what, title: task.what}), last), actions);
  });
  const add = el('button', {type: 'button', class: 'add-topic'}, icon('plus'), el('span', {text: '말로 맡기기'}));
  add.addEventListener('click', () => { setView(null); $('input').focus(); });
  sheet.replaceChildren(el('h2', null, '맡긴 일', el('span', {text: `${info.tasks.length} / ${info.max_tasks}`})), ...rows,
                        ...(info.tasks.length ? [] : [el('p', {class: 'none', text: '맡긴 일이 없습니다.'})]), add);
}

// 메모: 비서가 적어 둔 것. 보고, 고치고, 지우고, 직접 적을 수 있다
function noteForm(note) {                                   // note 가 없으면 새 메모
  const input = el('input', {maxlength: '200', placeholder: '메모 추가', 'aria-label': note ? '메모 고치기' : '메모 추가', autocomplete: 'off'});
  input.value = note ? note.text : '';
  const form = el('form', {class: 'note-form' + (note ? ' editing' : '')}, icon(note ? 'edit' : 'plus'), input);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return note ? paintNotes() : input.focus();
    input.blur();
    if (!await deskAction({id: note ? note.id : '', action: 'note_save', text})) input.focus();
  });
  if (note) input.addEventListener('keydown', (event) => { if (event.key === 'Escape') { event.stopPropagation(); input.blur(); paintNotes(); } });
  return form;
}

function paintNotes() {
  const sheet = deskSheet('notes'), info = state.desk;
  if (!sheet) return;
  const rows = info.notes.map((note) => {
    const row = el('div', {class: 'topic memo'}, el('div', {class: 'topic-main'}, el('strong', {text: note.text}), el('span', {text: `${note.by === 'user' ? '직접 적음' : '비서가 적음'} · ${when(note.at)}`})),
                   el('span', {class: 'row-actions'},
                      iconButton('edit', '메모 고치기', () => { const form = noteForm(note); row.replaceWith(form); form.querySelector('input').focus(); }),
                      iconButton('trash', '메모 지우기', () => deskAction({id: note.id, action: 'note_delete'}))));
    return row;
  });
  sheet.replaceChildren(el('h2', null, '메모', el('span', {text: `${info.notes.length} / ${info.max_notes}`})), ...rows, noteForm(null));
}

// ── 문서: 올린 문서와 모델이 만들거나 고친 문서를 대화와 상관없이 모아 본다 ──
const ORIGIN = {upload: '올린 문서', generated: '만든 문서', edited: '고친 문서'};

async function openDocs(page) {
  const sheet = el('div', {class: 'sheet'}, el('p', {class: 'waiting'}, el('span', {class: 'spinner'})));
  page.append(sheet);
  let info;
  try {
    info = await (await api('/api/files')).json();
  } catch { info = null; }
  if (state.view !== 'docs') return;
  if (!info || !info.files) return sheet.replaceChildren(el('p', {class: 'none', text: '문서 목록을 받지 못했습니다.'}));
  const input = el('input', {type: 'search', placeholder: '문서 검색', 'aria-label': '문서 검색', autocomplete: 'off', maxlength: '200'});
  const box = el('div', {class: 'results'}), count = el('span', {class: 'count'});
  let only = '';
  const tabs = [['', '전체'], ['upload', '올린 문서'], ['made', '만든 문서']].map(([key, label]) => {
    const tab = el('button', {type: 'button', class: 'pill keep', 'aria-pressed': String(key === only)}, el('span', {text: label}));
    tab.addEventListener('click', () => { only = key; for (const other of tabs) other.setAttribute('aria-pressed', String(other === tab)); show(); });
    return tab;
  });
  const row = (file, words) => {
    const detail = [ORIGIN[file.origin] || '', KIND[file.kind] || file.kind, file.pages ? `${file.pages}쪽` : '', size(file.size)].filter(Boolean).join(' · ');
    const open = el('button', {type: 'button', class: 'doc-open', title: '미리보기'},
                    el('span', {class: 'doc-kind ' + file.kind, text: file.kind.toUpperCase()}),
                    el('span', {class: 'doc-main'}, marked(el('strong'), file.name, words), el('span', {text: detail})));
    open.addEventListener('click', () => openViewer(file));
    const actions = el('span', {class: 'row-actions'});
    if (file.conversation) {
      actions.append(iconButton('out', '대화로 가기', async () => {
        await openConversation(file.conversation.id);
        const made = [...document.querySelectorAll('#thread .filecard')].find((node) => node.dataset.id === file.id);
        if (made) made.scrollIntoView({block: 'center'});
      }));
    }
    actions.append(el('a', {class: 'icon', href: fileUrl(file), download: file.name, title: '내려받기', 'aria-label': '내려받기'}, icon('download')),
                   iconButton('trash', '문서 지우기', async () => {
                     if (!confirm(`"${file.name}" 문서를 지울까요?`)) return;
                     const response = await api(fileUrl(file), {method: 'DELETE', headers: API_HEADERS});
                     if (!response.ok) return;
                     info.files = info.files.filter((other) => other !== file);
                     for (const made of document.querySelectorAll('#thread .filecard')) if (made.dataset.id === file.id) made.remove();
                     show();
                   }));
    return el('div', {class: 'doc'}, open, el('span', {class: 'result-side', text: when(file.created)}), actions);
  };
  const show = () => {
    const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const kept = info.files.filter((file) => (!only || (only === 'upload') === (file.origin === 'upload')) && words.every((word) => file.name.toLowerCase().includes(word)));
    box.replaceChildren(...kept.map((file) => row(file, words)));
    if (!kept.length) box.append(el('p', {class: 'none', text: info.files.length ? '찾는 문서가 없습니다.' : '아직 문서가 없습니다.'}));
    count.textContent = info.files.length ? `${info.files.length}개 · ${size(info.files.reduce((sum, file) => sum + (file.size || 0), 0))}` : '';
  };
  input.addEventListener('input', show);
  sheet.replaceChildren(el('label', {class: 'page-search'}, icon('search'), input), el('div', {class: 'doc-bar'}, ...tabs, count), box);
  show();
  if (!mobile()) input.focus();
}

function stamp(text) {                                     // "2026-11-21T09:00:02" → "11월 21일 09:00"
  const found = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(text || '');
  return found ? `${Number(found[2])}월 ${Number(found[3])}일 ${found[4]}:${found[5]}` : '';
}

function took(from, to) {
  const seconds = Math.round((new Date(to) - new Date(from)) / 1000);
  if (!(seconds >= 0)) return '';
  return seconds >= 60 ? `${Math.floor(seconds / 60)}분 ${seconds % 60}초` : `${seconds}초`;
}

function toolGroup(name, tools, ok, note) {
  const head = el('summary', {class: 'line ' + (ok ? 'ok' : 'down')}, el('span', {class: 'dot'}), el('span', {class: 'name', text: name}),
                  el('span', {class: 'note', text: note}), icon('right'));
  head.lastElementChild.classList.add('caret');
  if (!tools.length) return el('div', {class: 'line ' + (ok ? 'ok' : 'down')}, el('span', {class: 'dot'}), el('span', {class: 'name', text: name}), el('span', {class: 'note', text: note}));
  return el('details', {class: 'server'}, head, el('div', {class: 'toolchips'}, ...tools.map((tool) => el('span', {text: TOOLS[tool] || tool}))));
}

async function openTools(page) {
  const sheet = el('div', {class: 'sheet'}, el('p', {class: 'waiting'}, el('span', {class: 'spinner'})));
  page.append(sheet);
  let info;
  try {
    info = await (await api('/api/status')).json();
  } catch { info = null; }
  if (state.view !== 'tools') return;
  if (!info) return sheet.replaceChildren(el('p', {class: 'none', text: '상태를 받지 못했습니다.'}));
  const model = info.model.ok === false ? 'down' : info.model.ok ? 'ok' : '';
  sheet.replaceChildren(
    el('h2', null, '모델'),
    el('div', {class: 'line ' + model}, el('span', {class: 'dot'}), el('span', {class: 'name', text: info.model.name}),
       el('span', {class: 'note', text: info.model.ok === false ? '마지막 요청 실패' : ''})),
    el('h2', null, '자료 서버'),
    ...info.servers.map((server) => toolGroup(SERVERS[server.name] || server.name, server.tools, server.ok, server.ok ? `도구 ${server.tools.length}` : '연결 안 됨')),
    ...(info.local.length ? [toolGroup('문서 작업 · 전체 확인', info.local, true, `도구 ${info.local.length}`)] : []));
  if (info.refresh) {
    const done = info.refresh.finished ? took(info.refresh.started, info.refresh.finished) : '진행 중';
    sheet.append(el('h2', null, '자료 갱신', el('span', {text: [stamp(info.refresh.started), done].filter(Boolean).join(' · ')})));
    for (const step of info.refresh.steps) {
      sheet.append(el('div', {class: 'line ' + (step.ok ? 'ok' : 'down')}, el('span', {class: 'dot'}), el('span', {class: 'name', text: step.name}),
                      el('span', {class: 'note', text: step.note || (step.ok ? '' : '실패')})));
    }
  }
  loadStatus().catch(() => {});                              // 방금 다시 확인한 연결 상태를 위쪽 표시에도 반영한다
}

// ── 계정: 가입 신청을 검토해 등록하거나 거절한다(검토하는 계정에게만 보인다) ──
async function openAccounts(page) {
  const sheet = el('div', {class: 'sheet'}, el('p', {class: 'waiting'}, el('span', {class: 'spinner'})));
  page.append(sheet);
  const paint = (info) => {
    if (state.view !== 'accounts') return;
    if (!info || !info.pending) return sheet.replaceChildren(el('p', {class: 'none', text: '계정 목록을 받지 못했습니다.'}));
    $('accountmark').hidden = !info.pending.length;
    const decide = async (name, action) => {
      const response = await api('/api/accounts', {method: 'POST', headers: API_HEADERS, body: JSON.stringify({name, action})});
      const reply = await response.json().catch(() => ({}));
      if (!response.ok) { alert(reply.error || '처리하지 못했습니다.'); reply.pending ? paint(reply) : load(); return; }
      paint(reply);
    };
    const row = (account, ...actions) => el('div', {class: 'topic'},
      el('div', {class: 'topic-main'}, el('strong', {text: account.name}), el('span', {text: account.email})),
      el('span', {class: 'result-side', text: when(account.decided || account.created)}), el('span', {class: 'account-actions'}, ...actions));
    const pill = (label, solid, handler) => {
      const button = el('button', {type: 'button', class: 'pill keep' + (solid ? ' solid' : '')}, el('span', {text: label}));
      button.addEventListener('click', () => { button.disabled = true; handler(); });
      return button;
    };
    sheet.replaceChildren(...[
      el('h2', null, '가입 신청', el('span', {text: info.pending.length ? `${info.pending.length}건` : ''})),
      ...info.pending.map((account) => row(account, pill('승인', true, () => decide(account.name, 'approve')), pill('거절', false, () => decide(account.name, 'reject')))),
      info.pending.length ? null : el('p', {class: 'none', text: '기다리는 신청이 없습니다.'}),
      info.active.length ? el('h2', null, '등록된 계정', el('span', {text: `${info.active.length}개`})) : null,
      ...info.active.map((account) => row(account, iconButton('trash', '계정 지우기', () => {
        if (confirm(`"${account.name}" 계정을 지울까요? 이 계정의 대화와 문서도 모두 지워집니다.`)) decide(account.name, 'remove');
      })))].filter(Boolean));                                 // 빈 자리(null)를 그대로 넣으면 글자로 찍힌다
  };
  const load = async () => {
    let info;
    try { info = await (await api('/api/accounts')).json(); } catch { info = null; }
    paint(info);
  };
  load();
}

(async () => {
  try {
    watchDesk().catch(() => {});
    if (!await loadStatus()) setTimeout(() => loadStatus().catch(() => {}), 2500);     // 서버가 막 떴으면 자료 서버 확인이 조금 늦다
    if (state.admin) {
      setInterval(() => loadStatus().catch(() => {}), 300000);                            // 새 가입 신청이 왔는지 가끔 본다
      if (new URLSearchParams(location.search).get('view') === 'accounts') { history.replaceState(null, '', '/'); setView('accounts'); }      // 알림 메일의 링크
    }
    await loadList();
    updateSend();
    $('input').focus();
  } catch { /* 로그인 화면으로 넘어가는 중 */ }
})();
