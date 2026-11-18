// 채팅 화면. 모델의 답과 도구 결과는 신뢰할 수 없는 글이므로 innerHTML 을 쓰지 않고 DOM 노드로만 그린다.
'use strict';

const API_HEADERS = {'Content-Type': 'application/json', 'X-Requested-With': 'rnd-chat'};
const SVG = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);
const root = document.documentElement;
const state = {conversation: null, conversations: [], busy: false, controller: null, limit: 4000, name: ''};
const TOOLS = {
  search: '공고 검색', fetch: '공고 본문 읽기', search_notices: '진행 중 공고 검색', get_notice: '공고 상세 확인', read_document: '첨부 문서 읽기',
  open_link: '링크 열기', view_link: '링크 화면 보기', search_history: '공고 이력 검색', count_history: '공고 이력 집계',
  get_history_notice: '이력 공고 상세 확인', read_history_document: '이력 첨부 읽기', view_history_document: '이력 첨부 보기',
  history_status: '이력 자료 범위 확인', search_projects: 'NTIS 과제 검색', count_projects: 'NTIS 과제 집계', get_project: 'NTIS 과제 상세 확인',
  search_results: 'NTIS 성과 검색', count_results: 'NTIS 성과 집계', search_reports: 'NTIS 연구보고서 검색', lookup_term: 'NTIS 용어사전 조회',
  lookup_class_code: 'NTIS 분류코드 조회', ntis_status: 'NTIS 연결 확인',
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

function inline(parent, text) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    const token = match[0];
    if (match[1]) parent.append(el('code', {text: token.slice(1, -1)}));
    else if (match[2]) parent.append(inline(el('strong'), token.slice(2, -2)));
    else if (match[3]) { const cut = token.indexOf(']('); parent.append(link(token.slice(1, cut), token.slice(cut + 2, -1))); }
    else parent.append(link(token, token));
    last = match.index + token.length;
  }
  if (last < text.length) parent.append(text.slice(last));
  return parent;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
const isRule = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
const LIST = /^\s*([-*•]|\d+[.)])\s+/;

function markdown(text) {
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
        for (const cell of cells(lines[i])) row.append(inline(el('td'), cell));
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
  const list = $('list'), words = $('filter').value.trim().toLowerCase();
  list.replaceChildren();
  let group = null, shown = 0;
  for (const item of state.conversations) {
    if (words && !item.title.toLowerCase().includes(words)) continue;
    const name = groupOf(item.updated);
    if (name !== group) { list.append(el('p', {class: 'group', text: name})); group = name; }
    list.append(conversationRow(item));
    shown++;
  }
  if (!shown) list.append(el('p', {class: 'none', text: words ? '찾는 대화가 없습니다.' : '아직 대화가 없습니다.'}));
}

