(function () {
  'use strict';

  const ASPRS = {
    0: ['Nunca clasificado', '#9e9e9e'], 1: ['Sin clasificar', '#bdbdbd'], 2: ['Suelo', '#b07d48'],
    3: ['Vegetación baja', '#a6d96a'], 4: ['Vegetación media', '#5fcf80'], 5: ['Vegetación alta', '#1a9641'],
    6: ['Edificación', '#e05a47'], 7: ['Ruido', '#ff00ff'], 9: ['Agua', '#3a86ff'], 17: ['Puente', '#ffbe0b'],
  };
  const ELEV_STOPS = ['#2c7bb6', '#00a6ca', '#00ccbc', '#90eb9d', '#ffff8c', '#f9d057', '#f29e2e', '#e76818', '#d7191c'];
  const INT_STOPS = ['#111111', '#555555', '#aaaaaa', '#ffffff'];

  let renderer, scene, camera, controls, points, el;
  let current = null;
  let mode = 'rgb';
  let running = false;
  const cache = new Map();

  const $ = (id) => document.getElementById(id);

  function hexToRgb(h) {
    const n = parseInt(h.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function ramp(stops) {
    const c = stops.map(hexToRgb);
    return (t) => {
      t = Math.max(0, Math.min(1, t)) * (c.length - 1);
      const i = Math.min(c.length - 2, Math.floor(t));
      const k = t - i;
      return [0, 1, 2].map((j) => c[i][j] + (c[i + 1][j] - c[i][j]) * k);
    };
  }

  function init() {
    if (renderer) return true;
    if (!window.THREE) { status('No se pudo cargar three.js (revisa la conexión a internet).'); return false; }
    el = $('las3dView');
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.insertBefore(renderer.domElement, el.firstChild);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(50, 1, 0.5, 50000);
    camera.up.set(0, 0, 1);
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = true;
    window.addEventListener('resize', resize);
    bindToolbar();
    return true;
  }

  function resize() {
    if (!renderer || el.hidden) return;
    const w = el.clientWidth;
    const h = el.clientHeight;
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }

  function tick() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  }

  function status(t) { $('las3dStatus').textContent = t || ''; }

  async function fetchCloud(url, meta) {
    if (cache.has(url)) return cache.get(url);
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const total = +r.headers.get('content-length') || meta.count * 11;
    const reader = r.body.getReader();
    const buf = new Uint8Array(total);
    let off = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf.set(value, off);
      off += value.length;
      status(`Cargando nube de puntos… ${Math.round((off / total) * 100)}%`);
    }
    cache.set(url, buf.buffer);
    return buf.buffer;
  }

  function buildPoints(buf, meta) {
    const n = meta.count;
    const q = new Uint16Array(buf, 0, n * 3);
    const rgb = new Uint8Array(buf, n * 6, n * 3);
    const inten = new Uint8Array(buf, n * 9, n);
    const cls = new Uint8Array(buf, n * 10, n);
    const span = [0, 1, 2].map((i) => meta.max[i] - meta.min[i]);
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      pos[i * 3] = (q[i * 3] / 65535 - 0.5) * span[0];
      pos[i * 3 + 1] = (q[i * 3 + 1] / 65535 - 0.5) * span[1];
      pos[i * 3 + 2] = (q[i * 3 + 2] / 65535) * span[2];
    }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geom.setAttribute('color', new THREE.BufferAttribute(new Float32Array(n * 3), 3));
    geom.userData = { q, rgb, inten, cls, span, n };
    const mat = new THREE.PointsMaterial({ size: +$('lasPointSize').value, sizeAttenuation: false, vertexColors: true });
    return new THREE.Points(geom, mat);
  }

  function availableModes(meta) {
    const m = [];
    if (meta.has_rgb) m.push(['rgb', 'Color real']);
    m.push(['elev', 'Elevación'], ['int', 'Intensidad']);
    if (Object.keys(meta.classes || {}).length > 1) m.push(['cls', 'Clasificación']);
    return m;
  }

  function applyColors() {
    if (!points) return;
    const g = points.geometry;
    const { q, rgb, inten, cls, n } = g.userData;
    const col = g.attributes.color.array;
    const meta = current.meta;
    if (mode === 'rgb') {
      for (let i = 0; i < n * 3; i++) col[i] = rgb[i] / 255;
    } else if (mode === 'elev') {
      const f = ramp(ELEV_STOPS);
      for (let i = 0; i < n; i++) {
        const c = f(q[i * 3 + 2] / 65535);
        col[i * 3] = c[0] / 255; col[i * 3 + 1] = c[1] / 255; col[i * 3 + 2] = c[2] / 255;
      }
    } else if (mode === 'int') {
      const f = ramp(INT_STOPS);
      for (let i = 0; i < n; i++) {
        const c = f(inten[i] / 255);
        col[i * 3] = c[0] / 255; col[i * 3 + 1] = c[1] / 255; col[i * 3 + 2] = c[2] / 255;
      }
    } else {
      const lut = {};
      Object.keys(meta.classes).forEach((k) => { lut[k] = hexToRgb((ASPRS[k] || ['', '#cccccc'])[1]); });
      for (let i = 0; i < n; i++) {
        const c = lut[cls[i]] || [200, 200, 200];
        col[i * 3] = c[0] / 255; col[i * 3 + 1] = c[1] / 255; col[i * 3 + 2] = c[2] / 255;
      }
    }
    g.attributes.color.needsUpdate = true;
    renderLegend();
  }

  function renderLegend() {
    const box = $('las3dLegend');
    const meta = current && current.meta;
    if (!meta) { box.innerHTML = ''; return; }
    const fmt = window.FNDR.fmt;
    if (mode === 'elev') {
      box.innerHTML = `<b>Elevación (m)</b><div class="grad" style="background:linear-gradient(to right, ${ELEV_STOPS.join(',')})"></div>
        <div class="ends"><span>${fmt(meta.min[2], 0)}</span><span>${fmt(meta.max[2], 0)}</span></div>`;
    } else if (mode === 'int') {
      box.innerHTML = `<b>Intensidad</b><div class="grad" style="background:linear-gradient(to right, ${INT_STOPS.join(',')})"></div>
        <div class="ends"><span>baja</span><span>alta</span></div>`;
    } else if (mode === 'cls') {
      const total = Object.values(meta.classes).reduce((a, b) => a + b, 0);
      box.innerHTML = '<b>Clasificación</b>' + Object.entries(meta.classes).map(([k, v]) => {
        const [name, color] = ASPRS[k] || ['Clase ' + k, '#cccccc'];
        return `<div class="row"><span class="sw" style="background:${color}"></span>${name} <small style="margin-left:auto">${fmt((v / total) * 100, 1)}%</small></div>`;
      }).join('');
    } else {
      box.innerHTML = '';
    }
  }

  function buildModeButtons(meta) {
    const box = $('lasColorModes');
    const modes = availableModes(meta);
    if (!modes.some(([k]) => k === mode)) mode = modes[0][0];
    box.innerHTML = modes.map(([k, l]) => `<button type="button" data-mode="${k}" class="${k === mode ? 'active' : ''}">${l}</button>`).join('');
    box.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      mode = b.dataset.mode;
      box.querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
      applyColors();
    }));
  }

  function resetView() {
    if (!current) return;
    const s = current.meta;
    const dx = s.max[0] - s.min[0];
    const dy = s.max[1] - s.min[1];
    const r = Math.max(dx, dy);
    const zs = +$('lasZScale').value;
    const zc = ((s.max[2] - s.min[2]) * zs) / 2;
    controls.target.set(0, 0, zc);
    camera.position.set(0, -r * 0.85, zc + r * 0.65);
    camera.near = r / 2000;
    camera.far = r * 20;
    camera.updateProjectionMatrix();
    controls.update();
  }

  function bindToolbar() {
    $('lasPointSize').addEventListener('input', (e) => { if (points) points.material.size = +e.target.value; });
    $('lasZScale').addEventListener('input', (e) => { if (points) points.scale.z = +e.target.value; });
    $('lasReset').addEventListener('click', resetView);
  }

  async function show(flight, url) {
    if (!init()) return;
    resize();
    if (!running) { running = true; tick(); }
    if (current && current.url === url) return;
    const meta = flight.layers.lidar;
    current = { url, meta };
    if (points) { scene.remove(points); points.geometry.dispose(); points.material.dispose(); points = null; }
    renderLegend();
    $('las3dLegend').innerHTML = '';
    status('Cargando nube de puntos…');
    try {
      const buf = await fetchCloud(url, meta);
      if (!current || current.url !== url) return;
      points = buildPoints(buf, meta);
      points.scale.z = +$('lasZScale').value;
      scene.add(points);
      buildModeButtons(meta);
      applyColors();
      resetView();
      status('');
    } catch (e) {
      console.error('Lidar3D', e);
      status(location.protocol === 'file:'
        ? 'Para ver la nube 3D abre el sitio con un servidor local (python -m http.server).'
        : 'No se pudo cargar la nube de puntos.');
    }
  }

  function hide() { running = false; }

  window.Lidar3D = { show, hide };
})();
