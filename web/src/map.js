import maplibregl from 'maplibre-gl';
import { fetchCSV } from './csv.js';
import { DATA_BASE as BASE } from './config.js';

export const TYPE_COLORS = {
  federal: '#2563eb',
  state: '#f97316',
  'local-gov': '#eab308',
  'university-marine-lab': '#16a34a',
  'university-institute': '#15803d',
  nonprofit: '#9333ea',
  foundation: '#d4a017',
  network: '#7c3aed',
  'international-federal': '#0d9488',
  'international-university': '#14b8a6',
  'international-nonprofit': '#5eead4',
  industry: '#475569',
  vessel: '#0ea5e9',
  observatory: '#0369a1',
  virtual: '#94a3b8',
};

// Build a MapLibre match expression for facility type → color
function typeColorExpr() {
  const expr = ['match', ['get', 'type']];
  for (const [k, v] of Object.entries(TYPE_COLORS)) {
    expr.push(k, v);
  }
  expr.push('#64748b'); // default
  return expr;
}

let map;
let _currentFeatures = [];
let _mapReady = false;
let _pendingFeatures = null;

const COASTLINE_URL =
  'https://raw.githubusercontent.com/martynafford/natural-earth-geojson/master/50m/physical/ne_50m_coastline.json';

export function initMap(container) {
  map = new maplibregl.Map({
    container,
    style: 'https://tiles.openfreemap.org/styles/positron',
    center: [-85, 32],
    zoom: 3,
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');

  // Custom fit-to-data control
  map.addControl(makeFitControl(), 'top-right');

  // Layer-toggle panel (replaces old single hull toggle)
  map.addControl(makeLayersControl(), 'top-left');

  // Custom legend control
  map.addControl(makeLegendControl(), 'bottom-left');

  map.on('load', () => {
    // ── Coastline source + layer ─────────────────────────────────
    map.addSource('coastline', {
      type: 'geojson',
      data: COASTLINE_URL,
    });
    map.addLayer({
      id: 'coastline-line',
      type: 'line',
      source: 'coastline',
      layout: {},
      paint: {
        'line-color': '#0d6e6e',
        'line-width': 0.8,
        'line-opacity': 0.6,
      },
    });

    // ── Observatory-region hull sources + layers ─────────────────
    map.addSource('hulls', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    });
    map.addLayer({
      id: 'hulls-fill',
      type: 'fill',
      source: 'hulls',
      maxzoom: 7,
      paint: {
        'fill-color': TYPE_COLORS['network'],
        'fill-opacity': 0.08,
      },
    });
    map.addLayer({
      id: 'hulls-outline',
      type: 'line',
      source: 'hulls',
      maxzoom: 7,
      paint: {
        'line-color': TYPE_COLORS['network'],
        'line-width': 1.2,
        'line-opacity': 0.3,
        'line-dasharray': [4, 3],
      },
    });

    // ── Facility points (NO clustering — every point is shown) ───
    map.addSource('facilities', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
    });

    map.addLayer({
      id: 'facility-points',
      type: 'circle',
      source: 'facilities',
      paint: {
        'circle-radius': [
          'interpolate', ['linear'], ['zoom'],
          2, 4,
          6, 6,
          10, 8,
        ],
        'circle-color': typeColorExpr(),
        'circle-stroke-width': 1.5,
        'circle-stroke-color': '#fff',
      },
    });

    // Acronym labels appear at higher zooms only
    map.addLayer({
      id: 'facility-labels',
      type: 'symbol',
      source: 'facilities',
      minzoom: 6,
      layout: {
        'text-field': ['coalesce', ['get', 'acronym'], ['get', 'name']],
        'text-size': 11,
        'text-offset': [0, 1.1],
        'text-anchor': 'top',
        'text-allow-overlap': false,
      },
      paint: {
        'text-color': '#0f172a',
        'text-halo-color': '#fff',
        'text-halo-width': 1.2,
      },
    });

    // Hover halo on points
    map.addLayer({
      id: 'facility-points-hover',
      type: 'circle',
      source: 'facilities',
      filter: ['==', ['get', 'id'], ''],
      paint: {
        'circle-radius': 10,
        'circle-color': 'transparent',
        'circle-stroke-width': 2.5,
        'circle-stroke-color': '#0d6e6e',
      },
    });

    // ── Point click → popup ─────────────────────────────────────
    map.on('click', 'facility-points', (e) => {
      const feat = e.features[0];
      const coords = feat.geometry.coordinates.slice();
      const p = feat.properties;
      // Properties from GeoJSON are stringified arrays — parse them
      for (const key of ['areas', 'networks', 'funders']) {
        if (typeof p[key] === 'string') {
          try { p[key] = JSON.parse(p[key]); } catch (_) { p[key] = []; }
        }
      }
      new maplibregl.Popup({ maxWidth: '320px' })
        .setLngLat(coords)
        .setHTML(popupHtml(p))
        .addTo(map);
    });

    // ── Hover state ──────────────────────────────────────────────
    map.on('mousemove', 'facility-points', (e) => {
      map.getCanvas().style.cursor = 'pointer';
      const id = e.features[0].properties.id || '';
      map.setFilter('facility-points-hover', ['==', ['get', 'id'], id]);
    });
    map.on('mouseleave', 'facility-points', () => {
      map.getCanvas().style.cursor = '';
      map.setFilter('facility-points-hover', ['==', ['get', 'id'], '']);
    });

    // Mark ready and flush any features that arrived before the sources existed
    _mapReady = true;
    const pending = _pendingFeatures ?? _currentFeatures;
    if (pending && pending.length) applyFeatures(pending);
  });

  return map;
}

