// overlays.js — Map overlay layer manager.
//
// Loads web/public/overlays/manifest.json on startup and exposes:
//   - initOverlays(map, sidebarContainer, onChange)
//       builds the sidebar section and wires fetch-on-demand toggles
//   - activeOverlays() → Array<{id, label, color}> for the legend control
//
// Overlays are fetched lazily the first time they are toggled on. Each
// overlay registers a single GeoJSON source and two MapLibre layers (fill +
// outline) and a label layer. Polygons sit under the clustered facility
// points so the points stay on top.

import maplibregl from 'maplibre-gl';
import { DATA_BASE } from './config.js';

const MANIFEST_URL = `${DATA_BASE}overlays/manifest.json`;

let _map = null;
let _manifest = {};
let _active = new Set();
let _onChange = () => {};

// Track which overlays have had their data fetched so we don't refetch.
const _loaded = new Set();

export async function initOverlays(map, container, onChange) {
  _map = map;
  _onChange = onChange || (() => {});

  try {
    const res = await fetch(MANIFEST_URL);
    _manifest = await res.json();
  } catch (e) {
    console.warn('overlays: manifest failed to load', e);
    return;
  }

  // Group overlays by category so the sidebar can render them as sections.
  const byCat = {};
  for (const [id, meta] of Object.entries(_manifest)) {
    (byCat[meta.category || 'other'] ??= []).push({ id, ...meta });
  }

  const CATEGORY_LABELS = {
    coastal: 'Coastal boundaries',
    marine:  'Marine protected areas',
    context: 'Context layers',
  };

  const sec = document.createElement('div');
  sec.className = 'facet-section overlay-section';
  sec.innerHTML = `
    <div class="facet-header">
      <h2>Map overlays</h2>
      <span class="facet-toggle">&#9650;</span>
    </div>
    <div class="facet-body overlay-body"></div>
  `;
  sec.querySelector('.facet-header').addEventListener('click', () => {
    sec.classList.toggle('collapsed');
    sec.querySelector('.facet-toggle').innerHTML =
      sec.classList.contains('collapsed') ? '&#9660;' : '&#9650;';
  });

  const body = sec.querySelector('.overlay-body');
  const orderedCats = ['coastal', 'marine', 'context'];
  for (const cat of orderedCats) {
    if (!byCat[cat]) continue;
    const group = document.createElement('div');
    group.className = 'overlay-group';
    const label = CATEGORY_LABELS[cat] || cat;
    group.innerHTML = `<div class="overlay-group-label">${label}</div>`;
    for (const o of byCat[cat]) {
      const row = document.createElement('label');
      row.className = 'overlay-row';
      row.innerHTML = `
        <input type="checkbox" data-overlay="${o.id}" />
        <span class="overlay-swatch" style="background:${o.color}"></span>
        <span class="overlay-label">${o.label}</span>
      `;
      group.appendChild(row);
    }
    body.appendChild(group);
  }

  container.appendChild(sec);

  body.addEventListener('change', async (ev) => {
    const cb = ev.target;
    if (!(cb instanceof HTMLInputElement)) return;
    const id = cb.dataset.overlay;
    if (!id) return;
    if (cb.checked) {
      await showOverlay(id);
    } else {
      hideOverlay(id);
    }
    _onChange();
  });
}

function whenStyleReady() {
  return new Promise((resolve) => {
    if (_map.isStyleLoaded()) return resolve();
    _map.once('load', resolve);
  });
}

// The facility point/cluster layers are added in map.js's 'load' handler.
// Wait until they actually exist before we reference them for z-ordering —
// isStyleLoaded() can return true before user 'load' handlers have run.
async function whenFacilityLayersReady() {
  const want = ['clusters', 'cluster-count', 'unclustered-point', 'unclustered-point-hover'];
  for (let i = 0; i < 50; i++) {
    if (want.every((id) => _map.getLayer(id))) return;
    await new Promise((r) => setTimeout(r, 50));
  }
}

// Force the facility layers to sit above any overlays. MapLibre's moveLayer
// (with no beforeId) pushes a layer to the top of the stack — do this for
// every point/cluster layer so the ordering is correct regardless of how the
// overlay was inserted.
function raiseFacilityLayers() {
  for (const id of ['clusters', 'cluster-count', 'unclustered-point', 'unclustered-point-hover']) {
    if (_map.getLayer(id)) _map.moveLayer(id);
  }
}

