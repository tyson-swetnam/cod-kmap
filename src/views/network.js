// network.js — Network view: D3 force-directed knowledge graph.
//
// Nodes are the _categorical_ entities in the cod-kmap schema:
//
//   - network           (IOOS regional associations, LTER, NERRS, etc.)
//   - research_area     (GCMD-style topics: coastal processes, etc.)
//   - facility_type     (federal, university-marine-lab, network, …)
//   - region            (individual overlay polygons: sanctuaries, NERRs, …)
//   - funder            (federal / foundation / state agencies)
//
// Facilities are NOT drawn as nodes — they act as the "connective tissue"
// that turns each shared facility between two category nodes into an edge
// of weight N. So "IOOS × coastal-processes" is one edge weighted by the
// number of facilities that are IOOS members AND tagged to coastal
// processes, rather than dozens of individual facility rays. Edges that
// are structural (region → network, area taxonomy) don't need the
// facility join — they're built directly from the link tables.

import { getConn, whenReady } from '../db.js';

let _container = null;
let _graphData = null;
let _d3Promise = null;
let _activeKinds = null;
let _sim = null;
let _minWeight = 1;

const KIND_COLORS = {
  network:       '#7c3aed',   // purple
  area:          '#d4a017',   // gold
  type:          '#e0651f',   // burnt orange
  region:        '#16a34a',   // green
  funder:        '#dc2626',   // red
};
const KIND_LABELS = {
  network:  'Network',
  area:     'Research area',
  type:     'Facility type',
  region:   'Region (overlay)',
  funder:   'Funder',
};
// Default view: the three top-level category layers (concise & readable).
// Regions and funders add ~150 and ~70 extra nodes — opt-in via toggle.
const DEFAULT_KINDS = new Set(['network', 'area', 'type']);

// ── d3 loader ───────────────────────────────────────────────────────
function loadD3() {
  if (_d3Promise) return _d3Promise;
  _d3Promise = import('https://esm.sh/d3@7');
  return _d3Promise;
}

