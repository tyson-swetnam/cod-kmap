import L from 'leaflet';
import 'leaflet.markercluster';

const TYPE_COLORS = {
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

  osm.addTo(map);
  L.control.layers({ OpenStreetMap: osm, 'CARTO Positron': carto }).addTo(map);

  cluster = L.markerClusterGroup({ chunkedLoading: true });
  map.addLayer(cluster);
  return map;
}

export function renderFacilities(features) {
  if (!cluster) return;
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
    marker.bindPopup(popupHtml(p));
    cluster.addLayer(marker);
  }
}

function popupHtml(p) {
  const areas = (p.areas || []).slice(0, 3).join(', ');
  const funders = (p.funders || []).slice(0, 3).join(', ');
  const title = p.url ? `<a href="${p.url}" target="_blank" rel="noopener">${escape(p.name)}</a>` : escape(p.name);
  return `
    <div class="popup">
      <strong>${title}</strong>
      ${p.acronym ? `<span class="acr"> (${escape(p.acronym)})</span>` : ''}
      <div class="meta">${escape(p.type || '')} &middot; ${escape(p.country || '')}</div>
      ${p.parent_org ? `<div class="parent">Parent: ${escape(p.parent_org)}</div>` : ''}
      ${areas ? `<div><em>Research:</em> ${escape(areas)}</div>` : ''}
      ${funders ? `<div><em>Funders:</em> ${escape(funders)}</div>` : ''}
    </div>
  `;
}

function escape(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
