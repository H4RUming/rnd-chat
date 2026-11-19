// 회원가입 신청 화면: 세 칸을 확인해 보내고, 받았으면 "검토 뒤 등록된다"는 안내로 바꾼다. 바로 가입되지 않는다.
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const form = $('signup'), go = $('go');
  const inputs = {username: $('username'), email: $('email'), password: $('password')};
  const EMPTY = {username: '아이디를 입력하세요.', email: '이메일을 입력하세요.', password: '비밀번호를 입력하세요.'};

  const mark = (field, message) => {
    inputs[field].closest('.field').classList.toggle('bad', Boolean(message));
    inputs[field].setAttribute('aria-invalid', message ? 'true' : 'false');
    $(field + 'help').textContent = message || '';
    $(field + 'help').hidden = !message;
  };
  const whole = (message) => { $('formhelp').textContent = message || ''; $('formhelp').hidden = !message; };
  const busy = (on) => {
    go.disabled = on;
    $('golabel').textContent = on ? '보내는 중' : '가입 신청';
    $('progress').hidden = !on;
  };

  for (const field of Object.keys(inputs)) inputs[field].addEventListener('input', () => { mark(field, ''); whole(''); });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    whole('');
    const values = {username: inputs.username.value.trim(), email: inputs.email.value.trim(), password: inputs.password.value};
    const empty = Object.keys(inputs).filter((field) => !values[field]);
    for (const field of Object.keys(inputs)) mark(field, empty.includes(field) ? EMPTY[field] : '');
    if (empty.length) return inputs[empty[0]].focus();
    busy(true);
    let status = 0, reply = {};
    try {
      const response = await fetch('/api/signup', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'rnd-chat'},
                                                   body: JSON.stringify(values), credentials: 'same-origin'});
      status = response.status;
      reply = await response.json();
    } catch (error) { /* 아래에서 알린다 */ }
    busy(false);
    if (status === 200 && reply.ok) {
      form.hidden = true;
      $('done').hidden = false;
      $('done').querySelector('a').focus();
      return;
    }
    const message = reply.error || '신청을 보내지 못했습니다. 잠시 뒤 다시 해 보세요.';
    if (reply.field && inputs[reply.field]) { mark(reply.field, message); inputs[reply.field].focus(); } else whole(message);
  });

  $('reveal').addEventListener('click', () => {
    const password = inputs.password, shown = password.type === 'password';
    password.type = shown ? 'text' : 'password';
    $('reveal').setAttribute('aria-pressed', String(shown));
    $('reveal').setAttribute('aria-label', shown ? '비밀번호 숨기기' : '비밀번호 보기');
    $('reveal').title = shown ? '비밀번호 숨기기' : '비밀번호 보기';
    $('revealicon').setAttribute('href', shown ? '#i-eye-off' : '#i-eye');
    password.focus();
  });
})();
