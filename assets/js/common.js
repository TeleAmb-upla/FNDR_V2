(function () {
  'use strict';

  const DATA_BASE = 'data_web';

  async function loadManifest() {
    if (window.FNDR_MANIFEST) return window.FNDR_MANIFEST;
    const r = await fetch(DATA_BASE + '/manifest.json');
    if (!r.ok) throw new Error('manifest.json no disponible');
    return r.json();
  }

  const nf = (digits) => new Intl.NumberFormat('es-CL', { maximumFractionDigits: digits, minimumFractionDigits: 0 });

  function fmt(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    return nf(digits == null ? 2 : digits).format(v);
  }

  function dataUrl(path) { return DATA_BASE + '/' + path; }

  function initNav() {
    const nav = document.getElementById('navmenu');
    const toggle = document.querySelector('.mobile-nav-toggle');
    if (!nav || !toggle) return;
    toggle.addEventListener('click', () => {
      nav.classList.toggle('open');
      toggle.classList.toggle('bi-list');
      toggle.classList.toggle('bi-x');
    });
  }

  document.addEventListener('DOMContentLoaded', initNav);

  window.FNDR = { DATA_BASE, loadManifest, fmt, dataUrl };
})();