// ── Data assembly ───────────────────────────────────────────────────
async function buildGraphFromDuckDB() {
  // Wait until every parquet view is registered. Without this, clicking
  // the Network tab during the initial bootstrap window hits a partially
  // initialised connection where some tables (e.g. `networks`) don't
  // yet exist, and the first query fails with a Catalog Error.
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  // Categorical entities (→ nodes).
  const entitySqls = {
    facility_types: `SELECT slug AS id, label AS name FROM facility_types`,
    networks:       `SELECT network_id AS id, label AS name, level, url FROM networks`,
    research_areas: `SELECT area_id AS id, label AS name, parent_id FROM research_areas`,
    regions:        `SELECT region_id AS id, name, acronym, kind, network_id, url
                       FROM regions`,
    funders:        `SELECT funder_id AS id, name, type, country FROM funders`,
  };

  // Co-occurrence edges across facilities. We only keep category ↔ category
  // (not facility ↔ category) and attach the count of shared facilities
  // so the renderer can weight / scale edges.
  //
  // Helper SQL: derive per-facility sets first, then cartesian-join them
  // inside DuckDB using the link tables. Everything is read-only against
  // the parquet views wired up by db.js.
  const edgeSqls = {
    // Every facility already has a facility_type (the column); count how
    // many facilities of each type are members of each network / area /
    // region / funder.
    type_network: `
      SELECT f.facility_type AS type_id, nm.network_id AS network_id,
             COUNT(DISTINCT f.facility_id) AS w
      FROM facilities f
      JOIN network_membership nm ON nm.facility_id = f.facility_id
      GROUP BY 1, 2`,
    type_area: `
      SELECT f.facility_type AS type_id, al.area_id AS area_id,
             COUNT(DISTINCT f.facility_id) AS w
      FROM facilities f
      JOIN area_links al ON al.facility_id = f.facility_id
      GROUP BY 1, 2`,
    type_funder: `
      SELECT f.facility_type AS type_id, fl.funder_id AS funder_id,
             COUNT(DISTINCT f.facility_id) AS w
      FROM facilities f
      JOIN funding_links fl ON fl.facility_id = f.facility_id
      GROUP BY 1, 2`,
    type_region: `
      SELECT f.facility_type AS type_id, fr.region_id AS region_id,
             COUNT(DISTINCT f.facility_id) AS w
      FROM facilities f
      JOIN facility_regions fr ON fr.facility_id = f.facility_id
      GROUP BY 1, 2`,
    network_area: `
      SELECT nm.network_id, al.area_id,
             COUNT(DISTINCT nm.facility_id) AS w
      FROM network_membership nm
      JOIN area_links al ON al.facility_id = nm.facility_id
      GROUP BY 1, 2`,
    network_funder: `
      SELECT nm.network_id, fl.funder_id,
             COUNT(DISTINCT nm.facility_id) AS w
      FROM network_membership nm
      JOIN funding_links fl ON fl.facility_id = nm.facility_id
      GROUP BY 1, 2`,
    network_region_facility: `
      SELECT nm.network_id, fr.region_id,
             COUNT(DISTINCT nm.facility_id) AS w
      FROM network_membership nm
      JOIN facility_regions fr ON fr.facility_id = nm.facility_id
      GROUP BY 1, 2`,
    area_region: `
      SELECT al.area_id, fr.region_id,
             COUNT(DISTINCT al.facility_id) AS w
      FROM area_links al
      JOIN facility_regions fr ON fr.facility_id = al.facility_id
      GROUP BY 1, 2`,
    area_funder: `
      SELECT al.area_id, fl.funder_id,
             COUNT(DISTINCT al.facility_id) AS w
      FROM area_links al
      JOIN funding_links fl ON fl.facility_id = al.facility_id
      GROUP BY 1, 2`,
    region_funder: `
      SELECT fr.region_id, fl.funder_id,
             COUNT(DISTINCT fr.facility_id) AS w
      FROM facility_regions fr
      JOIN funding_links fl ON fl.facility_id = fr.facility_id
      GROUP BY 1, 2`,

    // Same-kind edges: area ↔ area taxonomy, network ↔ network
    // (via facilities holding dual membership), region ↔ region
    // (via shared facility containment).
    area_area_parent: `
      SELECT area_id, parent_id FROM research_areas WHERE parent_id IS NOT NULL`,
    network_network: `
      SELECT a.network_id AS a_id, b.network_id AS b_id,
             COUNT(DISTINCT a.facility_id) AS w
      FROM network_membership a
      JOIN network_membership b ON a.facility_id = b.facility_id
                               AND a.network_id  < b.network_id
      GROUP BY 1, 2`,

    // Structural edges from the regions table itself.
    region_network_direct: `
      SELECT region_id, network_id FROM regions WHERE network_id IS NOT NULL`,
    region_area_direct: `
      SELECT region_id, area_id FROM region_area_links`,
  };

  // Kick everything off in parallel.
  const [entities, edges] = await Promise.all([
    fetchAll(conn, entitySqls),
    fetchAll(conn, edgeSqls),
  ]);

  return assembleGraph(entities, edges);
}

async function fetchAll(conn, sqls) {
  const out = {};
  const pairs = Object.entries(sqls);
  await Promise.all(pairs.map(async ([k, sql]) => {
    const r = await conn.query(sql);
    out[k] = r.toArray().map((row) => row.toJSON());
  }));
  return out;
}

