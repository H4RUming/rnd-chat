// 로그인 화면: 빈 칸 안내, 비밀번호 보기, Caps Lock 알림, 보내는 동안의 표시. 배경과 화면 밝기 전환은 backdrop.js 에 있다.
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const form = $('login'), name = $('username'), password = $('password'), go = $('go');
  const fields = [[name, $('namehelp')], [password, $('passwordhelp')]];

  const mark = (input, help, bad) => {
    input.closest('.field').classList.toggle('bad', bad);
    input.setAttribute('aria-invalid', bad ? 'true' : 'false');
    help.hidden = !bad;
  };
  const settle = () => {                                   // 보내기 전 모습으로
    go.disabled = false;
    $('golabel').textContent = '로그인';
    $('progress').hidden = true;
  };

  // 서버가 거절한 뒤 돌아온 화면이면(이름·비밀번호 불일치, 잠김) 두 칸에 표시한다.
  const refusal = form.querySelector('.alert');
  if (refusal) form.classList.add('refused');

  for (const [input, help] of fields) {
    input.addEventListener('input', () => { mark(input, help, false); form.classList.remove('refused'); });
  }

  // 이름 칸에서 Enter: 비밀번호가 비어 있으면 보내지 않고 다음 칸으로 간다.
  name.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.isComposing || password.value) return;
    event.preventDefault();
    if (name.value.trim()) password.focus(); else mark(name, $('namehelp'), true);
  });

  form.addEventListener('submit', (event) => {
    const empty = fields.filter(([input]) => !(input === name ? input.value.trim() : input.value));
    for (const [input, help] of fields) mark(input, help, empty.some(([missing]) => missing === input));
    if (empty.length) {
      event.preventDefault();
      if (refusal) refusal.hidden = true;                  // 지난 시도의 안내와 겹치지 않게
      form.classList.remove('refused');
      empty[0][0].focus();
      return;
    }
    go.disabled = true;
    $('golabel').textContent = '확인하는 중';
    $('progress').hidden = false;
  });
  window.addEventListener('pageshow', (event) => { if (event.persisted) settle(); });    // 뒤로 가기로 돌아온 화면

  $('reveal').addEventListener('click', () => {
    const shown = password.type === 'password';
    const caret = [password.selectionStart, password.selectionEnd];
    password.type = shown ? 'text' : 'password';
    $('reveal').setAttribute('aria-pressed', String(shown));
    $('reveal').setAttribute('aria-label', shown ? '비밀번호 숨기기' : '비밀번호 보기');
    $('reveal').title = shown ? '비밀번호 숨기기' : '비밀번호 보기';
    $('revealicon').setAttribute('href', shown ? '#i-eye-off' : '#i-eye');
    password.focus();
    try { password.setSelectionRange(caret[0], caret[1]); } catch (error) { /* 자리를 못 되돌려도 입력은 된다 */ }
  });

  const caps = (event) => { if (event.getModifierState) $('caps').hidden = !event.getModifierState('CapsLock'); };
  password.addEventListener('keydown', caps);
  password.addEventListener('keyup', caps);
  password.addEventListener('blur', () => { $('caps').hidden = true; });
})();