async function ensureLoaded(id) {
  if (_loaded.has(id)) return;
  await whenStyleReady();
  await whenFacilityLayersReady();
  const meta = _manifest[id];
  const url = `${DATA_BASE}overlays/${id}.geojson`;
  _map.addSource(`ov-${id}`, { type: 'geojson', data: url });

  // Insert beneath the facility cluster layers so points stay on top.
  const beforeLayer = _map.getLayer('clusters') ? 'clusters' : undefined;

  _map.addLayer({
    id: `ov-${id}-fill`,
    type: 'fill',
    source: `ov-${id}`,
    layout: { visibility: 'none' },
    paint: {
      'fill-color': meta.color,
      'fill-opacity': 0.18,
    },
  }, beforeLayer);

  _map.addLayer({
    id: `ov-${id}-outline`,
    type: 'line',
    source: `ov-${id}`,
    layout: { visibility: 'none' },
    paint: {
      'line-color': meta.color,
      'line-width': 1.25,
      'line-opacity': 0.75,
    },
  }, beforeLayer);

  // Belt-and-braces: force the facility/cluster layers back to the top in
  // case any ordering slipped.
  raiseFacilityLayers();

  // Click handler for polygon popups
  _map.on('click', `ov-${id}-fill`, (e) => {
    const f = e.features?.[0];
    if (!f) return;
    new maplibregl.Popup({ maxWidth: '280px' })
      .setLngLat([e.lngLat.lng, e.lngLat.lat])
      .setHTML(overlayPopup(id, f.properties || {}))
      .addTo(_map);
  });

  _map.on('mouseenter', `ov-${id}-fill`, () => { _map.getCanvas().style.cursor = 'pointer'; });
  _map.on('mouseleave', `ov-${id}-fill`, () => { _map.getCanvas().style.cursor = ''; });

  _loaded.add(id);
}

async function showOverlay(id) {
  await ensureLoaded(id);
  _map.setLayoutProperty(`ov-${id}-fill`, 'visibility', 'visible');
  _map.setLayoutProperty(`ov-${id}-outline`, 'visibility', 'visible');
  // Re-raise after any visibility flip — clicks can land while the map is
  // still reconciling layer order.
  raiseFacilityLayers();
  _active.add(id);
}

function hideOverlay(id) {
  if (!_loaded.has(id)) { _active.delete(id); return; }
  _map.setLayoutProperty(`ov-${id}-fill`, 'visibility', 'none');
  _map.setLayoutProperty(`ov-${id}-outline`, 'visibility', 'none');
  _active.delete(id);
}

export function activeOverlays() {
  return Array.from(_active).map((id) => ({
    id,
    label: _manifest[id]?.label || id,
    color: _manifest[id]?.color || '#64748b',
  }));
}

function overlayPopup(id, p) {
  const meta = _manifest[id] || {};
  const rows = [];
  if (p.name) rows.push(`<div class="popup-name">${esc(p.name)}</div>`);
  rows.push(`<div class="popup-meta"><span class="type-badge" style="background:${meta.color}">${esc(meta.label || id)}</span></div>`);
  if (p.year) rows.push(`<div class="popup-row"><em>Designated:</em> ${esc(p.year)}</div>`);
  if (p.epa_region) rows.push(`<div class="popup-row"><em>EPA Region:</em> ${esc(p.epa_region)}</div>`);
  if (p.area_sqmi) rows.push(`<div class="popup-row"><em>Area:</em> ${esc(Number(p.area_sqmi).toLocaleString())} sq mi</div>`);
  if (p.state) rows.push(`<div class="popup-row"><em>State:</em> ${esc(p.state)}</div>`);
  if (p.management) rows.push(`<div class="popup-row"><em>Management:</em> ${esc(p.management)}</div>`);
  if (p.protection_level) rows.push(`<div class="popup-row"><em>Protection:</em> ${esc(p.protection_level)}</div>`);
  if (p.domain_id) rows.push(`<div class="popup-row"><em>NEON domain:</em> ${esc(p.domain_id)}</div>`);
  return `<div class="popup">${rows.join('')}</div>`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
