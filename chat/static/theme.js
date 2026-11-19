// 화면이 그려지기 전에 저장해 둔 테마와 사이드바 상태를 적용한다(깜빡임 방지). head 에서 먼저 읽힌다.
(function () {
  document.documentElement.classList.add('js');
  try {
    var theme = localStorage.getItem('theme');
    if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
    if (localStorage.getItem('sidebar') === 'closed') document.documentElement.dataset.sidebar = 'closed';
  } catch (error) { /* 저장소를 못 쓰는 환경이면 기본값으로 */ }
})();
