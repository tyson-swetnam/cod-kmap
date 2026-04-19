// stats.js — Stats view: SVG bar charts for facilities by type, country, area

import { TYPE_COLORS } from '../map.js';

let _container = null;

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function normalizeFeature(f) {
  return f.properties ?? f;
}

function countBy(features, keyFn) {
  const map = {};
  for (const f of features) {
    const k = keyFn(normalizeFeature(f));
    if (k) map[k] = (map[k] || 0) + 1;
  }
  return Object.entries(map).sort((a, b) => b[1] - a[1]);
}

function barChart(entries, title, colorFn, maxBars) {
  const rows = entries.slice(0, maxBars);
  if (!rows.length) return `<div class="card"><h3>${esc(title)}</h3><p class="no-data">No data</p></div>`;

  const W = 240, BAR_H = 14, GAP = 4, LABEL_W = 100, PAD = 8;
  const maxVal = Math.max(...rows.map((r) => r[1]));
  const barW = W - LABEL_W - PAD;
  const H = rows.length * (BAR_H + GAP) + PAD * 2;

  const bars = rows.map(([label, count], i) => {
    const y = PAD + i * (BAR_H + GAP);
    const w = Math.max(2, Math.round((count / maxVal) * barW));
    const color = colorFn(label);
    const shortLabel = label.length > 16 ? label.slice(0, 15) + '…' : label;
    return `<g>
      <text x="${LABEL_W - 4}" y="${y + BAR_H - 3}" text-anchor="end" class="bar-label">${esc(shortLabel)}</text>
      <rect x="${LABEL_W}" y="${y}" width="${w}" height="${BAR_H}" fill="${esc(color)}" rx="2"/>
      <text x="${LABEL_W + w + 4}" y="${y + BAR_H - 3}" class="bar-val">${count}</text>
    </g>`;
  }).join('');

  return `<div class="card">
    <h3>${esc(title)}</h3>
    <svg width="${W}" height="${H}" class="bar-svg">
      <style>
        .bar-label{font:11px system-ui,sans-serif;fill:#4a6165}
        .bar-val{font:10px system-ui,sans-serif;fill:#0f172a}
      </style>
      ${bars}
    </svg>
  </div>`;
}

export function renderStats(features) {
  if (!_container) return;

  const total = features.length;
  const byType = countBy(features, (p) => p.type ?? p.facility_type ?? '');
  const byCountry = countBy(features, (p) => p.country ?? '');
  const byArea = [];
  const areaMap = {};
  for (const f of features) {
    const p = normalizeFeature(f);
    const areas = Array.isArray(p.areas) ? p.areas : [];
    for (const a of areas) {
      if (a) areaMap[a] = (areaMap[a] || 0) + 1;
    }
  }
  const areaEntries = Object.entries(areaMap).sort((a, b) => b[1] - a[1]);
  const hasAreas = areaEntries.length > 0;

  const countries = new Set(features.map((f) => normalizeFeature(f).country)).size;

  const typeChart = barChart(
    byType, 'By Facility Type',
    (slug) => TYPE_COLORS[slug] || '#64748b',
    15,
  );
  const countryChart = barChart(
    byCountry, 'By Country',
    () => '#0d6e6e',
    15,
  );
  const areaNote = hasAreas
    ? ''
    : '<p class="no-data-note">Research area data requires DuckDB-Wasm; not available in GeoJSON fallback.</p>';
  const areaChart = hasAreas
    ? barChart(areaEntries, 'By Research Area', () => '#d4a017', 12)
    : `<div class="card"><h3>By Research Area</h3>${areaNote}</div>`;

  _container.innerHTML = `
    <div class="stats-summary">
      <strong>${total.toLocaleString()}</strong> facilities across
      <strong>${countries}</strong> countries
    </div>
    <div class="stats-charts">
      ${typeChart}
      ${countryChart}
      ${areaChart}
    </div>`;
}

export function initStatsView(container) {
  _container = container;
  _container.innerHTML = '<p style="padding:16px;color:var(--c-muted)">Loading…</p>';
}