function assembleGraph(e, l) {
  const nodes = [];
  const seen = new Set();

  function push(kind, id, props = {}) {
    if (!id) return;
    const nid = `${kind}:${id}`;
    if (seen.has(nid)) return;
    seen.add(nid);
    nodes.push({ id: nid, kind, raw_id: id, ...props });
  }

  for (const ft of e.facility_types)   push('type',    ft.id, { name: ft.name });
  for (const n  of e.networks)          push('network', n.id,  { name: n.name, level: n.level, url: n.url });
  for (const ra of e.research_areas)    push('area',    ra.id, { name: ra.name, parent_id: ra.parent_id });
  for (const r  of e.regions)           push('region',  r.id,  { name: r.name, acronym: r.acronym, kind_label: r.kind, network_id: r.network_id, url: r.url });
  for (const fu of e.funders)           push('funder',  fu.id, { name: fu.name, type: fu.type, country: fu.country });

  const edges = [];
  function pushEdge(src, tgt, relation, weight = 1) {
    if (!src || !tgt || src === tgt) return;
    if (!seen.has(src) || !seen.has(tgt)) return;
    edges.push({ source: src, target: tgt, relation, weight: Number(weight) || 1 });
  }

  // Weighted co-occurrence edges.
  for (const x of l.type_network)    pushEdge(`type:${x.type_id}`,       `network:${x.network_id}`, 'type-network', x.w);
  for (const x of l.type_area)       pushEdge(`type:${x.type_id}`,       `area:${x.area_id}`,        'type-area', x.w);
  for (const x of l.type_funder)     pushEdge(`type:${x.type_id}`,       `funder:${x.funder_id}`,    'type-funder', x.w);
  for (const x of l.type_region)     pushEdge(`type:${x.type_id}`,       `region:${x.region_id}`,    'type-region', x.w);
  for (const x of l.network_area)    pushEdge(`network:${x.network_id}`, `area:${x.area_id}`,        'network-area', x.w);
  for (const x of l.network_funder)  pushEdge(`network:${x.network_id}`, `funder:${x.funder_id}`,    'network-funder', x.w);
  for (const x of l.network_region_facility)
                                     pushEdge(`network:${x.network_id}`, `region:${x.region_id}`,    'network-region', x.w);
  for (const x of l.area_region)     pushEdge(`area:${x.area_id}`,       `region:${x.region_id}`,    'area-region', x.w);
  for (const x of l.area_funder)     pushEdge(`area:${x.area_id}`,       `funder:${x.funder_id}`,    'area-funder', x.w);
  for (const x of l.region_funder)   pushEdge(`region:${x.region_id}`,   `funder:${x.funder_id}`,    'region-funder', x.w);

  // Same-kind edges.
  for (const x of l.network_network) pushEdge(`network:${x.a_id}`,       `network:${x.b_id}`,        'network-network', x.w);
  for (const x of l.area_area_parent) pushEdge(`area:${x.area_id}`,      `area:${x.parent_id}`,      'sub-area', 2);

  // Structural (unweighted) region ↔ network and region ↔ area come from
  // the schema itself. Dedupe against the facility-derived edges by using
  // Map[key] max weight.
  const edgeMap = new Map();
  for (const ed of edges) {
    const key = [ed.source, ed.target, ed.relation].sort().join('|');
    const prev = edgeMap.get(key);
    if (!prev || prev.weight < ed.weight) edgeMap.set(key, ed);
  }
  for (const x of l.region_network_direct) {
    const e1 = `region:${x.region_id}`, e2 = `network:${x.network_id}`;
    const key = [e1, e2, 'region-network'].sort().join('|');
    if (!edgeMap.has(key)) edgeMap.set(key, { source: e1, target: e2, relation: 'region-network', weight: 1 });
  }
  for (const x of l.region_area_direct) {
    const e1 = `region:${x.region_id}`, e2 = `area:${x.area_id}`;
    const key = [e1, e2, 'area-region'].sort().join('|');
    if (!edgeMap.has(key)) edgeMap.set(key, { source: e1, target: e2, relation: 'area-region', weight: 1 });
  }

  return { nodes, edges: [...edgeMap.values()] };
}

