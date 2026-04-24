// network.js — Network view: D3 force-directed graph linking facilities,
// research areas, networks, facility types, regions, and funders.
//
// Inspired by the unm_kmap force-directed knowledge graph, but adapted to
// the cod-kmap multipartite schema:
//
//   facility  ──member-of──> network
//   facility  ──works-on───> research_area
//   facility  ──instance-of─> facility_type
//   facility  ──located-in──> region
//   facility  ──funded-by──> funder
//   region    ──part-of────> network
//
// Nodes from each category get a distinct color and are sized by their
// in-graph degree. Toggle buttons under the legend let the user pull a
// whole layer in or out without re-querying. The data load is lazy —
// the first time the view becomes active we fetch parquet via DuckDB-Wasm
// (or fall back to the GeoJSON facilities + a flat parquet read).

import { TYPE_COLORS } from '../map.js';
import { getConn } from '../db.js';

let _container = null;
let _initialised = false;
let _graphData = null;        // { nodes, edges }
let _d3Promise = null;
let _activeKinds = null;      // Set of node kinds currently visible
let _sim = null;              // current d3.forceSimulation

const KIND_COLORS = {
  facility:      '#0d6e6e',   // teal — primary brand colour
  network:       '#7c3aed',   // purple
  area:          '#d4a017',   // gold
  type:          '#e0651f',   // burnt orange
  region:        '#16a34a',   // green
  funder:        '#dc2626',   // red
};
const KIND_LABELS = {
  facility: 'Facility',
  network:  'Network',
  area:     'Research area',
  type:     'Facility type',
  region:   'Region (overlay)',
  funder:   'Funder',
};
const DEFAULT_KINDS = new Set(['facility', 'network', 'area', 'type']);

// ── d3 loader ───────────────────────────────────────────────────────
function loadD3() {
  if (_d3Promise) return _d3Promise;
  _d3Promise = import('https://esm.sh/d3@7');
  return _d3Promise;
}

// ── Data assembly ───────────────────────────────────────────────────
//
// We build a multi-partite graph from the DuckDB views/tables that the
// existing db.js wires up (facilities, networks, research_areas,
// facility_types, regions, funders, plus the link tables).
//
// Edges carry a `relation` so the link styling / future filters can
// distinguish "facility ↔ area" from "region ↔ network" etc.

async function buildGraphFromDuckDB() {
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  // Fetch all the entities and links in parallel.
  const sqls = {
    facilities:    `SELECT facility_id AS id, canonical_name AS name, acronym,
                           facility_type AS type, country
                      FROM facilities`,
    facility_types:`SELECT slug AS id, label AS name FROM facility_types`,
    networks:      `SELECT network_id AS id, label AS name, level, url FROM networks`,
    research_areas:`SELECT area_id AS id, label AS name, parent_id FROM research_areas`,
    regions:       `SELECT region_id AS id, name, acronym, kind, network_id, url
                      FROM regions`,
    funders:       `SELECT funder_id AS id, name, type, country FROM funders`,
    area_links:    `SELECT facility_id, area_id FROM area_links`,
    network_membership: `SELECT facility_id, network_id, role FROM network_membership`,
    facility_regions:   `SELECT facility_id, region_id, relation FROM facility_regions`,
    funding_links:      `SELECT facility_id, funder_id, relation FROM funding_links`,
    region_area_links:  `SELECT region_id, area_id FROM region_area_links`,
  };
  const out = {};
  for (const [k, sql] of Object.entries(sqls)) {
    const r = await conn.query(sql);
    out[k] = r.toArray().map((row) => row.toJSON());
  }
  return assembleGraph(out);
}

