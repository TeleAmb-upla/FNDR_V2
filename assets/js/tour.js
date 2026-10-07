(function () {
  'use strict';

  let steps = [];
  let idx = 0;
  const $ = (id) => document.getElementById(id);

  function visible(sel) {
    const el = document.querySelector(sel);
    if (!el || el.hidden || el.closest('[hidden]')) return null;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? el : null;
  }

  function place(el) {
    const pad = 6;
    const r = el.getBoundingClientRect();
    const spot = $('tourSpot');
    const isMap = el.id === 'map';
    const box = isMap
      ? { left: r.left + r.width * 0.35, top: r.top + r.height * 0.25, width: r.width * 0.4, height: r.height * 0.5 }
      : { left: r.left - pad, top: r.top - pad, width: r.width + pad * 2, height: r.height + pad * 2 };
    Object.assign(spot.style, { left: box.left + 'px', top: box.top + 'px', width: box.width + 'px', height: box.height + 'px' });

    const card = $('tourCard');
    const cw = card.offsetWidth;
    const ch = card.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = box.left + box.width + 16;
    let top = box.top;
    if (left + cw > vw - 12) {
      left = Math.max(12, box.left - cw - 16);
      if (left < 12 || box.left - cw - 16 < 12) {
        left = Math.min(vw - cw - 12, Math.max(12, box.left));
        top = box.top + box.height + 14;
        if (top + ch > vh - 12) top = Math.max(12, box.top - ch - 14);
      }
    }
    top = Math.min(Math.max(12, top), vh - ch - 12);
    card.style.left = left + 'px';
    card.style.top = top + 'px';
  }

  function show() {
    while (idx < steps.length) {
      const s = steps[idx];
      if (s.before) s.before();
      const el = visible(s.el);
      if (el) {
        $('tourTitle').textContent = s.title;
        $('tourText').textContent = s.text;
        $('tourProgress').innerHTML = steps.map((_, i) => `<span class="${i <= idx ? 'on' : ''}"></span>`).join('');
        $('tourNext').textContent = idx === steps.length - 1 ? 'Terminar' : 'Siguiente';
        el.scrollIntoView({ block: 'nearest' });
        requestAnimationFrame(() => place(el));
        return;
      }
      idx++;
    }
    end();
  }

  function end() {
    $('tour').hidden = true;
    window.removeEventListener('resize', onResize);
  }

  function onResize() {
    const s = steps[idx];
    const el = s && visible(s.el);
    if (el) place(el);
  }

  function start(list) {
    steps = list;
    idx = 0;
    $('tour').hidden = false;
    window.addEventListener('resize', onResize);
    show();
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('tourNext').addEventListener('click', () => { idx++; show(); });
    $('tourSkip').addEventListener('click', end);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('tour').hidden) end(); });
  });

  window.Tour = { start };
})();