// ── Render ──────────────────────────────────────────────────────────
async function ensureData() {
  if (_graphData) return _graphData;
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

function filterGraph(graph, activeKinds, minWeight) {
  const nodes = graph.nodes.filter((n) => activeKinds.has(n.kind));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = graph.edges
    .filter((e) => {
      const s = typeof e.source === 'object' ? e.source.id : e.source;
      const t = typeof e.target === 'object' ? e.target.id : e.target;
      if (!ids.has(s) || !ids.has(t)) return false;
      if ((e.weight || 1) < minWeight && e.relation !== 'sub-area'
          && e.relation !== 'region-network') return false;
      return true;
    })
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
  const { nodes, edges } = filterGraph(graph, _activeKinds, _minWeight);

  const degree = new Map();
  const weightedDegree = new Map();
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1);
    degree.set(e.target, (degree.get(e.target) || 0) + 1);
    weightedDegree.set(e.source, (weightedDegree.get(e.source) || 0) + e.weight);
    weightedDegree.set(e.target, (weightedDegree.get(e.target) || 0) + e.weight);
  }
  for (const n of nodes) {
    n.degree = degree.get(n.id) || 0;
    n.wdegree = weightedDegree.get(n.id) || 0;
  }

  const stage = _container.querySelector('#net-stage');
  stage.innerHTML = '';
  const w = stage.clientWidth || 1000;
  const h = stage.clientHeight || 700;

  const svg = d3.select(stage).append('svg')
    .attr('viewBox', `0 0 ${w} ${h}`)
    .attr('preserveAspectRatio', 'xMidYMid meet')
    .attr('class', 'network-svg');
  const g = svg.append('g');

  svg.call(d3.zoom().scaleExtent([0.15, 6]).on('zoom', (ev) => {
    g.attr('transform', ev.transform);
  }));

  let tip = _container.querySelector('.network-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'network-tooltip';
    tip.style.display = 'none';
    _container.appendChild(tip);
  }

  if (_sim) _sim.stop();

  // Layout: arrange the visible kinds on a ring, pull each kind's nodes
  // toward its home position. Gives the user a predictable "galaxy of
  // topics" shape instead of a hairball.
  const cx = w / 2, cy = h / 2;
  const ringR = Math.min(w, h) * 0.34;
  const visibleKinds = [...new Set(nodes.map((n) => n.kind))];
  const kindAngle = {};
  visibleKinds.forEach((k, i) => {
    kindAngle[k] = (i / Math.max(1, visibleKinds.length)) * Math.PI * 2 - Math.PI / 2;
  });
  const homeX = (d) => cx + Math.cos(kindAngle[d.kind] || 0) * ringR;
  const homeY = (d) => cy + Math.sin(kindAngle[d.kind] || 0) * ringR;

  const maxWeight = Math.max(1, ...edges.map((e) => e.weight));

  const nodeRadius = (d) => {
    const base = 6 + Math.sqrt(d.wdegree || d.degree || 1) * 1.1;
    return Math.max(5, Math.min(30, base));
  };
  const nodeColor = (d) => KIND_COLORS[d.kind] || '#64748b';

  _sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges)
      .id((d) => d.id)
      .distance((d) => {
        const norm = (d.weight || 1) / maxWeight;
        // Stronger relation => shorter link. Clamp.
        return 170 - norm * 110;
      })
      .strength((d) => {
        const norm = (d.weight || 1) / maxWeight;
        if (d.relation === 'sub-area') return 0.8;
        return 0.08 + norm * 0.6;
      }))
    .force('charge', d3.forceManyBody().strength(-320))
    .force('collision', d3.forceCollide().radius((d) => nodeRadius(d) + 4))
    .force('center', d3.forceCenter(cx, cy))
    .force('x', d3.forceX(homeX).strength(0.12))
    .force('y', d3.forceY(homeY).strength(0.12));

  const link = g.append('g').attr('class', 'net-links')
    .selectAll('line').data(edges).join('line')
    .attr('stroke', (d) => {
      if (d.relation === 'sub-area')       return '#fde68a';
      if (d.relation === 'region-network') return '#c4b5fd';
      if (d.relation === 'area-region')    return '#86efac';
      if (d.relation.startsWith('type-'))  return '#fbbf24';
      if (d.relation.includes('funder'))   return '#fca5a5';
      if (d.relation === 'network-network')return '#a78bfa';
      return '#94a3b8';
    })
    .attr('stroke-opacity', (d) => 0.25 + 0.45 * Math.min(1, d.weight / maxWeight))
    .attr('stroke-width',   (d) => 0.6 + 3.5 * Math.min(1, d.weight / maxWeight));

  const node = g.append('g').attr('class', 'net-nodes')
    .selectAll('circle').data(nodes).join('circle')
    .attr('r', nodeRadius)
    .attr('fill', nodeColor)
    .attr('stroke', '#fff')
    .attr('stroke-width', 1.6)
    .attr('opacity', 0.95)
    .style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) _sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) _sim.alphaTarget(0); d.fx = null; d.fy = null; }));

  const labels = g.append('g').attr('class', 'net-labels')
    .selectAll('text').data(nodes).join('text')
    .text((d) => d.name)
    .attr('font-size', (d) => {
      const base = 10 + Math.sqrt(d.wdegree || d.degree || 1) * 0.2;
      return Math.max(10, Math.min(15, base));
    })
    .attr('text-anchor', 'middle')
    .attr('dy', (d) => -nodeRadius(d) - 4)
    .attr('paint-order', 'stroke')
    .attr('stroke', 'rgba(255,255,255,0.92)')
    .attr('stroke-width', 3.5)
    .attr('fill', '#0f172a')
    .style('pointer-events', 'none');

  // Neighbourhood highlight on hover.
  const adj = new Map();
  for (const n of nodes) adj.set(n.id, new Set());
  for (const e of edges) {
    adj.get(e.source).add(e.target);
    adj.get(e.target).add(e.source);
  }

  function showTip(ev, d) {
    const neighbours = adj.get(d.id) || new Set();
    const kindLabel = KIND_LABELS[d.kind] || d.kind;
    const sub = [
      d.acronym ? `<code>${esc(d.acronym)}</code>` : '',
      d.country ? esc(d.country) : '',
      d.level ? esc(d.level) : '',
      d.kind_label ? esc(d.kind_label) : '',
    ].filter(Boolean).join(' · ');
    tip.innerHTML = `
      <div class="tt-kind" style="color:${nodeColor(d)}">${esc(kindLabel)}</div>
      <div class="tt-name">${esc(d.name)}</div>
      ${sub ? `<div class="tt-sub">${sub}</div>` : ''}
      <div class="tt-meta">${neighbours.size} connection${neighbours.size === 1 ? '' : 's'} · weighted deg ${d.wdegree}</div>
      ${d.url ? `<div class="tt-url"><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a></div>` : ''}`;
    tip.style.display = 'block';
    const rect = stage.getBoundingClientRect();
    tip.style.left = (ev.clientX - rect.left + 14) + 'px';
    tip.style.top  = (ev.clientY - rect.top + 14) + 'px';

    node.attr('opacity', (n) => (n.id === d.id || neighbours.has(n.id)) ? 1 : 0.12);
    link.attr('stroke-opacity', (l) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      return (s === d.id || t === d.id) ? 0.9 : 0.04;
    });
    labels.attr('opacity', (n) => (n.id === d.id || neighbours.has(n.id)) ? 1 : 0.15);
  }
  function hideTip() {
    tip.style.display = 'none';
    node.attr('opacity', 0.95);
    link.attr('stroke-opacity', (d) => 0.25 + 0.45 * Math.min(1, d.weight / maxWeight));
    labels.attr('opacity', 1);
  }
  node.on('mouseover', showTip).on('mousemove', showTip).on('mouseout', hideTip);

  _sim.on('tick', () => {
    link
      .attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
      .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y);
    node.attr('cx', (d) => d.x).attr('cy', (d) => d.y);
    labels.attr('x', (d) => d.x).attr('y', (d) => d.y);
  });

  const statusEl = _container.querySelector('#net-status');
  statusEl.innerHTML =
    `<strong>${nodes.length.toLocaleString()}</strong> nodes · ` +
    `<strong>${edges.length.toLocaleString()}</strong> edges · ` +
    `drag to pin · scroll to zoom · hover to highlight`;
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
          <p class="network-sub">Networks, research areas, facility types, regions and funders.
          Edges are weighted by the number of facilities they share — thicker lines mean stronger
          category overlap. Toggle layers below; hover a node to spotlight its neighbourhood.</p>
        </div>
        <div class="network-actions">
          <label class="network-weight">
            Min&nbsp;edge&nbsp;weight:
            <input id="net-min-weight" type="number" min="1" step="1" value="1">
          </label>
          <button id="net-restart" class="btn-ghost" title="Restart layout">Restart layout</button>
        </div>
      </header>
      <div class="network-controls">
        <div class="network-legend">${legendRows}</div>
        <div id="net-status" class="network-status">Loading…</div>
      </div>
      <div id="net-stage" class="network-stage"></div>
    </div>`;

  _container.querySelectorAll('.net-toggle input').forEach((el) => {
    el.addEventListener('change', () => {
      const k = el.dataset.kind;
      if (el.checked) _activeKinds.add(k); else _activeKinds.delete(k);
      render().catch((err) => console.error(err));
    });
  });
  _container.querySelector('#net-restart').addEventListener('click', () => {
    if (_sim) _sim.alpha(1).restart();
  });
  _container.querySelector('#net-min-weight').addEventListener('change', (ev) => {
    const v = Math.max(1, parseInt(ev.target.value, 10) || 1);
    ev.target.value = String(v);
    _minWeight = v;
    render().catch((err) => console.error(err));
  });
}

export async function renderNetworkView() {
  if (!_container) return;
  try {
    await render();
  } catch (e) {
    console.error('network render failed', e);
  }
}

export function invalidateNetworkData() {
  _graphData = null;
}
