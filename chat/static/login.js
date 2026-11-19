// 로그인 화면: 빈 칸 안내, 비밀번호 보기, Caps Lock 알림, 보내는 동안의 표시, 화면 밝기 전환, 그리고 배경의 빛과 연결망.
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
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

  // ── 배경: 색 빛 덩어리가 천천히 흐르고, 그 위로 떠다니는 마디들이 가까워지면 선으로 이어진다. 포인터는 빛을 끌어당기고 마디를 밀어낸다 ──
  const field = (function () {
    const canvas = $('field');
    const ctx = canvas.getContext && canvas.getContext('2d');
    if (!ctx) return {recolor() {}};
    const SHADES = 24, REACH = 140, LINK = 132;            // REACH: 포인터가 마디를 밀어내는 거리, LINK: 마디끼리 선이 이어지는 거리(px)
    const STOPS = {light: ['#5a3df0', '#8a6ae8', '#c990dc', '#f08fb2'], dark: ['#8f7aff', '#b39cf7', '#dcaee8', '#f6b8c6']};
    const GLOWS = {light: [[90, 61, 240, 0.20], [240, 134, 171, 0.24], [150, 120, 250, 0.16]], dark: [[111, 85, 255, 0.30], [240, 134, 171, 0.20], [150, 120, 250, 0.22]]};
    const MIST = 10;                                       // 빛은 화면의 1/10 크기로 그려서 늘려 붙인다(어차피 흐릿한 그림이라 티가 나지 않고, 훨씬 가볍다)
    const mist = document.createElement('canvas'), haze = mist.getContext('2d');
    const calm = window.matchMedia('(prefers-reduced-motion: reduce)');
    const pointer = {x: -9999, y: -9999};
    let width = 0, height = 0, nodes = [], glows = [], shades = [], tints = [], running = false, clock = 0;

    const hex = (value) => [1, 3, 5].map((at) => parseInt(value.slice(at, at + 2), 16));
    function recolor() {
      const scheme = getComputedStyle(root).colorScheme.includes('dark') ? 'dark' : 'light';
      const stops = STOPS[scheme].map(hex);
      shades = Array.from({length: SHADES}, (_, index) => {
        const at = index / (SHADES - 1) * (stops.length - 1), low = Math.min(Math.floor(at), stops.length - 2), mix = at - low;
        return stops[low].map((channel, i) => Math.round(channel + (stops[low + 1][i] - channel) * mix)).join(',');
      });
      tints = GLOWS[scheme];
      if (!running) draw();
    }

    function build() {
      const scale = Math.min(window.devicePixelRatio || 1, 2);
      width = window.innerWidth; height = window.innerHeight;
      canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      mist.width = Math.ceil(width / MIST); mist.height = Math.ceil(height / MIST);
      const count = Math.round(Math.max(34, Math.min(width * height / 9500, 150)));
      nodes = Array.from({length: count}, (_, index) => nodes[index] || {x: Math.random() * width, y: Math.random() * height, vx: 0, vy: 0,
        cx: (Math.random() - 0.5) * 0.5, cy: (Math.random() - 0.5) * 0.5, size: 1.3 + Math.random() * 1.6});
      for (const node of nodes) { node.x = Math.min(node.x, width); node.y = Math.min(node.y, height); }
      // 빛 덩어리 셋: 저마다 다른 빠르기로 화면을 느리게 돈다
      glows = [0, 1, 2].map((index) => ({at: index * 2.1, pace: [0.00021, 0.00017, 0.00026][index], x: 0, y: 0}));
      settle(true);
      if (calm.matches) draw(); else start();
    }

    // 빛 덩어리의 자리: 느린 궤도 위를 돌되, 포인터가 있으면 그쪽으로 조금 끌려간다.
    function settle(jump) {
      glows.forEach((glow, index) => {
        const angle = glow.at + clock * glow.pace * 16;
        let x = width * (0.5 + Math.cos(angle * (1 + index * 0.13)) * 0.34), y = height * (0.5 + Math.sin(angle * 1.3 + index) * 0.3);
        if (pointer.x > -999) { x += (pointer.x - x) * 0.22; y += (pointer.y - y) * 0.22; }
        glow.x = jump ? x : glow.x + (x - glow.x) * 0.03;
        glow.y = jump ? y : glow.y + (y - glow.y) * 0.03;
      });
    }

    function step() {
      clock += 1;
      settle(false);
      for (const node of nodes) {
        node.vx += (node.cx - node.vx) * 0.03; node.vy += (node.cy - node.vy) * 0.03;      // 제 속도로 흘러가려는 힘
        const dx = node.x - pointer.x, dy = node.y - pointer.y, far = dx * dx + dy * dy;
        if (far < REACH * REACH && far > 0.01) {             // 포인터 가까이에서는 바깥으로 밀린다(가까울수록 세게)
          const distance = Math.sqrt(far), push = (1 - distance / REACH) ** 2 * 1.6;
          node.vx += dx / distance * push; node.vy += dy / distance * push;
        }
        node.x += node.vx; node.y += node.vy;
        if (node.x < -20) node.x = width + 20; else if (node.x > width + 20) node.x = -20;     // 화면 밖으로 나가면 반대편에서 들어온다
        if (node.y < -20) node.y = height + 20; else if (node.y > height + 20) node.y = -20;
      }
    }

    const shadeAt = (x) => shades[Math.max(0, Math.min(SHADES - 1, Math.floor(x / width * SHADES)))];
    function draw() {
      ctx.clearRect(0, 0, width, height);
      const spread = Math.max(width, height) * 0.48 / MIST;
      haze.clearRect(0, 0, mist.width, mist.height);
      glows.forEach((glow, index) => {                       // 빛 덩어리: 가운데가 짙고 바깥으로 사라지는 원
        const [r, g, b, alpha] = tints[index % tints.length];
        const light = haze.createRadialGradient(glow.x / MIST, glow.y / MIST, 0, glow.x / MIST, glow.y / MIST, spread);
        light.addColorStop(0, `rgba(${r},${g},${b},${alpha})`);
        light.addColorStop(1, `rgba(${r},${g},${b},0)`);
        haze.fillStyle = light;
        haze.fillRect(0, 0, mist.width, mist.height);
      });
      ctx.drawImage(mist, 0, 0, width, height);
      ctx.lineWidth = 1;
      for (let first = 0; first < nodes.length; first += 1) {   // 가까운 마디끼리 선으로 잇는다(가까울수록 진하게)
        const a = nodes[first];
        for (let second = first + 1; second < nodes.length; second += 1) {
          const b = nodes[second], dx = a.x - b.x, dy = a.y - b.y, far = dx * dx + dy * dy;
          if (far > LINK * LINK) continue;
          ctx.strokeStyle = `rgba(${shadeAt((a.x + b.x) / 2)},${(1 - Math.sqrt(far) / LINK) * 0.5})`;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
        const dx = a.x - pointer.x, dy = a.y - pointer.y, far = dx * dx + dy * dy;      // 포인터도 마디 하나처럼 이어진다
        if (far < 190 * 190) {
          ctx.strokeStyle = `rgba(${shadeAt(a.x)},${(1 - Math.sqrt(far) / 190) * 0.7})`;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(pointer.x, pointer.y); ctx.stroke();
        }
      }
      for (const node of nodes) {
        ctx.fillStyle = `rgba(${shadeAt(node.x)},0.85)`;
        ctx.beginPath(); ctx.arc(node.x, node.y, node.size, 0, 6.283); ctx.fill();
      }
    }

    function loop() {
      if (!running) return;
      step(); draw();
      requestAnimationFrame(loop);
    }
    function start() { if (!running && !calm.matches && !document.hidden) { running = true; requestAnimationFrame(loop); } }
    function stop() { running = false; }

    // 누르면 그 자리에서 마디들이 한 번 흩어진다.
    function burst(x, y) {
      for (const node of nodes) {
        const dx = node.x - x, dy = node.y - y, distance = Math.sqrt(dx * dx + dy * dy) || 1;
        if (distance < 220) { const kick = (1 - distance / 220) * 9; node.vx += dx / distance * kick; node.vy += dy / distance * kick; }
      }
    }

    window.addEventListener('pointermove', (event) => { pointer.x = event.clientX; pointer.y = event.clientY; }, {passive: true});
    window.addEventListener('pointerdown', (event) => { pointer.x = event.clientX; pointer.y = event.clientY; if (!event.target.closest('#login, button')) burst(event.clientX, event.clientY); }, {passive: true});
    const away = () => { pointer.x = -9999; pointer.y = -9999; };
    window.addEventListener('pointerup', (event) => { if (event.pointerType !== 'mouse') away(); }, {passive: true});
    document.addEventListener('pointerleave', away);
    window.addEventListener('blur', away);
    document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); else start(); });
    calm.addEventListener('change', () => { stop(); build(); });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', recolor);
    let waiting = 0;
    window.addEventListener('resize', () => { clearTimeout(waiting); waiting = setTimeout(build, 120); });

    recolor();
    build();
    return {recolor};
  })();

  const NEXT = {system: 'light', light: 'dark', dark: 'system'};
  const LABEL = {system: '화면: 시스템 설정', light: '화면: 밝게', dark: '화면: 어둡게'}, ICON = {system: '#i-monitor', light: '#i-sun', dark: '#i-moon'};
  const show = (theme) => { $('theme').title = LABEL[theme]; $('theme').setAttribute('aria-label', LABEL[theme]); $('themeicon').setAttribute('href', ICON[theme]); };
  show(root.dataset.theme || 'system');
  $('theme').addEventListener('click', () => {
    const next = NEXT[root.dataset.theme || 'system'];
    if (next === 'system') delete root.dataset.theme; else root.dataset.theme = next;
    try { if (next === 'system') localStorage.removeItem('theme'); else localStorage.setItem('theme', next); } catch (error) { /* 저장 못 해도 바뀌기는 한다 */ }
    show(next);
    field.recolor();
  });
})();