// Re-layout the canvas (useful after sidebar drawer opens/closes on mobile)
export function resizeMap() {
  if (map) map.resize();
}

// ── renderFacilities ───────────────────────────────────────────────
export function renderFacilities(features) {
  _currentFeatures = features;
  if (!_mapReady) {
    // Stash until the map 'load' handler runs and registers sources
    _pendingFeatures = features;
    return;
  }
  applyFeatures(features);
}

function applyFeatures(features) {
  const src = map.getSource('facilities');
  if (!src) {
    // Sources not yet registered — try again shortly
    setTimeout(() => applyFeatures(features), 100);
    return;
  }
  const mappable = features.filter(
    (f) => f && f.geometry && Array.isArray(f.geometry.coordinates)
      && Number.isFinite(f.geometry.coordinates[0])
      && Number.isFinite(f.geometry.coordinates[1])
  );
  src.setData({ type: 'FeatureCollection', features: mappable });
  updateHulls(mappable);
}

function updateHulls(features) {
  // Group by networks[0] if present, else by country
  const groups = new Map();
  for (const f of features) {
    const p = f.properties ?? {};
    let nets = p.networks;
    if (typeof nets === 'string') { try { nets = JSON.parse(nets); } catch (_) { nets = null; } }
    const key = (Array.isArray(nets) && nets.length > 0) ? nets[0] : (p.country || 'unknown');
    if (!groups.has(key)) groups.set(key, []);
    const coords = f.geometry?.coordinates;
    if (coords) groups.get(key).push(coords);
  }

  const hullFeatures = [];
  for (const [, pts] of groups) {
    if (pts.length < 3) continue;
    const hull = convexHull(pts);
    if (hull.length < 3) continue;
    hullFeatures.push({
      type: 'Feature',
      geometry: {
        type: 'Polygon',
        coordinates: [[...hull, hull[0]]],
      },
      properties: {},
    });
  }

  const src = map.getSource('hulls');
  if (src) src.setData({ type: 'FeatureCollection', features: hullFeatures });
}

// ── Convex hull (monotone chain) ───────────────────────────────────
function convexHull(points) {
  const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [];
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0)
      lower.pop();
    lower.push(p);
  }
  const upper = [];
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i];
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0)
      upper.pop();
    upper.push(p);
  }
  upper.pop(); lower.pop();
  return lower.concat(upper);
}