function assembleGraph(t) {
  const nodes = [];
  const seen = new Set();

  function pushNode(id, kind, props = {}) {
    if (!id) return;
    const nid = `${kind}:${id}`;
    if (seen.has(nid)) return;
    seen.add(nid);
    nodes.push({ id: nid, kind, raw_id: id, ...props });
  }

  // Facility-type nodes (categorical hubs).
  for (const ft of t.facility_types) {
    pushNode(ft.id, 'type', {
      name: ft.name,
      color: TYPE_COLORS[ft.id] || KIND_COLORS.type,
    });
  }
  // Networks.
  for (const n of t.networks) {
    pushNode(n.id, 'network', { name: n.name, level: n.level, url: n.url });
  }
  // Research areas.
  for (const ra of t.research_areas) {
    pushNode(ra.id, 'area', { name: ra.name, parent_id: ra.parent_id });
  }
  // Regions.
  for (const r of t.regions) {
    pushNode(r.id, 'region', {
      name: r.name, acronym: r.acronym, kind_label: r.kind,
      network_id: r.network_id, url: r.url,
    });
  }
  // Funders.
  for (const fu of t.funders) {
    pushNode(fu.id, 'funder', { name: fu.name, type: fu.type, country: fu.country });
  }
  // Facilities (last so the type/network/area nodes already exist).
  for (const f of t.facilities) {
    pushNode(f.id, 'facility', {
      name: f.name, acronym: f.acronym, type: f.type, country: f.country,
      color: TYPE_COLORS[f.type] || KIND_COLORS.facility,
    });
  }

  const edges = [];
  function pushEdge(src, tgt, relation, weight = 1) {
    if (!src || !tgt) return;
    if (!seen.has(src) || !seen.has(tgt)) return;
    edges.push({ source: src, target: tgt, relation, weight });
  }

  // facility ↔ facility_type (always present, makes the "type" hubs visible).
  for (const f of t.facilities) {
    pushEdge(`facility:${f.id}`, `type:${f.facility_type ?? f.type}`, 'instance-of');
  }
  // facility ↔ network.
  for (const m of t.network_membership) {
    pushEdge(`facility:${m.facility_id}`, `network:${m.network_id}`, 'member-of');
  }
  // facility ↔ research_area.
  for (const al of t.area_links) {
    pushEdge(`facility:${al.facility_id}`, `area:${al.area_id}`, 'works-on');
  }
  // facility ↔ region (point-in-polygon).
  for (const fr of t.facility_regions) {
    pushEdge(`facility:${fr.facility_id}`, `region:${fr.region_id}`,
      fr.relation || 'within');
  }
  // facility ↔ funder.
  for (const fl of t.funding_links) {
    pushEdge(`facility:${fl.facility_id}`, `funder:${fl.funder_id}`,
      fl.relation || 'funded-by');
  }
  // region ↔ network (each region row carries a network_id).
  for (const r of t.regions) {
    if (r.network_id) {
      pushEdge(`region:${r.id}`, `network:${r.network_id}`, 'part-of');
    }
  }
  // region ↔ research_area.
  for (const ral of t.region_area_links) {
    pushEdge(`region:${ral.region_id}`, `area:${ral.area_id}`, 'addresses');
  }
  // research_area ↔ research_area (parent/child taxonomy).
  for (const ra of t.research_areas) {
    if (ra.parent_id) {
      pushEdge(`area:${ra.id}`, `area:${ra.parent_id}`, 'sub-area');
    }
  }

  return { nodes, edges };
}

// ── Render ──────────────────────────────────────────────────────────
async function ensureData() {
  if (_graphData) return _graphData;
  // Show progress while building.
  _container.querySelector('#net-status').textContent =
    'Querying DuckDB and assembling graph…';
  try {
    _graphData = await buildGraphFromDuckDB();
  } catch (err) {
    _container.querySelector('#net-status').textContent =
      `Network unavailable: ${err.message}`;
    throw err;
  }
  return _graphData;
}