function conversationRow(item) {
  const open = el('button', {type: 'button', class: 'open', title: item.title, text: item.title || '대화'});
  open.addEventListener('click', () => openConversation(item.id));
  const row = el('div', {class: 'item' + (item.id === state.conversation ? ' current' : '')}, open);
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
function setTitle(title) {
  $('title').textContent = title || '새 대화';
  document.title = title ? `${title} · R&D 검색` : 'R&D 검색';
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
}

function addUser(text) {
  document.body.classList.remove('empty');
  const node = el('article', {class: 'msg user'}, el('div', {class: 'bubble', text}));
  $('thread').append(node);
  return node;
}

function addAssistant(text) {
  document.body.classList.remove('empty');
  const body = el('div', {class: 'body'}), meta = el('span', {class: 'meta-info'}), actions = el('div', {class: 'actions'});
  const node = el('article', {class: 'msg assistant'}, el('span', {class: 'mark', 'aria-hidden': 'true'}, icon('spark')),
                  el('div', {class: 'content'}, el('div', {class: 'meta'}, el('strong', {text: 'R&D 검색'}), meta), body, actions));
  const view = {node, body, meta, actions, text: text || '', steps: null, stepList: null};
  if (text) { body.append(markdown(text)); addActions(view); }
  $('thread').append(node);
  return view;
}

function addActions(view) {
  const copy = iconButton('copy', '답 복사', () => { copyText(view.text); flash(copy, 'copy'); });
  view.actions.replaceChildren(copy);
  for (const old of $('thread').querySelectorAll('.regenerate')) old.remove();     // 다시 생성은 마지막 답에만
  if (state.conversation && view.node === $('thread').lastElementChild) {
    const again = iconButton('refresh', '다시 생성', () => regenerate(view));
    again.classList.add('regenerate');
    view.actions.append(again);
  }
}

function describe(args) {
  return Object.entries(args || {}).filter(([, value]) => value !== '' && value !== 0 && value !== null && value !== false)
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ');
}

function addStep(view, event) {
  if (!view.steps) {
    view.stepList = el('ol');
    view.steps = el('details', {class: 'steps', open: ''}, el('summary', null, el('span', {class: 'spinner'}), el('span', {class: 'steps-label', text: '자료를 찾는 중'})), view.stepList);
    view.body.before(view.steps);
  }
  const detail = describe(event.arguments);
  view.stepList.append(el('li', {class: 'running'}, el('span', {class: 'step-name', text: TOOLS[event.name] || event.name}),
                          detail ? el('span', {class: 'step-args', text: detail}) : null));
  view.steps.querySelector('.steps-label').textContent = `${TOOLS[event.name] || event.name}…`;
}

function finishSteps(view, stopped) {
  if (!view.steps) return;
  const count = view.stepList.childElementCount;
  for (const item of view.stepList.querySelectorAll('.running')) item.className = stopped ? 'stopped' : 'done';
  view.steps.querySelector('.spinner').replaceWith(icon(stopped ? 'alert' : 'check'));
  view.steps.querySelector('.steps-label').textContent = `자료 조회 ${count}회`;
  view.steps.removeAttribute('open');
}

async function openConversation(id) {
  if (state.busy) return;
  const response = await api('/api/conversations/' + encodeURIComponent(id));
  if (!response.ok) return loadList();
  const item = await response.json();
  state.conversation = item.id;
  setTitle(item.title);
  clearThread();
  let last = null;
  for (const message of item.messages) last = message.role === 'user' ? addUser(message.content) : addAssistant(message.content);
  if (last && last.node) addActions(last);
  closeSidebarOnMobile();
  renderList();
  scrollDown(true);
}

function newConversation() {
  if (state.busy) return;
  state.conversation = null;
  setTitle('');
  clearThread();
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
  $('send').disabled = !state.busy && (!length || length > state.limit);
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
  addUser(question);
  await run({message: question, conversation_id: state.conversation});
}

async function run(payload) {
  setBusy(true);
  const view = addAssistant('');
  view.node.classList.add('pending');
  view.body.append(el('p', {class: 'thinking'}, el('span', {class: 'spinner'}), '생각하는 중'));
  scrollDown(true);
  const started = performance.now();
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
        } else if (event.type === 'tool_call') {
          calls++;
          const stick = nearBottom();
          addStep(view, event);
          if (!buffer) view.body.replaceChildren();
          if (stick) scrollDown(true);
        } else if (event.type === 'tool_result') {
          const item = view.stepList && view.stepList.lastElementChild;
          if (item) item.className = event.is_error ? 'error' : 'done';
        } else if (event.type === 'answer') {
          finished = true;
          const stick = nearBottom();
          view.text = event.text || '';
          finishSteps(view, false);
          view.body.replaceChildren(markdown(view.text || '(빈 답)'));
          if (event.stopped === 'max_steps') view.body.append(el('p', {class: 'note', text: '조회 횟수 상한에 걸려 여기까지 찾은 내용으로 답했습니다.'}));
          if (event.stopped === 'length') view.body.append(el('p', {class: 'note', text: '답이 길이 상한에서 끊겼습니다.'}));
          view.node.classList.remove('pending');
          view.meta.textContent = `${((performance.now() - started) / 1000).toFixed(1)}초` + (calls ? ` · 자료 조회 ${calls}회` : '');
          if (stick) scrollDown(true);
        } else if (event.type === 'done') {
          state.conversation = event.conversation_id;
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
    state.controller = null;
    setBusy(false);
    loadList().catch(() => {});
    $('input').focus();
  }
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
  $('themelabel').textContent = '테마: ' + {light: '밝게', dark: '어둡게', system: '시스템 설정'}[theme];
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
$('new').addEventListener('click', newConversation);
$('newtop').addEventListener('click', newConversation);
$('collapse').addEventListener('click', () => setSidebar(false));
$('expand').addEventListener('click', () => setSidebar(true));
$('backdrop').addEventListener('click', closeSidebarOnMobile);
$('filter').addEventListener('input', renderList);
$('tobottom').addEventListener('click', () => scrollDown(true));
$('scroll').addEventListener('scroll', () => { $('tobottom').hidden = nearBottom(); });
for (const button of $('suggestions').querySelectorAll('button')) button.addEventListener('click', () => ask(button.dataset.q));
bindMenu($('user'), $('usermenu'));
bindMenu($('status'), $('statusmenu'));
document.addEventListener('click', () => {
  for (const menu of document.querySelectorAll('.menu')) menu.hidden = true;
  for (const button of document.querySelectorAll('[aria-expanded]')) button.setAttribute('aria-expanded', 'false');
});
$('theme').addEventListener('click', () => applyTheme({system: 'light', light: 'dark', dark: 'system'}[$('theme').dataset.theme]));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { closeSidebarOnMobile(); for (const menu of document.querySelectorAll('.menu')) menu.hidden = true; }
  if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'o') { event.preventDefault(); newConversation(); }
});
applyTheme(root.dataset.theme || 'system');

async function loadStatus() {
  const me = await (await api('/api/me')).json();
  state.name = me.name;
  state.limit = me.max_question_chars;
  $('who').textContent = me.name;
  $('initial').textContent = me.name.slice(0, 1).toUpperCase();
  $('model').textContent = me.model;
  const down = me.servers.filter((server) => !server.ok).length;
  $('servers').replaceChildren(...me.servers.map((server) => el('li', {class: server.ok ? 'ok' : 'down'}, el('span', {class: 'dot'}),
                                                                 el('span', {text: server.name}), el('span', {class: 'state', text: server.ok ? '연결됨' : '연결 안 됨'}))));
  $('status').className = 'chip ' + (!me.servers.length ? 'wait' : down ? 'warn' : 'ok');
  $('statuslabel').textContent = !me.servers.length ? `${me.model} · 확인 중` : `${me.model} · 자료 ${me.servers.length - down}/${me.servers.length}`;
  return me.servers.length;
}

(async () => {
  try {
    if (!await loadStatus()) setTimeout(() => loadStatus().catch(() => {}), 2500);     // 서버가 막 떴으면 자료 서버 확인이 조금 늦다
    $('greeting').textContent = `${state.name}님, 무엇을 찾아 드릴까요?`;
    await loadList();
    updateSend();
    $('input').focus();
  } catch { /* 로그인 화면으로 넘어가는 중 */ }
})();