// ── Popup HTML ─────────────────────────────────────────────────────
function popupHtml(p) {
  const color = TYPE_COLORS[p.type] || '#64748b';
  const nameHtml = p.url
    ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.name)}</a>`
    : esc(p.name);
  const areas = Array.isArray(p.areas) && p.areas.length
    ? p.areas.slice(0, 4).map(esc).join(', ') : null;
  const networks = Array.isArray(p.networks) && p.networks.length
    ? p.networks.slice(0, 3).map(esc).join(', ') : null;
  const funders = Array.isArray(p.funders) && p.funders.length
    ? p.funders.slice(0, 3).map(esc).join(', ') : null;

  return `<div class="popup">
    <div class="popup-name">${nameHtml}${p.acronym ? ` <span class="popup-acr">(${esc(p.acronym)})</span>` : ''}</div>
    <div class="popup-meta">
      <span class="type-badge" style="background:${color}">${esc(p.type || 'unknown')}</span>
      ${p.country ? `<span class="popup-country">${esc(p.country)}</span>` : ''}
    </div>
    ${p.parent_org ? `<div class="popup-row"><em>Org:</em> ${esc(p.parent_org)}</div>` : ''}
    ${areas ? `<div class="popup-row"><em>Research:</em> ${areas}</div>` : ''}
    ${networks ? `<div class="popup-row"><em>Networks:</em> ${networks}</div>` : ''}
    ${funders ? `<div class="popup-row"><em>Funders:</em> ${funders}</div>` : ''}
    ${p.url ? `<a class="popup-source" href="${esc(p.url)}" target="_blank" rel="noopener">View source</a>` : ''}
  </div>`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ── Custom controls ────────────────────────────────────────────────
function makeFitControl() {
  return {
    onAdd(m) {
      const div = document.createElement('div');
      div.className = 'maplibregl-ctrl fit-control';
      div.innerHTML = '<a href="#" title="Zoom to data">&#8982; Fit</a>';
      div.querySelector('a').addEventListener('click', (e) => {
        e.preventDefault();
        if (_currentFeatures.length === 0) return;
        const coords = _currentFeatures
          .map((f) => f.geometry?.coordinates)
          .filter(Boolean);
        if (!coords.length) return;
        const lngs = coords.map((c) => c[0]);
        const lats = coords.map((c) => c[1]);
        m.fitBounds(
          [[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]],
          { padding: 40 }
        );
      });
      return div;
    },
    onRemove() {},
  };
}

// Layer toggle panel — collapsible card with checkboxes for each layer group.
// Each entry maps to one or more MapLibre layer IDs; toggling sets visibility.
const LAYER_GROUPS = [
  { key: 'coastline', label: 'Coastline',           layers: ['coastline-line'],          on: true,  hint: 'all zooms' },
  { key: 'regions',   label: 'Observatory regions', layers: ['hulls-fill', 'hulls-outline'], on: true, hint: 'zoom 0-7' },
  { key: 'points',    label: 'Facility points',     layers: ['facility-points', 'facility-points-hover'], on: true, hint: 'all zooms' },
  { key: 'labels',    label: 'Facility labels',     layers: ['facility-labels'],         on: true,  hint: 'zoom 6+' },
];

function makeLayersControl() {
  return {
    onAdd(m) {
      const div = document.createElement('div');
      div.className = 'maplibregl-ctrl layers-control';
      div.innerHTML = `
        <div class="layers-header">
          <span>Layers</span>
          <span class="layers-toggle">&#9650;</span>
        </div>
        <div class="layers-body">
          ${LAYER_GROUPS.map((g) => `
            <label class="layers-row">
              <input type="checkbox" data-key="${g.key}" ${g.on ? 'checked' : ''} />
              <span class="layers-label">${esc(g.label)}</span>
              <span class="layers-hint">${esc(g.hint)}</span>
            </label>
          `).join('')}
        </div>
      `;
      div.querySelector('.layers-header').addEventListener('click', () => {
        div.classList.toggle('collapsed');
        div.querySelector('.layers-toggle').innerHTML =
          div.classList.contains('collapsed') ? '&#9660;' : '&#9650;';
      });
      div.addEventListener('click', (e) => e.stopPropagation());
      div.addEventListener('wheel', (e) => e.stopPropagation());
      div.querySelectorAll('input[type=checkbox]').forEach((cb) => {
        cb.addEventListener('change', () => {
          const grp = LAYER_GROUPS.find((g) => g.key === cb.dataset.key);
          if (!grp) return;
          const vis = cb.checked ? 'visible' : 'none';
          for (const id of grp.layers) {
            if (m.getLayer(id)) m.setLayoutProperty(id, 'visibility', vis);
          }
        });
      });
      return div;
    },
    onRemove() {},
  };
}

function makeLegendControl() {
  return {
    onAdd() {
      const div = document.createElement('div');
      div.className = 'maplibregl-ctrl legend-control';
      div.innerHTML = `
        <div class="legend-header">
          <span>Facility type</span>
          <span class="legend-toggle">&#9650;</span>
        </div>
        <div class="legend-body" id="legend-body">Loading…</div>
      `;
      div.querySelector('.legend-header').addEventListener('click', () => {
        div.classList.toggle('collapsed');
        div.querySelector('.legend-toggle').innerHTML =
          div.classList.contains('collapsed') ? '&#9660;' : '&#9650;';
      });
      div.addEventListener('click', (e) => e.stopPropagation());
      div.addEventListener('wheel', (e) => e.stopPropagation());

      fetchCSV(`${BASE}vocab/facility_types.csv`).then((rows) => {
        const body = div.querySelector('#legend-body');
        body.innerHTML = rows.map((r) => {
          const color = TYPE_COLORS[r.slug] || '#64748b';
          return `<div class="legend-row">
            <span class="legend-chip" style="background:${color}"></span>
            <span>${esc(r.label)}</span>
          </div>`;
        }).join('');
      }).catch(() => {
        const body = div.querySelector('#legend-body');
        body.innerHTML = Object.entries(TYPE_COLORS).map(([slug, color]) =>
          `<div class="legend-row"><span class="legend-chip" style="background:${color}"></span><span>${slug}</span></div>`
        ).join('');
      });
      return div;
    },
    onRemove() {},
  };
}