function filterGraph(graph, activeKinds) {
  const nodes = graph.nodes.filter((n) => activeKinds.has(n.kind));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = graph.edges
    .filter((e) => ids.has(typeof e.source === 'object' ? e.source.id : e.source)
                && ids.has(typeof e.target === 'object' ? e.target.id : e.target))
    // d3.forceLink mutates source/target into node refs once a sim runs;
    // create a fresh edge array so re-renders don't carry stale refs.
    .map((e) => ({
      source: typeof e.source === 'object' ? e.source.id : e.source,
      target: typeof e.target === 'object' ? e.target.id : e.target,
      relation: e.relation,
      weight: e.weight ?? 1,
    }));
  return { nodes: nodes.map((n) => ({ ...n })), edges };
}

async function render() {
  const d3 = await loadD3();
  const graph = await ensureData();
  const { nodes, edges } = filterGraph(graph, _activeKinds);

  // Compute per-node degree → drives radius.
  const degree = new Map();
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1);
    degree.set(e.target, (degree.get(e.target) || 0) + 1);
  }
  for (const n of nodes) n.degree = degree.get(n.id) || 0;

  const stage = _container.querySelector('#net-stage');
  stage.innerHTML = '';
  const w = stage.clientWidth || 1000;
  const h = stage.clientHeight || 700;

  const svg = d3.select(stage).append('svg')
    .attr('viewBox', `0 0 ${w} ${h}`)
    .attr('preserveAspectRatio', 'xMidYMid meet')
    .attr('class', 'network-svg');

  const g = svg.append('g');

  // Pan + zoom (whole graph as one transform group).
  svg.call(d3.zoom().scaleExtent([0.15, 6]).on('zoom', (ev) => {
    g.attr('transform', ev.transform);
  }));

  // Tooltip element (lazy create).
  let tip = _container.querySelector('.network-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'network-tooltip';
    tip.style.display = 'none';
    _container.appendChild(tip);
  }

  // Stop any previous simulation cleanly so we don't leak ticks across renders.
  if (_sim) _sim.stop();

  // Nodes from "category" kinds (type/network/area/region/funder) act as
  // hubs — strong attraction to their own region of the canvas. Facilities
  // are pulled by their links. Mirrors the unm_kmap "departments arranged
  // on a ring" pattern but with categories instead of departments.
  const kindAngle = {};
  const visibleKinds = [...new Set(nodes.map((n) => n.kind))]
    .filter((k) => k !== 'facility');
  visibleKinds.forEach((k, i) => {
    kindAngle[k] = (i / Math.max(1, visibleKinds.length)) * Math.PI * 2;
  });
  const ringR = Math.min(w, h) * 0.35;
  const cx = w / 2, cy = h / 2;
  function homeX(d) {
    if (d.kind === 'facility') return cx;
    return cx + Math.cos(kindAngle[d.kind] || 0) * ringR;
  }
  function homeY(d) {
    if (d.kind === 'facility') return cy;
    return cy + Math.sin(kindAngle[d.kind] || 0) * ringR;
  }

  _sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges)
      .id((d) => d.id)
      .distance((d) => {
        // Categorical edges (instance-of / member-of) shorter so hubs
        // stay tight; cross-domain edges (funder/region) longer.
        if (d.relation === 'instance-of') return 28;
        if (d.relation === 'member-of') return 38;
        if (d.relation === 'works-on') return 50;
        if (d.relation === 'within' || d.relation === 'addresses') return 60;
        if (d.relation === 'sub-area') return 24;
        return 60;
      })
      .strength((d) => {
        if (d.relation === 'instance-of') return 0.6;
        if (d.relation === 'sub-area') return 0.7;
        if (d.relation === 'member-of') return 0.45;
        return 0.25;
      }))
    .force('charge', d3.forceManyBody().strength((d) =>
      d.kind === 'facility' ? -45 : -260))
    .force('collision', d3.forceCollide().radius((d) => nodeRadius(d) + 2))
    .force('center', d3.forceCenter(cx, cy))
    .force('x', d3.forceX(homeX).strength((d) => d.kind === 'facility' ? 0.02 : 0.18))
    .force('y', d3.forceY(homeY).strength((d) => d.kind === 'facility' ? 0.02 : 0.18));

  function nodeRadius(d) {
    if (d.kind === 'facility') {
      return Math.max(3, Math.min(7, 2 + d.degree * 0.4));
    }
    // Categorical hubs scale with their degree (i.e., how many facilities
    // / regions reference them) so visually the "big" topics pop.
    return Math.max(6, Math.min(22, 5 + Math.sqrt(d.degree) * 2));
  }

  function nodeColor(d) {
    if (d.color) return d.color;
    return KIND_COLORS[d.kind] || '#64748b';
  }

  // Links.
  const link = g.append('g').attr('class', 'net-links')
    .selectAll('line').data(edges).join('line')
    .attr('stroke', (d) => {
      switch (d.relation) {
        case 'instance-of': return '#cbd5e1';
        case 'member-of':   return KIND_COLORS.network;
        case 'works-on':    return KIND_COLORS.area;
        case 'within':      return KIND_COLORS.region;
        case 'funded-by':   return KIND_COLORS.funder;
        case 'addresses':   return '#86efac';
        case 'sub-area':    return '#fde68a';
        case 'part-of':     return '#c4b5fd';
        default:            return '#94a3b8';
      }
    })
    .attr('stroke-opacity', (d) => d.relation === 'instance-of' ? 0.18 : 0.35)
    .attr('stroke-width', (d) => d.relation === 'sub-area' ? 1.2 : 0.9);

  // Nodes.
  const node = g.append('g').attr('class', 'net-nodes')
    .selectAll('circle').data(nodes).join('circle')
    .attr('r', nodeRadius)
    .attr('fill', nodeColor)
    .attr('stroke', '#fff')
    .attr('stroke-width', (d) => d.kind === 'facility' ? 0.6 : 1.4)
    .attr('opacity', 0.92)
    .style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) _sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) _sim.alphaTarget(0); d.fx = null; d.fy = null; }));

  // Labels — only for non-facility hubs (facility names would clutter).
  const labels = g.append('g').attr('class', 'net-labels')
    .selectAll('text').data(nodes.filter((n) => n.kind !== 'facility'))
    .join('text')
    .text((d) => d.name)
    .attr('font-size', (d) => Math.min(13, 9 + Math.sqrt(d.degree) * 0.4))
    .attr('text-anchor', 'middle')
    .attr('dy', (d) => -nodeRadius(d) - 4)
    .attr('paint-order', 'stroke')
    .attr('stroke', 'rgba(255,255,255,0.85)')
    .attr('stroke-width', 3)
    .attr('fill', '#0f172a')
    .style('pointer-events', 'none');

  // Hover: highlight neighbourhood.
  const adj = new Map();
  for (const n of nodes) adj.set(n.id, new Set());
  for (const e of edges) {
    adj.get(typeof e.source === 'object' ? e.source.id : e.source).add(
      typeof e.target === 'object' ? e.target.id : e.target);
    adj.get(typeof e.target === 'object' ? e.target.id : e.target).add(
      typeof e.source === 'object' ? e.source.id : e.source);
  }

  function showTip(ev, d) {
    const neighbours = adj.get(d.id) || new Set();
    const kindLabel = KIND_LABELS[d.kind] || d.kind;
    const sub = [
      d.acronym ? `<code>${esc(d.acronym)}</code>` : '',
      d.country ? `· ${esc(d.country)}` : '',
      d.level ? `· ${esc(d.level)}` : '',
      d.kind_label ? `· ${esc(d.kind_label)}` : '',
    ].filter(Boolean).join(' ');
    tip.innerHTML = `
      <div class="tt-kind" style="color:${nodeColor(d)}">${esc(kindLabel)}</div>
      <div class="tt-name">${esc(d.name)}</div>
      ${sub ? `<div class="tt-sub">${sub}</div>` : ''}
      <div class="tt-meta">${neighbours.size} link${neighbours.size === 1 ? '' : 's'} · degree ${d.degree}</div>
      ${d.url ? `<div class="tt-url"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a></div>` : ''}
    `;
    tip.style.display = 'block';
    tip.style.left = (ev.clientX - stage.getBoundingClientRect().left + 14) + 'px';
    tip.style.top  = (ev.clientY - stage.getBoundingClientRect().top + 14) + 'px';

    node.attr('opacity', (n) => (n.id === d.id || neighbours.has(n.id)) ? 1 : 0.12);
    link.attr('stroke-opacity', (l) => {
      const sId = typeof l.source === 'object' ? l.source.id : l.source;
      const tId = typeof l.target === 'object' ? l.target.id : l.target;
      return (sId === d.id || tId === d.id) ? 0.85 : 0.04;
    });
  }
  function hideTip() {
    tip.style.display = 'none';
    node.attr('opacity', 0.92);
    link.attr('stroke-opacity', (d) => d.relation === 'instance-of' ? 0.18 : 0.35);
  }
  node.on('mouseover', showTip).on('mousemove', showTip).on('mouseout', hideTip);

  _sim.on('tick', () => {
    link
      .attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
      .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y);
    node.attr('cx', (d) => d.x).attr('cy', (d) => d.y);
    labels.attr('x', (d) => d.x).attr('y', (d) => d.y);
  });

  // Status line + counts.
  const statusEl = _container.querySelector('#net-status');
  statusEl.innerHTML =
    `<strong>${nodes.length.toLocaleString()}</strong> nodes · ` +
    `<strong>${edges.length.toLocaleString()}</strong> edges · ` +
    `drag nodes to pin · scroll to zoom`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ── Public API ──────────────────────────────────────────────────────
