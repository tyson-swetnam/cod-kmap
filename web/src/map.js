import L from 'leaflet';
import 'leaflet.markercluster';
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

let map;
let cluster;
let _currentFeatures = [];

// ── Fit-to-data control ────────────────────────────────────────────
const FitControl = L.Control.extend({
  options: { position: 'topright' },
  onAdd() {
    const div = L.DomUtil.create('div', 'fit-control leaflet-bar');
    const a = L.DomUtil.create('a', '', div);
    a.href = '#';
    a.title = 'Zoom to data';
    a.innerHTML = '&#8982; Fit';
    L.DomEvent.on(a, 'click', (e) => {
      L.DomEvent.preventDefault(e);
      if (_currentFeatures.length === 0) return;
      const latlngs = _currentFeatures.map((f) => {
        const lat = f.geometry ? f.geometry.coordinates[1] : (f.lat ?? f.properties?.lat);
        const lng = f.geometry ? f.geometry.coordinates[0] : (f.lng ?? f.properties?.lng);
        return [lat, lng];
      }).filter(([a, b]) => a != null && b != null);
      if (latlngs.length) map.fitBounds(L.latLngBounds(latlngs), { padding: [30, 30] });
    });
    return div;
  },
});

// ── Legend control ─────────────────────────────────────────────────
const LegendControl = L.Control.extend({
  options: { position: 'bottomleft' },
  onAdd() {
    const div = L.DomUtil.create('div', 'legend-control');
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
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    // Populate async
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
});

export function initMap(container) {
  map = L.map(container, { preferCanvas: true }).setView([32, -85], 3);

  const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors',
  });
  const carto = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    { attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 20 },
  );

  carto.addTo(map);
  L.control.layers({ OpenStreetMap: osm, 'CARTO Positron': carto }).addTo(map);

  cluster = L.markerClusterGroup({ chunkedLoading: true });
  map.addLayer(cluster);

  new FitControl().addTo(map);
  new LegendControl().addTo(map);

  return map;
}

export function renderFacilities(features) {
  if (!cluster) return;
  _currentFeatures = features;
  cluster.clearLayers();
  for (const f of features) {
    const p = f.properties ?? f;
    const lat = f.geometry ? f.geometry.coordinates[1] : p.lat;
    const lng = f.geometry ? f.geometry.coordinates[0] : p.lng;
    if (lat == null || lng == null) continue;
    const color = TYPE_COLORS[p.type] || '#64748b';
    const marker = L.circleMarker([lat, lng], {
      radius: 6,
      color,
      weight: 1,
      fillColor: color,
      fillOpacity: 0.85,
    });
    marker.bindPopup(popupHtml(p), { maxWidth: 320 });
    cluster.addLayer(marker);
  }
}

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
