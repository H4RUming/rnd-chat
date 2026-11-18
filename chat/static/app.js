// 채팅 화면. 모델의 답과 도구 결과는 신뢰할 수 없는 글이므로 innerHTML 을 쓰지 않고 DOM 노드로만 그린다.
'use strict';

const API_HEADERS = {'Content-Type': 'application/json', 'X-Requested-With': 'rnd-chat'};
const $ = (id) => document.getElementById(id);
const state = {conversation: null, busy: false};

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === 'class') node.className = value; else if (key === 'text') node.textContent = value; else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
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
  if (!href) return document.createTextNode(text);
  return el('a', {href, target: '_blank', rel: 'noopener noreferrer', text});
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\((?:[^()\s]|\([^()\s]*\))+\))|(https?:\/\/[^\s<>()"']+[^\s<>()"'.,;:!?])/g;

function inline(parent, text) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) parent.append(text.slice(last, match.index));
    const token = match[0];
    if (match[1]) parent.append(el('code', {text: token.slice(1, -1)}));
    else if (match[2]) { const strong = el('strong'); inline(strong, token.slice(2, -2)); parent.append(strong); }
    else if (match[3]) { const cut = token.lastIndexOf(']('); parent.append(link(token.slice(1, cut), token.slice(cut + 2, -1))); }
    else parent.append(link(token, token));
    last = match.index + token.length;
  }
  if (last < text.length) parent.append(text.slice(last));
  return parent;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
const isRule = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

function markdown(text) {
  const root = document.createDocumentFragment();
  const lines = text.replace(/\r/g, '').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.trim().startsWith('```')) {                         // 코드 블록
      const body = [];
      for (i++; i < lines.length && !lines[i].trim().startsWith('```'); i++) body.push(lines[i]);
      i++;
      root.append(el('pre', null, el('code', {text: body.join('\n')})));
    } else if (/^#{1,6}\s/.test(line)) {                          // 제목
      const level = Math.min(line.match(/^#+/)[0].length + 1, 5);
      root.append(inline(el('h' + level), line.replace(/^#+\s*/, '')));
      i++;
    } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {             // 가로줄
      root.append(el('hr')); i++;
    } else if (line.includes('|') && i + 1 < lines.length && isRule(lines[i + 1])) {   // 표
      const table = el('table'), head = el('tr');
      for (const cell of cells(line)) head.append(inline(el('th'), cell));
      table.append(el('thead', null, head));
      const body = el('tbody');
      for (i += 2; i < lines.length && lines[i].includes('|') && lines[i].trim(); i++) {
        const row = el('tr');
        for (const cell of cells(lines[i])) row.append(inline(el('td'), cell));
        body.append(row);
      }
      table.append(body);
      root.append(el('div', {class: 'table'}, table));
    } else if (/^\s*>/.test(line)) {                              // 인용
      const quote = el('blockquote');
      for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.append(inline(el('p'), lines[i].replace(/^\s*>\s?/, '')));
      root.append(quote);
    } else if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {             // 목록
      const ordered = /^\s*\d+[.)]\s+/.test(line), list = el(ordered ? 'ol' : 'ul');
      for (; i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i]); i++) {
        list.append(inline(el('li'), lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, '')));
      }
      root.append(list);
    } else {                                                       // 문단
      const paragraph = el('p');
      let first = true;
      for (; i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*>|\s*([-*•]|\d+[.)])\s+|\s*```)/.test(lines[i])
             && !(lines[i].includes('|') && i + 1 < lines.length && isRule(lines[i + 1])); i++) {
        if (!first) paragraph.append(el('br'));
        inline(paragraph, lines[i]);
        first = false;
      }
      root.append(paragraph);
    }
  }
  return root;
}

// ── 화면 ────────────────────────────────────────────────────────────────
function addMessage(role, text) {
  $('empty').hidden = true;
  const body = el('div', {class: 'body'});
  if (role === 'user') body.textContent = text; else body.append(markdown(text));
  const node = el('article', {class: role}, body);
  $('log').append(node);
  node.scrollIntoView({block: 'end'});
  return node;
}

function describe(event) {
  const pairs = Object.entries(event.arguments || {}).filter(([, value]) => value !== '' && value !== 0 && value !== null)
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`);
  return pairs.length ? `${event.name} (${pairs.join(', ')})` : event.name;
}