export function initNetworkView(container) {
  _container = container;
  _activeKinds = new Set(DEFAULT_KINDS);

  const legendRows = Object.keys(KIND_COLORS).map((k) => `
    <label class="net-toggle">
      <input type="checkbox" data-kind="${k}" ${_activeKinds.has(k) ? 'checked' : ''}>
      <span class="net-swatch" style="background:${KIND_COLORS[k]}"></span>
      ${KIND_LABELS[k]}
    </label>`).join('');

  _container.innerHTML = `
    <div class="network-view">
      <header class="network-header">
        <div>
          <h2>Knowledge graph</h2>
          <p class="network-sub">Multipartite force-directed view of facilities, networks,
          research areas, facility types, regions and funders. Toggle layers below.</p>
        </div>
        <div class="network-actions">
          <button id="net-restart" class="btn-ghost" title="Restart layout">Restart layout</button>
        </div>
      </header>
      <div class="network-controls">
        <div class="network-legend">${legendRows}</div>
        <div id="net-status" class="network-status">Loading…</div>
      </div>
      <div id="net-stage" class="network-stage"></div>
    </div>`;

  // Wire toggles.
  _container.querySelectorAll('.net-toggle input').forEach((el) => {
    el.addEventListener('change', () => {
      const k = el.dataset.kind;
      if (el.checked) _activeKinds.add(k); else _activeKinds.delete(k);
      // facility kind drives most edges — warn (visually) if user removes it.
      render().catch((err) => console.error(err));
    });
  });
  _container.querySelector('#net-restart').addEventListener('click', () => {
    if (_sim) { _sim.alpha(1).restart(); }
  });
}

export async function renderNetworkView() {
  if (!_container) return;
  if (!_initialised) {
    _initialised = true;
  }
  try {
    await render();
  } catch (e) {
    console.error('network render failed', e);
  }
}

// Force a re-fetch (e.g., after data refresh). Not currently triggered
// from the rest of the app but kept for the future ingest-button path.
export function invalidateNetworkData() {
  _graphData = null;
}
