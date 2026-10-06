/* Same patched Vanta topology and palette as aubree.wtf; decoration only. */
(function () {
  'use strict';
  const element = document.getElementById('wallpaper');
  const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
  if (!element || !window.VANTA?.TOPOLOGY || !window.p5) return;
  let effect;
  let paused = preference.matches;
  let settle;
  try {
    effect = window.VANTA.TOPOLOGY({el: element, p5: window.p5, mouseControls: !paused,
      touchControls: !paused, gyroControls: false, minHeight: 200, minWidth: 200,
      scale: 1, scaleMobile: 1, color: 0xb400ff, backgroundColor: 0x2a});
  } catch { return; }
  function apply() {
    const sketch = effect.p5;
    if (!sketch) return;
    if (document.hidden || paused) sketch.noLoop();
    else { effect.resize(); sketch.loop(); }
  }
  document.addEventListener('visibilitychange', apply);
  preference.addEventListener('change', event => {
    clearTimeout(settle);
    paused = event.matches;
    effect.setOptions?.({mouseControls: !paused, touchControls: !paused});
    apply();
  });
  if (paused) settle = setTimeout(apply, 400);
  else apply();
  window.addEventListener('pagehide', () => { clearTimeout(settle); effect.p5?.noLoop(); });
  window.addEventListener('pageshow', event => { if (event.persisted) apply(); });
})();