async function loadList() {
  const response = await fetch('/api/conversations');
  if (response.status === 401) return location.assign('/login');
  const {conversations} = await response.json();
  const list = $('list');
  list.replaceChildren();
  for (const item of conversations) {
    const open = el('button', {type: 'button', class: 'open' + (item.id === state.conversation ? ' current' : ''), text: item.title || '대화'});
    open.addEventListener('click', () => openConversation(item.id));
    const remove = el('button', {type: 'button', class: 'remove', 'aria-label': '대화 지우기', text: '×'});
    remove.addEventListener('click', async () => {
      await fetch('/api/conversations/' + encodeURIComponent(item.id), {method: 'DELETE', headers: API_HEADERS});
      if (state.conversation === item.id) newConversation();
      loadList();
    });
    list.append(el('div', {class: 'item'}, open, remove));
  }
}

function newConversation() {
  state.conversation = null;
  $('title').textContent = '새 대화';
  for (const node of [...$('log').querySelectorAll('article')]) node.remove();
  $('empty').hidden = false;
  document.body.classList.remove('side-open');
  loadList();
  $('input').focus();
}

async function openConversation(id) {
  if (state.busy) return;
  const response = await fetch('/api/conversations/' + encodeURIComponent(id));
  if (!response.ok) return loadList();
  const item = await response.json();
  state.conversation = item.id;
  $('title').textContent = item.title;
  for (const node of [...$('log').querySelectorAll('article')]) node.remove();
  for (const message of item.messages) addMessage(message.role, message.content);
  document.body.classList.remove('side-open');
  loadList();
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  $('send').disabled = true;
  addMessage('user', question);
  const steps = el('ul', {class: 'steps'}), status = el('p', {class: 'status', text: '찾는 중…'});
  const pending = el('article', {class: 'assistant pending'}, el('div', {class: 'body'}, status, steps));
  $('log').append(pending);
  pending.scrollIntoView({block: 'end'});
  const fail = (message) => { pending.classList.add('failed'); status.textContent = message; };
  try {
    const response = await fetch('/api/chat', {method: 'POST', headers: API_HEADERS,
                                               body: JSON.stringify({message: question, conversation_id: state.conversation})});
    if (response.status === 401) return location.assign('/login');
    if (!response.ok) return fail((await response.json().catch(() => ({}))).error || '요청이 실패했습니다.');
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (!chunk.startsWith('data: ')) continue;
        const event = JSON.parse(chunk.slice(6));
        if (event.type === 'queued') status.textContent = '앞선 질문을 처리하는 중이라 잠시 기다립니다…';
        else if (event.type === 'tool_call') { status.textContent = '자료를 찾는 중…'; steps.append(el('li', {text: describe(event)})); }
        else if (event.type === 'tool_result' && event.is_error && steps.lastChild) steps.lastChild.classList.add('error');
        else if (event.type === 'answer') {
          const used = steps.childElementCount;
          const body = el('div', {class: 'body'});
          if (used) body.append(el('details', null, el('summary', {text: `자료 조회 ${used}회`}), steps));
          body.append(markdown(event.text || '(빈 답)'));
          if (event.stopped === 'max_steps') body.append(el('p', {class: 'note', text: '조회 횟수 상한에 걸려 여기까지 찾은 내용으로 답했습니다.'}));
          if (event.stopped === 'length') body.append(el('p', {class: 'note', text: '답이 길이 상한에서 끊겼습니다.'}));
          pending.replaceChildren(body);
          pending.classList.remove('pending');
          pending.scrollIntoView({block: 'nearest'});
        } else if (event.type === 'done') {
          state.conversation = event.conversation_id;
          $('title').textContent = event.title;
        } else if (event.type === 'error') fail(event.message);
      }
    }
  } catch { fail('연결이 끊겼습니다. 다시 시도해 주세요.'); }
  finally {
    state.busy = false;
    $('send').disabled = false;
    loadList();
    $('input').focus();
  }
}

// ── 시작 ────────────────────────────────────────────────────────────────
$('ask').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('input'), question = input.value;
  input.value = '';
  input.style.height = '';
  ask(question);
});
$('input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('ask').requestSubmit(); }
});
$('input').addEventListener('input', (event) => {
  event.target.style.height = '';
  event.target.style.height = Math.min(event.target.scrollHeight, 200) + 'px';
});
$('new').addEventListener('click', newConversation);
$('menu').addEventListener('click', () => document.body.classList.toggle('side-open'));
for (const button of $('examples').querySelectorAll('button')) button.addEventListener('click', () => ask(button.textContent));

(async () => {
  const response = await fetch('/api/me');
  if (response.status === 401) return location.assign('/login');
  const me = await response.json();
  $('who').textContent = me.name;
  $('input').maxLength = me.max_question_chars;
  for (const server of me.servers) $('servers').append(el('li', {class: server.ok ? 'ok' : 'down', text: server.name + (server.ok ? '' : ' (연결 안 됨)')}));
  loadList();
})();
