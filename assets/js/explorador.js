(function () {
  'use strict';

  const { loadManifest, fmt, dataUrl } = window.FNDR;

  const GROUPS = [
    ['color', 'Color'],
    ['indices', 'Índices espectrales'],
    ['termico', 'Temperatura'],
    ['relieve', 'Relieve'],
    ['3d', 'Nube de puntos'],
  ];
  const MISSION_ORDER = ['multi', 'termico', 'lidar'];
  const MISSION_BASE = { multi: ['rgb'], termico: ['rgb_t', 'rgb'], lidar: ['dom', 'rgb'] };
  const DIGITS = { thermal: 1, dem: 1 };
  const QUERY_ORDER = ['ndvi', 'ndwi', 'ndci', 'ndsi', 'thermal', 'dem'];

  const state = {
    m: null,
    flightId: null,
    mission: 'multi',
    layer: 'rgb',
    opacity: 1,
    lastLayer: {},
  };

  let map;
  let baseLayers = {};
  let primaryGroup = null;
  let comunasLayer = null;
  const gridCache = new Map();

  const $ = (id) => document.getElementById(id);
  const flight = (id) => state.m.flights.find((f) => f.id === (id || state.flightId));
  const meta = (kind) => state.m.layers[kind] || {};
  const isScalar = (kind) => !!meta(kind).legend_gradient;
  const missionOf = (kind) => meta(kind).mission || 'multi';
  const missionKinds = (f, mission) => state.m.layer_order.filter((k) => f.layers[k] && missionOf(k) === mission);
  const flightMissions = (f) => MISSION_ORDER.filter((ms) => missionKinds(f, ms).length);
  const missionMeta = (ms) => (state.m.missions && state.m.missions[ms]) || { label: ms, icon: 'bi-layers' };

  function layerRange(f, kind) {
    const l = (f && f.layers[kind]) || {};
    const M = meta(kind);
    return {
      vmin: l.vmin != null ? l.vmin : M.vmin,
      vmax: l.vmax != null ? l.vmax : M.vmax,
      unit: l.unit || M.unit || '',
    };
  }

  /* ---------------- Map ---------------- */

  function initMap() {
    map = L.map('map', { zoomControl: false, maxZoom: 22, wheelPxPerZoomLevel: 140, preferCanvas: false });
    L.control.zoom({ position: 'topright' }).addTo(map);
    L.control.scale({ imperial: false, position: 'topright' }).addTo(map);

    baseLayers = {
      sat: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Imágenes © Esri', maxZoom: 22, maxNativeZoom: 19,
      }),
      topo: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
        attribution: '© Esri', maxZoom: 22, maxNativeZoom: 19,
      }),
    };
    baseLayers.topo.addTo(map);

    [['primary', 410], ['comunas', 430]].forEach(([name, z]) => {
      map.createPane(name).style.zIndex = z;
    });
    map.getPane('comunas').style.pointerEvents = 'none';

    map.on('click', onMapClick);
    map.setView([-32.83, -71.23], 12);
  }

  function setBasemap(key) {
    Object.entries(baseLayers).forEach(([k, l]) => (k === key ? l.addTo(map) : map.removeLayer(l)));
    document.querySelectorAll('#basemapSeg button').forEach((b) => b.classList.toggle('active', b.dataset.base === key));
  }

  function baseKind(f, kind) {
    if (!isScalar(kind)) return null;
    return (MISSION_BASE[missionOf(kind)] || []).find((k) => f.layers[k]) || null;
  }

  const EMPTY_TILE = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
  const indexCache = new WeakMap();

  function tileIndex(tiles) {
    if (!indexCache.has(tiles)) {
      const idx = {};
      Object.entries(tiles.index || {}).forEach(([z, [x0, y0, w, h, b64]]) => {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        idx[z] = { x0, y0, w, h, bytes };
      });
      indexCache.set(tiles, idx);
    }
    return indexCache.get(tiles);
  }

  const IndexedTiles = L.TileLayer.extend({
    getTileUrl(c) {
      const e = this.options.tileIndex[c.z];
      const x = c.x - (e ? e.x0 : 0);
      const y = c.y - (e ? e.y0 : 0);
      if (!e || x < 0 || y < 0 || x >= e.w || y >= e.h) return EMPTY_TILE;
      const i = y * e.w + x;
      if (!(e.bytes[i >> 3] & (128 >> (i & 7)))) return EMPTY_TILE;
      return L.TileLayer.prototype.getTileUrl.call(this, c);
    },
  });

  function tileLayer(lay, opts) {
    const t = lay.tiles;
    return new IndexedTiles(dataUrl(t.url), {
      pane: 'primary',
      bounds: L.latLngBounds(lay.bounds),
      minNativeZoom: t.minzoom,
      maxNativeZoom: t.maxzoom,
      maxZoom: 22,
      tileIndex: tileIndex(t),
      errorTileUrl: EMPTY_TILE,
      keepBuffer: 3,
      ...opts,
    });
  }

  function renderPrimary() {
    if (primaryGroup) map.removeLayer(primaryGroup);
    primaryGroup = L.layerGroup();
    const f = flight();
    const kind = state.layer;
    const base = baseKind(f, kind);
    if (base && f.layers[base].tiles) primaryGroup.addLayer(tileLayer(f.layers[base], {}));
    const lay = f.layers[kind];
    if (lay && lay.tiles) {
      primaryGroup.addLayer(tileLayer(lay, { opacity: isScalar(kind) ? state.opacity : 1, className: 'ov-scalar' }));
    }
    primaryGroup.addTo(map);
  }

  function setOpacity(v) {
    state.opacity = v;
    $('opacityVal').textContent = Math.round(v * 100) + '%';
    if (isScalar(state.layer)) primaryGroup.eachLayer((l) => { if (l.options.className === 'ov-scalar') l.setOpacity(v); });
    updateNote();
  }

  function fitFlight(f) {
    if (!f) return;
    const lay = f.layers[state.layer];
    const bounds = (lay && lay.bounds) || f.bounds;
    if (!bounds) return;
    const panelW = window.innerWidth > 900 ? $('panel').offsetWidth + 40 : 20;
    map.fitBounds(bounds, { paddingTopLeft: [panelW, 60], paddingBottomRight: [90, 70] });
  }

  async function loadComunas() {
    if (!state.m.comunas) return;
    try {
      const r = await fetch(dataUrl(state.m.comunas));
      const gj = await r.json();
      comunasLayer = L.geoJSON(gj, {
        pane: 'comunas',
        interactive: false,
        style: (ft) => ({
          color: ft.properties.con_vuelo ? '#f4d35e' : '#ffffff',
          weight: ft.properties.con_vuelo ? 1.6 : 0.9,
          opacity: ft.properties.con_vuelo ? 0.85 : 0.5,
          fillOpacity: 0,
          dashArray: '4 4',
        }),
      });
      if ($('btnComunas').classList.contains('active')) comunasLayer.addTo(map);
    } catch (e) {
      $('btnComunas').hidden = true;
    }
  }

  function toggleComunas() {
    const btn = $('btnComunas');
    const on = !btn.classList.contains('active');
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', String(on));
    if (comunasLayer) (on ? comunasLayer.addTo(map) : map.removeLayer(comunasLayer));
  }

  /* ---------------- Value query ---------------- */

  function loadGrid(f, kind) {
    const g = f.layers[kind] && f.layers[kind].grid;
    if (!g) return Promise.resolve(null);
    const key = f.id + '/' + kind;
    if (!gridCache.has(key)) {
      gridCache.set(key, fetch(dataUrl(g.path))
        .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject()))
        .then((buf) => ({ meta: g, data: new Uint16Array(buf) }))
        .catch(() => null));
    }
    return gridCache.get(key);
  }

  function sampleGrid(grid, latlng) {
    if (!grid) return null;
    const g = grid.meta;
    const p = L.CRS.EPSG3857.project(latlng);
    const sw = L.CRS.EPSG3857.project(L.latLng(g.bounds[0]));
    const ne = L.CRS.EPSG3857.project(L.latLng(g.bounds[1]));
    const col = Math.floor(((p.x - sw.x) / (ne.x - sw.x)) * g.width);
    const row = Math.floor(((ne.y - p.y) / (ne.y - sw.y)) * g.height);
    if (col < 0 || row < 0 || col >= g.width || row >= g.height) return null;
    const q = grid.data[row * g.width + col];
    return q === 65535 ? null : g.offset + q * g.scale;
  }

  function fmtVal(kind, v, f) {
    if (v == null) return '—';
    const u = layerRange(f || flight(), kind).unit;
    const d = DIGITS[kind] != null ? DIGITS[kind] : 3;
    return fmt(v, d) + (u ? ' ' + u : '');
  }

  async function onMapClick(e) {
    if (state.layer === 'lidar') return;
    const f = flight();
    const active = state.layer;
    const kinds = QUERY_ORDER.filter((k) => f.layers[k] && f.layers[k].grid && missionOf(k) === state.mission);
    if (!kinds.length) return;
    const popup = L.popup({ maxWidth: 260 }).setLatLng(e.latlng).setContent('<div class="value-popup">Consultando…</div>').openOn(map);
    const values = {};
    await Promise.all(kinds.map(async (k) => { values[k] = sampleGrid(await loadGrid(f, k), e.latlng); }));
    const empty = kinds.every((k) => values[k] == null);
    const offline = empty && location.protocol === 'file:';
    if (empty && !offline) {
      popup.setContent(`<div class="value-popup"><small>${f.site_name}</small><br>Fuera del área con datos.</div>`);
      return;
    }
    const main = isScalar(active) && kinds.includes(active) ? active : kinds[0];
    const rows = kinds.filter((k) => k !== main)
      .map((k) => `<tr><td>${meta(k).label}</td><td style="text-align:right;padding-left:12px"><b>${fmtVal(k, values[k], f)}</b></td></tr>`).join('');
    popup.setContent(`<div class="value-popup">
      <small>${f.site_name} · ${f.date_label}</small><br>
      <b>${meta(main).label}</b><div class="v">${fmtVal(main, values[main], f)}</div>
      ${rows ? `<table style="font-size:12.5px;margin-top:6px">${rows}</table>` : ''}
      ${offline ? '<small>Para consultar valores abre el sitio con un servidor local.</small><br>' : ''}
      <small>${e.latlng.lat.toFixed(6)}, ${e.latlng.lng.toFixed(6)}</small></div>`);
  }

  /* ---------------- Panel UI ---------------- */

  function buildFlightList() {
    const box = $('flightList');
    box.innerHTML = '';
    state.m.flights.slice().reverse().forEach((f) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'flight-card';
      b.dataset.id = f.id;
      const badges = flightMissions(f)
        .map((ms) => `<span class="mission-badge" title="${missionMeta(ms).label}"><i class="bi ${missionMeta(ms).icon}"></i> ${missionMeta(ms).label}</span>`)
        .join('');
      b.innerHTML = `<img src="${dataUrl(f.thumb)}" alt="" loading="lazy">
        <div><div class="t">${f.site_name}</div>
        <div class="s"><i class="bi bi-calendar3"></i> ${f.date_label}</div>
        <div class="s"><i class="bi bi-geo-alt"></i> ${f.comuna || ''} · ${fmt(f.area_ha, 0)} ha</div>
        <div class="mission-badges">${badges}</div></div>`;
      b.addEventListener('click', () => selectFlight(f.id, true));
      box.appendChild(b);
    });
  }

  function buildMissionSeg() {
    const f = flight();
    const available = flightMissions(f);
    $('missionSeg').innerHTML = '';
    const anywhere = new Set(state.m.flights.flatMap(flightMissions));
    MISSION_ORDER.filter((ms) => anywhere.has(ms)).forEach((ms) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = ms === state.mission ? 'active' : '';
      b.disabled = !available.includes(ms);
      b.title = b.disabled ? 'Sin vuelo de este tipo en el sector' : missionMeta(ms).label;
      b.innerHTML = `<i class="bi ${missionMeta(ms).icon}"></i> ${missionMeta(ms).label}`;
      b.addEventListener('click', () => selectMission(ms));
      $('missionSeg').appendChild(b);
    });
  }

  function buildLayerPills() {
    const box = $('layerGroups');
    box.innerHTML = '';
    const kinds = missionKinds(flight(), state.mission);
    GROUPS.forEach(([group, label]) => {
      const inGroup = kinds.filter((k) => meta(k).group === group);
      if (!inGroup.length) return;
      const wrap = document.createElement('div');
      wrap.className = 'layer-group';
      wrap.innerHTML = `<div class="layer-group__label">${label}</div><div class="pills"></div>`;
      const pills = wrap.querySelector('.pills');
      inGroup.forEach((k) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'pill' + (k === state.layer ? ' active' : '');
        b.textContent = meta(k).label;
        b.addEventListener('click', () => selectLayer(k));
        pills.appendChild(b);
      });
      box.appendChild(wrap);
    });
  }

  function updateDescription() {
    const M = meta(state.layer);
    $('layerDescLabel').textContent = M.label || state.layer;
    let text = M.description || '';
    const lid = flight().layers.lidar;
    if (state.layer === 'lidar' && lid) {
      text += ` Se muestran ${fmt(lid.count, 0)} de ${fmt(lid.total_points, 0)} puntos (submuestreo para la web).`;
    }
    $('layerDescText').textContent = text;
  }

  function updateStats() {
    const lay = flight().layers[state.layer];
    const st = lay && lay.stats;
    $('blockStats').hidden = !st;
    if (!st) return;
    const k = state.layer;
    const items = [
      ['Media', fmtVal(k, st.mean)], ['Mediana', fmtVal(k, st.median)],
      ['Desv. est.', fmt(st.std, DIGITS[k] || 3)],
      ['P2', fmtVal(k, st.p2)], ['P98', fmtVal(k, st.p98)], ['Área', fmt(st.area_ha, 1) + ' ha'],
    ];
    $('statsGrid').innerHTML = items.map(([l, v]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join('');
  }

  function updateColorbar() {
    const M = meta(state.layer);
    const show = isScalar(state.layer);
    $('colorbarBox').hidden = !show;
    if (!show) return;
    const R = layerRange(flight(), state.layer);
    const d = DIGITS[state.layer] != null ? DIGITS[state.layer] : 2;
    $('cbTitle').textContent = M.label;
    $('cbGradient').style.background = M.legend_gradient;
    $('cbMax').textContent = fmt(R.vmax, d);
    $('cbMin').textContent = fmt(R.vmin, d);
    $('cbUnit').textContent = R.unit;
  }

  function updateOpacityDock() {
    const f = flight();
    const base = baseKind(f, state.layer);
    $('opacityDock').hidden = !isScalar(state.layer);
    document.querySelector('#opacityDock .dock-sub').textContent = base ? `capa sobre ${meta(base).label}` : 'capa sobre mapa base';
  }

  function updateNote() {
    const f = flight();
    const ms = missionMeta(state.mission).label;
    let t = `${ms} · ${meta(state.layer).label} · ${f.site_name} · ${f.date_label}`;
    if (isScalar(state.layer)) t += ` · opacidad ${Math.round(state.opacity * 100)}%`;
    if (state.layer === 'lidar') t = `LiDAR 3D · ${f.site_name} · arrastra para rotar, rueda para zoom, clic derecho para desplazar`;
    $('mapNote').textContent = t;
  }

  function writeHash() {
    const p = new URLSearchParams({ vuelo: state.flightId, capa: state.layer });
    history.replaceState(null, '', '#' + p.toString());
  }

  function readHash() {
    const p = new URLSearchParams(location.hash.replace(/^#/, ''));
    return { vuelo: p.get('vuelo'), capa: p.get('capa') };
  }

  /* ---------------- Selection ---------------- */

  function pickLayer(f, mission) {
    const kinds = missionKinds(f, mission);
    const last = state.lastLayer[mission];
    return kinds.includes(last) ? last : kinds[0];
  }

  function selectFlight(id, fit, keepLayer) {
    state.flightId = id;
    const f = flight();
    const missions = flightMissions(f);
    if (!keepLayer) {
      state.mission = missions.includes('multi') ? 'multi' : missions[0] || 'multi';
      state.layer = missionKinds(f, state.mission)[0];
    }
    if (!missions.includes(state.mission)) state.mission = missions[0] || 'multi';
    if (!f.layers[state.layer] || missionOf(state.layer) !== state.mission) state.layer = pickLayer(f, state.mission);
    document.querySelectorAll('.flight-card').forEach((b) => b.classList.toggle('active', b.dataset.id === id));
    applyLayer();
    if (fit) fitFlight(f);
  }

  function selectMission(mission) {
    if (mission === state.mission) return;
    state.mission = mission;
    state.layer = pickLayer(flight(), mission);
    applyLayer();
    fitFlight(flight());
  }

  function selectLayer(kind) {
    state.layer = kind;
    state.mission = missionOf(kind);
    applyLayer();
  }

  function applyLayer() {
    state.lastLayer[state.mission] = state.layer;
    buildMissionSeg();
    buildLayerPills();
    updateDescription();
    updateStats();
    updateColorbar();
    renderPrimary();
    updateOpacityDock();
    updateNote();
    writeHash();
    const lidar = state.layer === 'lidar';
    $('las3dView').hidden = !lidar;
    $('legendRailRight').hidden = lidar;
    document.body.classList.toggle('is-3d', lidar);
    if (lidar) window.Lidar3D.show(flight(), dataUrl(flight().layers.lidar.path));
    else window.Lidar3D.hide();
  }

  /* ---------------- Boot ---------------- */

  function bindUi() {
    $('opacity').addEventListener('input', (e) => setOpacity(+e.target.value / 100));
    document.querySelectorAll('#basemapSeg button').forEach((b) => b.addEventListener('click', () => setBasemap(b.dataset.base)));
    $('btnComunas').addEventListener('click', toggleComunas);
    $('panelCollapse').addEventListener('click', () => {
      $('panel').classList.toggle('collapsed');
      $('app').classList.toggle('panel-collapsed', $('panel').classList.contains('collapsed'));
    });
    window.addEventListener('hashchange', () => {
      const h = readHash();
      if (h.vuelo && h.vuelo !== state.flightId && flight(h.vuelo)) selectFlight(h.vuelo, true);
      if (h.capa && h.capa !== state.layer && flight().layers[h.capa]) selectLayer(h.capa);
    });
    $('tourBtn').addEventListener('click', startTour);
  }

  function startTour() {
    if (window.innerWidth <= 900 && $('panel').classList.contains('collapsed')) $('panelCollapse').click();
    window.Tour.start([
      { el: '#blockFlights', title: 'Elige un sector', text: 'Cada tarjeta es una campaña de dron en un sector. Las etiquetas indican qué tipos de vuelo tiene: multiespectral o LiDAR.' },
      { el: '#blockMission', title: 'Tipo de vuelo', text: 'En un mismo sector puede haber vuelos multiespectrales y LiDAR. Elige cuál quieres ver.' },
      { el: '#blockLayers', title: 'Cambia de capa', text: 'Cada tipo de vuelo tiene sus capas: RGB e índices espectrales, temperatura, o relieve y nube de puntos LiDAR en 3D.' },
      { el: '#legendRailRight', title: 'Transparencia', text: 'Con un índice activo, baja la opacidad para ver la foto RGB debajo y ubicarte mejor en el terreno.', before: () => { if (!isScalar(state.layer) && flight().layers.ndvi) selectLayer('ndvi'); } },
      { el: '#map', title: 'Consulta valores', text: 'Haz clic en cualquier punto del vuelo para ver el valor de cada capa en ese lugar.' },
      { el: '#mapTools', title: 'Mapa base', text: 'Cambia entre mapa satelital y topográfico, y muestra u oculta los límites comunales.' },
    ]);
    try { localStorage.setItem('fndr_tour_seen', '1'); } catch (e) { /* noop */ }
  }

  async function boot() {
    initMap();
    bindUi();
    try {
      state.m = await loadManifest();
    } catch (e) {
      $('loadHint').hidden = false;
      $('loadHint').innerHTML = 'No se encontraron datos. Ejecuta <code>python scripts/preprocess.py</code> para generar <code>data_web/</code>.';
      $('mapNote').textContent = 'Datos no disponibles';
      return;
    }
    if (!state.m.flights.length) return;
    buildFlightList();
    const h = readHash();
    const start = flight(h.vuelo) ? h.vuelo : state.m.flights[state.m.flights.length - 1].id;
    if (h.capa && flight(start).layers[h.capa]) {
      state.layer = h.capa;
      state.mission = missionOf(h.capa);
    }
    selectFlight(start, true, !!(h.capa && flight(start).layers[h.capa]));
    loadComunas();
    setTimeout(() => map.invalidateSize(), 200);
    let seen = false;
    try { seen = !!localStorage.getItem('fndr_tour_seen'); } catch (e) { /* noop */ }
    if (!seen) setTimeout(startTour, 900);
  }

  window.addEventListener('load', boot);
})();
