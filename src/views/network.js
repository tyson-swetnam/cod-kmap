// network.js — Knowledge map (MVG, Map Visualization with Group restriction).
//
// Replaces the previous force-directed knowledge graph with a country-like
// map where each polygon is one research area (parent-collapsed when small),
// polygon area is proportional to the number of facilities in that area,
// and facilities + people sit inside their polygon. Cross-area edges
// reveal interdisciplinary collaboration.
//
// Implements the KMap algorithm from Hossain, Moradi, Mondal & Kobourov,
// "Map Visualizations for Graphs with Group Restrictions" (Graphics
// Interface 2025, DOI 10.1145/3769872.3769900). Three steps:
//
//   1. Supergraph: one supernode per active research area, weight = facility
//      count, edges = cross-area facility-personnel + co-author counts.
//      Embed with d3-force using square collision so each area gets a
//      non-overlapping square sized by sqrt(weight).
//
//   2. Subgraph layout: for each area, run a small d3-force layout on its
//      facility + person nodes, then scale to fit inside its square.
//
//   3. Voronoi-merged polygons: compute Voronoi over all node positions
//      plus a ring of perimeter "anchor" points; for each area, union the
//      cells of its members via polygon-clipping. Smooth boundaries via
//      one Chaikin pass to soften the polygon outlines.
//
// Phase 3 (PCL refinement, custom force terms) and Phase 5 (canvas
// rendering + Web Worker) are documented in docs/map_visualization_plan.md
// and will land as follow-up commits.

import { getConn, whenReady } from '../db.js';

// ── Module state ────────────────────────────────────────────────────
let _container = null;
let _layout = null;
let _d3Promise = null;
let _delaunayPromise = null;
let _polygonClippingPromise = null;
let _showFacility = true;
let _showPerson = true;
let _showCrossEdges = true;

// 33-step palette for area polygons. Tuned for distinguishability
// against a parchment background with low-alpha fills.
const AREA_PALETTE = [
  '#7c3aed', '#0d9488', '#d97706', '#dc2626', '#2563eb',
  '#059669', '#a16207', '#9333ea', '#0891b2', '#65a30d',
  '#e11d48', '#0284c7', '#ca8a04', '#7e22ce', '#16a34a',
  '#b45309', '#1d4ed8', '#15803d', '#a21caf', '#be123c',
  '#0369a1', '#4d7c0f', '#be185d', '#1e40af', '#166534',
  '#86198f', '#1e3a8a', '#854d0e', '#5b21b6', '#0c4a6e',
  '#365314', '#3f6212', '#172554',
];

const NODE_COLORS = {
  facility: '#0d6e6e',
  person:   '#0ea5e9',
};
const NODE_RADIUS = { facility: 4, person: 3 };

// Layout tuning
const SUPERGRAPH_TICKS = 400;
const SUBGRAPH_TICKS   = 120;
const SUPER_PADDING    = 18;     // px gap between adjacent squares
const PERIMETER_PAD    = 0.18;   // anchor ring at 1+pad of layout bbox half-width
const PERIMETER_NODES  = 24;     // anchors around the layout
const SUPERNODE_SCALE  = 9;      // side = scale * sqrt(weight)


// ── Async-import helpers ────────────────────────────────────────────
function loadD3() {
  if (_d3Promise) return _d3Promise;
  _d3Promise = import('https://esm.sh/d3@7');
  return _d3Promise;
}
function loadDelaunay() {
  if (_delaunayPromise) return _delaunayPromise;
  _delaunayPromise = import('https://esm.sh/d3-delaunay@6');
  return _delaunayPromise;
}
function loadPolygonClipping() {
  if (_polygonClippingPromise) return _polygonClippingPromise;
  _polygonClippingPromise = import('https://esm.sh/polygon-clipping@0.15.7');
  return _polygonClippingPromise;
}


// ── Data fetch ──────────────────────────────────────────────────────
async function fetchData() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  const queries = {
    // ACTIVE areas only — collapsed_into IS NULL means this area is its
    // own polygon. Collapsed areas are absorbed into their parent in the
    // facility/person primary tables already.
    areas: `
      SELECT area_id AS id, label AS name, n_facilities AS weight
      FROM   research_areas_active
      WHERE  collapsed_into IS NULL
      ORDER  BY area_id`,

    // One row per facility with its primary area + display fields.
    facilities: `
      SELECT f.facility_id AS id,
             f.canonical_name AS name,
             f.acronym,
             f.country,
             f.facility_type AS f_type,
             f.url,
             g.primary_area_id AS area_id
      FROM   facilities f
      JOIN   facility_primary_groups g ON g.facility_id = f.facility_id
      WHERE  g.primary_area_id IS NOT NULL`,

    // One row per person with primary area; only people who have ≥1
    // facility role appear so the map stays in sync with what we know.
    people: `
      SELECT p.person_id AS id,
             p.name,
             p.orcid,
             p.openalex_id,
             p.homepage_url,
             g.primary_area_id AS area_id
      FROM   people p
      JOIN   person_primary_groups g ON g.person_id = p.person_id
      WHERE  g.primary_area_id IS NOT NULL`,

    // Facility ↔ person via facility_personnel (intra+inter polygon).
    fac_pers: `
      SELECT facility_id AS source, person_id AS target,
             COUNT(*) AS w
      FROM   facility_personnel
      GROUP  BY facility_id, person_id`,

    // Person ↔ person via co-authorship.
    coauthors: `
      SELECT person_a_id AS source, person_b_id AS target,
             co_pub_count AS w
      FROM   collaborations
      WHERE  co_pub_count >= 2`,
  };

  const out = {};
  for (const [k, sql] of Object.entries(queries)) {
    const r = await conn.query(sql);
    out[k] = r.toArray().map((row) => row.toJSON());
  }
  // Coerce BigInt counts to Number.
  for (const a of out.areas) a.weight = Number(a.weight) || 0;
  for (const e of out.fac_pers) e.w = Number(e.w) || 1;
  for (const e of out.coauthors) e.w = Number(e.w) || 1;
  return out;
}


// ── Step 1: supergraph layout (squares packed by area weight) ───────
function buildSupergraph(data) {
  const areaIds = new Set(data.areas.map((a) => a.id));
  const facById = new Map(data.facilities.map((f) => [f.id, f]));
  const perById = new Map(data.people.map((p) => [p.id, p]));

  // Cross-area edge weights from facility-person + co-author edges.
  const edgeW = new Map();
  function bump(a, b, w) {
    if (!a || !b || a === b) return;
    if (!areaIds.has(a) || !areaIds.has(b)) return;
    const k = a < b ? `${a}|${b}` : `${b}|${a}`;
    edgeW.set(k, (edgeW.get(k) || 0) + w);
  }
  for (const e of data.fac_pers) {
    const f = facById.get(e.source); const p = perById.get(e.target);
    if (f && p) bump(f.area_id, p.area_id, e.w);
  }
  for (const e of data.coauthors) {
    const a = perById.get(e.source); const b = perById.get(e.target);
    if (a && b) bump(a.area_id, b.area_id, e.w);
  }

  return {
    nodes: data.areas.map((a) => ({
      id: a.id, name: a.name, weight: a.weight,
      side: Math.max(50, SUPERNODE_SCALE * Math.sqrt(a.weight)),
    })),
    edges: [...edgeW.entries()].map(([k, w]) => {
      const [s, t] = k.split('|');
      return { source: s, target: t, w };
    }),
  };
}

async function layoutSupergraph(d3, sg, w, h) {
  const cx = w / 2, cy = h / 2;
  // Seed positions on a ring proportional to weight so the simulation
  // converges quickly and large groups end up roughly central.
  const sorted = [...sg.nodes].sort((a, b) => b.weight - a.weight);
  const maxR = Math.min(w, h) * 0.36;
  sorted.forEach((n, i) => {
    const t = i / Math.max(sorted.length - 1, 1);
    const r = t * maxR * 0.85 + 0.05 * maxR;
    const a = i * (2 * Math.PI / Math.max(sorted.length, 6)) + 0.1 * i;
    n.x = cx + r * Math.cos(a);
    n.y = cy + r * Math.sin(a);
  });

  // Square-collision: forceCollide treats each node as a circle of
  // radius r; we set r = side/sqrt(2) + padding/2 so square bounding
  // boxes don't quite touch. Approximation but visually adequate.
  const sim = d3.forceSimulation(sg.nodes)
    .alphaDecay(0.04)
    .force('link', d3.forceLink(sg.edges)
      .id((d) => d.id)
      .distance((d) => 30 + Math.sqrt(d.w) * 8)
      .strength(0.4))
    .force('charge', d3.forceManyBody()
      .strength((d) => -120 - d.weight * 4))
    .force('collide', d3.forceCollide()
      .radius((d) => d.side * 0.71 + SUPER_PADDING)
      .strength(1)
      .iterations(2))
    .force('center', d3.forceCenter(cx, cy).strength(0.05))
    .stop();
  for (let i = 0; i < SUPERGRAPH_TICKS; i++) sim.tick();

  // Resolve any remaining overlap with a deterministic relax pass.
  for (let r = 0; r < 60; r++) {
    let moved = false;
    for (let i = 0; i < sg.nodes.length; i++) {
      for (let j = i + 1; j < sg.nodes.length; j++) {
        const a = sg.nodes[i], b = sg.nodes[j];
        const dx = b.x - a.x, dy = b.y - a.y;
        const minD = (a.side + b.side) * 0.5 + SUPER_PADDING;
        const dist = Math.hypot(dx, dy) || 1e-6;
        if (dist < minD) {
          const push = (minD - dist) / 2;
          const nx = dx / dist, ny = dy / dist;
          a.x -= nx * push; a.y -= ny * push;
          b.x += nx * push; b.y += ny * push;
          moved = true;
        }
      }
    }
    if (!moved) break;
  }

  return new Map(sg.nodes.map((n) => [n.id, n]));
}


// ── Step 2: per-group subgraph layout, scale to fit ─────────────────
function membersOfArea(areaId, data) {
  const facs = data.facilities.filter((f) => f.area_id === areaId)
    .map((f) => ({ id: f.id, name: f.name, kind: 'facility',
                   acronym: f.acronym, country: f.country, url: f.url,
                   f_type: f.f_type, area_id: areaId }));
  const peo = data.people.filter((p) => p.area_id === areaId)
    .map((p) => ({ id: p.id, name: p.name, kind: 'person',
                   orcid: p.orcid, openalex_id: p.openalex_id,
                   homepage_url: p.homepage_url, area_id: areaId }));
  return [...facs, ...peo];
}

function intraEdgesOfArea(members, data) {
  const ids = new Set(members.map((m) => m.id));
  const edges = [];
  for (const e of data.fac_pers) {
    if (ids.has(e.source) && ids.has(e.target)) {
      edges.push({ source: e.source, target: e.target, w: e.w });
    }
  }
  for (const e of data.coauthors) {
    if (ids.has(e.source) && ids.has(e.target)) {
      edges.push({ source: e.source, target: e.target, w: e.w });
    }
  }
  return edges;
}

async function layoutAndFit(d3, members, edges, square) {
  if (!members.length) return [];

  // Initial seed inside square so simulation converges fast.
  const cx = square.x, cy = square.y;
  const r0 = square.side * 0.3;
  members.forEach((m, i) => {
    const a = (i / members.length) * 2 * Math.PI;
    m.x = cx + r0 * Math.cos(a);
    m.y = cy + r0 * Math.sin(a);
  });

  const sim = d3.forceSimulation(members)
    .alphaDecay(0.06)
    .force('link', d3.forceLink(edges)
      .id((d) => d.id)
      .distance(18).strength(0.6))
    .force('charge', d3.forceManyBody().strength(-30))
    .force('collide', d3.forceCollide().radius(7).strength(0.9))
    .force('x', d3.forceX(cx).strength(0.04))
    .force('y', d3.forceY(cy).strength(0.04))
    .stop();
  for (let i = 0; i < SUBGRAPH_TICKS; i++) sim.tick();

  // Scale-and-fit into the square.
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of members) {
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x > maxX) maxX = n.x;
    if (n.y > maxY) maxY = n.y;
  }
  const pad = 12;  // breathing room inside the polygon edge
  const targetW = square.side - 2 * pad;
  const spanX = Math.max(maxX - minX, 1);
  const spanY = Math.max(maxY - minY, 1);
  const s = Math.min(targetW / spanX, targetW / spanY);
  const tx = cx - (minX + spanX / 2) * s;
  const ty = cy - (minY + spanY / 2) * s;
  for (const n of members) {
    n.x = n.x * s + tx;
    n.y = n.y * s + ty;
  }
  return members;
}


// ── Step 3: Voronoi-merged country-like polygons ────────────────────
async function computePolygons(d3delaunay, polygonClipping, allNodes, w, h) {
  // Bounding box of all node positions, with padding.
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of allNodes) {
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x > maxX) maxX = n.x;
    if (n.y > maxY) maxY = n.y;
  }
  const padX = (maxX - minX) * PERIMETER_PAD + 30;
  const padY = (maxY - minY) * PERIMETER_PAD + 30;
  const bbMinX = minX - padX, bbMinY = minY - padY;
  const bbMaxX = maxX + padX, bbMaxY = maxY + padY;
  const bbW = bbMaxX - bbMinX, bbH = bbMaxY - bbMinY;

  // Add anchor nodes around the perimeter so outer Voronoi cells get
  // bounded shapes (otherwise they extend to infinity).
  const cxA = (bbMinX + bbMaxX) / 2;
  const cyA = (bbMinY + bbMaxY) / 2;
  const ringR = Math.max(bbW, bbH) * 0.6;
  const anchors = [];
  for (let i = 0; i < PERIMETER_NODES; i++) {
    const a = (i / PERIMETER_NODES) * 2 * Math.PI;
    anchors.push({
      id: `__anchor_${i}`,
      kind: '__anchor',
      x: cxA + ringR * Math.cos(a),
      y: cyA + ringR * Math.sin(a),
    });
  }

  // Voronoi over [nodes + anchors], clipped to a generous bounding box.
  const all = [...allNodes, ...anchors];
  const points = all.map((n) => [n.x, n.y]);
  const delaunay = d3delaunay.Delaunay.from(points);
  const voronoi = delaunay.voronoi([
    cxA - ringR * 1.2, cyA - ringR * 1.2,
    cxA + ringR * 1.2, cyA + ringR * 1.2,
  ]);

  // Group cell indices by area_id (anchors are excluded).
  const cellsByArea = new Map();
  for (let i = 0; i < all.length; i++) {
    const n = all[i];
    if (n.kind === '__anchor') continue;
    const cell = voronoi.cellPolygon(i);
    if (!cell) continue;
    const list = cellsByArea.get(n.area_id) || [];
    list.push(cell);
    cellsByArea.set(n.area_id, list);
  }

  // Union the cells of each area via polygon-clipping. The library
  // returns a MultiPolygon: array of polygons, each an array of rings,
  // each ring an array of [x,y]. For an area's cells that all share
  // edges, this collapses to one polygon. Disconnected components
  // (rare, only happens if Voronoi splits an area's cells across
  // others) come back as multiple polygons in the result.
  const PC = polygonClipping.default || polygonClipping;
  const result = new Map();
  for (const [area, cells] of cellsByArea.entries()) {
    if (!cells.length) continue;
    const wrapped = cells.map((c) => [c]);  // cells are rings → wrap
    let merged;
    try {
      merged = PC.union(...wrapped);
    } catch (e) {
      console.warn('[mvg] polygon union failed for', area, e);
      // Fallback: just use the largest single cell as the "polygon".
      merged = [[cells[0]]];
    }
    // Keep only the largest polygon per area for clean rendering.
    let bestRing = null, bestArea = -Infinity;
    for (const poly of merged) {
      if (!poly || !poly[0] || poly[0].length < 3) continue;
      const a = Math.abs(d3PolygonArea(poly[0]));
      if (a > bestArea) { bestArea = a; bestRing = poly[0]; }
    }
    if (bestRing) result.set(area, chaikin(bestRing, 1));
  }
  return { polygons: result, bbox: { x: bbMinX, y: bbMinY, w: bbW, h: bbH } };
}

// Shoelace area (positive only used for picking largest ring).
function d3PolygonArea(ring) {
  let a = 0;
  for (let i = 0, n = ring.length, j = n - 1; i < n; j = i++) {
    a += ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
  }
  return a / 2;
}

// One pass of Chaikin's corner-cutting smoothing. Each edge contributes
// two new vertices at 1/4 and 3/4 along it. Closes the ring naturally.
function chaikin(ring, passes = 1) {
  let pts = ring;
  if (pts[0][0] === pts[pts.length - 1][0] && pts[0][1] === pts[pts.length - 1][1]) {
    pts = pts.slice(0, -1);
  }
  for (let p = 0; p < passes; p++) {
    const out = [];
    const n = pts.length;
    for (let i = 0; i < n; i++) {
      const a = pts[i], b = pts[(i + 1) % n];
      out.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]]);
      out.push([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
    }
    pts = out;
  }
  pts.push(pts[0]);  // close the ring
  return pts;
}


// ── Wait for the stage to have a real size ──────────────────────────
async function waitForStage(stage) {
  for (let i = 0; i < 20; i++) {
    const w = stage.clientWidth, h = stage.clientHeight;
    if (w > 0 && h > 0) return { w, h };
    await new Promise((r) => requestAnimationFrame(r));
  }
  return { w: stage.clientWidth || 1000, h: stage.clientHeight || 700 };
}


// ── Top-level layout ────────────────────────────────────────────────
async function buildLayout(data, w, h) {
  const d3 = await loadD3();
  const d3delaunay = await loadDelaunay();
  const polygonClipping = await loadPolygonClipping();

  const sg = buildSupergraph(data);
  const squares = await layoutSupergraph(d3, sg, w, h);

  // Per-area subgraph layout, scaled to fit each square.
  const allNodes = [];
  for (const a of data.areas) {
    const square = squares.get(a.id);
    if (!square) continue;
    const members = membersOfArea(a.id, data);
    const edges = intraEdgesOfArea(members, data);
    const placed = await layoutAndFit(d3, members, edges, square);
    allNodes.push(...placed);
  }

  const polyOut = await computePolygons(d3delaunay, polygonClipping, allNodes, w, h);

  // Cross-area edges for rendering (one row per pair, weight summed).
  const memberArea = new Map(allNodes.map((n) => [n.id, n.area_id]));
  const crossW = new Map();
  function edgeKey(s, t) { return s < t ? `${s}|${t}` : `${t}|${s}`; }
  function addCross(s, t, w) {
    if (!memberArea.has(s) || !memberArea.has(t)) return;
    if (memberArea.get(s) === memberArea.get(t)) return;
    const k = edgeKey(s, t);
    crossW.set(k, (crossW.get(k) || 0) + w);
  }
  for (const e of data.fac_pers) addCross(e.source, e.target, e.w);
  for (const e of data.coauthors) addCross(e.source, e.target, e.w);
  const crossEdges = [...crossW.entries()].map(([k, w]) => {
    const [s, t] = k.split('|');
    return { source: s, target: t, w };
  });

  // Polygon centroids for label placement.
  const labels = new Map();
  for (const a of data.areas) {
    const ring = polyOut.polygons.get(a.id);
    if (!ring) continue;
    let cx = 0, cy = 0, n = 0;
    for (let i = 0; i < ring.length - 1; i++) { cx += ring[i][0]; cy += ring[i][1]; n++; }
    if (n) labels.set(a.id, { x: cx / n, y: cy / n, name: a.name, weight: a.weight });
  }

  return {
    polygons: polyOut.polygons,
    bbox: polyOut.bbox,
    nodes: allNodes,
    crossEdges,
    labels,
    areas: data.areas,
  };
}


// ── Render ─────────────────────────────────────────────────────────
async function render() {
  const statusEl = _container.querySelector('#net-status');
  const stage = _container.querySelector('#net-stage');
  if (!stage) return;
  try {
    const { w, h } = await waitForStage(stage);
    statusEl.textContent = 'Loading data…';
    const d3 = await loadD3();
    if (!_layout) {
      const data = await fetchData();
      statusEl.textContent = 'Computing knowledge map (this takes 5-10 s)…';
      _layout = await buildLayout(data, w, h);
    }
    statusEl.innerHTML = `<strong>${_layout.areas.length}</strong> research-area polygons, <strong>${_layout.nodes.length}</strong> nodes, <strong>${_layout.crossEdges.length}</strong> cross-area edges`;

    stage.innerHTML = '';
    const { x: bx, y: by, w: bw, h: bh } = _layout.bbox;
    const svg = d3.select(stage).append('svg')
      .attr('viewBox', `${bx} ${by} ${bw} ${bh}`)
      .attr('preserveAspectRatio', 'xMidYMid meet')
      .attr('class', 'mvg-svg');
    const root = svg.append('g').attr('class', 'mvg-root');

    svg.call(d3.zoom().scaleExtent([0.4, 8]).on('zoom', (ev) => {
      root.attr('transform', ev.transform);
    }));

    const tip = ensureTooltip();

    // Layer 1: polygons
    const areaList = _layout.areas;
    const colorOf = new Map(areaList.map((a, i) => [a.id, AREA_PALETTE[i % AREA_PALETTE.length]]));
    const polyG = root.append('g').attr('class', 'mvg-polys');
    polyG.selectAll('path').data(areaList).enter().append('path')
      .attr('d', (a) => {
        const ring = _layout.polygons.get(a.id);
        return ring ? `M${ring.map((p) => p.join(',')).join('L')}Z` : '';
      })
      .attr('fill', (a) => colorOf.get(a.id))
      .attr('fill-opacity', 0.18)
      .attr('stroke', (a) => colorOf.get(a.id))
      .attr('stroke-opacity', 0.9)
      .attr('stroke-width', 1.4)
      .style('cursor', 'pointer')
      .on('mouseenter', function (ev, a) {
        d3.select(this).attr('fill-opacity', 0.32);
        const lab = _layout.labels.get(a.id);
        if (lab) showTip(tip, ev, `<strong>${escapeHtml(a.name)}</strong><br><small>${a.weight} facilities</small>`);
      })
      .on('mouseleave', function () {
        d3.select(this).attr('fill-opacity', 0.18);
        hideTip(tip);
      });

    // Layer 2: cross-area edges (light gray)
    if (_showCrossEdges) {
      const nodeIdx = new Map(_layout.nodes.map((n) => [n.id, n]));
      const edgeG = root.append('g').attr('class', 'mvg-edges')
        .attr('stroke', '#94a3b8')
        .attr('stroke-opacity', 0.18)
        .attr('fill', 'none');
      edgeG.selectAll('line').data(_layout.crossEdges).enter().append('line')
        .attr('x1', (e) => (nodeIdx.get(e.source) || {}).x)
        .attr('y1', (e) => (nodeIdx.get(e.source) || {}).y)
        .attr('x2', (e) => (nodeIdx.get(e.target) || {}).x)
        .attr('y2', (e) => (nodeIdx.get(e.target) || {}).y)
        .attr('stroke-width', (e) => 0.4 + Math.log(1 + e.w) * 0.4);
    }

    // Layer 3: nodes
    const visibleNodes = _layout.nodes.filter((n) =>
      (n.kind === 'facility' && _showFacility) ||
      (n.kind === 'person' && _showPerson));
    const nodeG = root.append('g').attr('class', 'mvg-nodes');
    nodeG.selectAll('circle').data(visibleNodes).enter().append('circle')
      .attr('cx', (d) => d.x).attr('cy', (d) => d.y)
      .attr('r', (d) => NODE_RADIUS[d.kind] || 3)
      .attr('fill', (d) => NODE_COLORS[d.kind])
      .attr('stroke', '#fff')
      .attr('stroke-width', 0.6)
      .style('cursor', 'pointer')
      .on('mouseenter', (ev, d) => {
        showTip(tip, ev, nodeTipHtml(d));
      })
      .on('mouseleave', () => hideTip(tip))
      .on('click', (ev, d) => {
        const url = d.url || d.homepage_url ||
          (d.openalex_id ? `https://openalex.org/${d.openalex_id}` :
            (d.orcid ? `https://orcid.org/${d.orcid}` : null));
        if (url) window.open(url, '_blank', 'noopener');
      });

    // Layer 4: polygon labels
    const labelG = root.append('g').attr('class', 'mvg-labels')
      .attr('text-anchor', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('pointer-events', 'none');
    for (const a of areaList) {
      const lab = _layout.labels.get(a.id);
      if (!lab) continue;
      const sz = Math.max(11, Math.min(20, 7 + Math.sqrt(a.weight) * 1.6));
      labelG.append('text')
        .attr('x', lab.x).attr('y', lab.y)
        .attr('font-size', sz)
        .attr('font-weight', 600)
        .attr('fill', '#1f2937')
        .attr('stroke', '#fff')
        .attr('stroke-width', 3)
        .attr('paint-order', 'stroke')
        .text(lab.name);
    }
  } catch (err) {
    console.error('[mvg] render failed', err);
    if (statusEl) statusEl.textContent = `Knowledge map render failed: ${err.message}`;
  }
}


// ── Tooltip helpers ─────────────────────────────────────────────────
function ensureTooltip() {
  let t = _container.querySelector('.network-tooltip');
  if (!t) {
    t = document.createElement('div');
    t.className = 'network-tooltip';
    t.style.display = 'none';
    _container.appendChild(t);
  }
  return t;
}
function showTip(t, ev, html) {
  t.innerHTML = html;
  t.style.display = 'block';
  t.style.left = `${ev.clientX + 14}px`;
  t.style.top  = `${ev.clientY + 14}px`;
}
function hideTip(t) { t.style.display = 'none'; }

function nodeTipHtml(d) {
  if (d.kind === 'facility') {
    const sub = [d.acronym, d.country, (d.f_type || '').replace(/-/g, ' ')]
      .filter(Boolean).join(' · ');
    return `<strong>${escapeHtml(d.name)}</strong>` +
      (sub ? `<br><small>${escapeHtml(sub)}</small>` : '') +
      (d.url ? '<br><small style="color:#7dd3fc">click to open website</small>' : '');
  }
  const sub = [d.orcid && `ORCID ${d.orcid}`, d.openalex_id]
    .filter(Boolean).join(' · ');
  return `<strong>${escapeHtml(d.name)}</strong>` +
    (sub ? `<br><small>${escapeHtml(sub)}</small>` : '');
}
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}


// ── Public API ──────────────────────────────────────────────────────
export function initNetworkView(container) {
  _container = container;
  _container.innerHTML = `
    <div class="network-view">
      <header class="network-header">
        <div>
          <h2>Knowledge map</h2>
          <p class="network-sub">Country-like map of cod-kmap. Each polygon is one
          research area (parent-collapsed when &lt; 3 facilities); polygon size
          is proportional to facility count. Inside each polygon: facilities
          (teal) and personnel (sky blue). Light gray lines are cross-area
          edges from facility-personnel + co-author relationships, revealing
          interdisciplinary collaboration. Hover for details, click a node to
          open its homepage. Algorithm: KMap from Hossain et al. GI&nbsp;'25.</p>
        </div>
        <div class="network-actions">
          <label class="net-toggle">
            <input type="checkbox" data-toggle="facility" checked>
            <span class="net-swatch" style="background:${NODE_COLORS.facility}"></span>
            Facilities
          </label>
          <label class="net-toggle">
            <input type="checkbox" data-toggle="person" checked>
            <span class="net-swatch" style="background:${NODE_COLORS.person}"></span>
            People
          </label>
          <label class="net-toggle">
            <input type="checkbox" data-toggle="cross-edges" checked>
            Cross-area edges
          </label>
          <button id="net-restart" class="btn-ghost" title="Recompute layout from scratch">Recompute layout</button>
        </div>
      </header>
      <div id="net-status" class="network-status">Loading…</div>
      <div id="net-stage" class="network-stage"></div>
    </div>`;

  _container.querySelectorAll('.net-toggle input').forEach((el) => {
    el.addEventListener('change', () => {
      const k = el.dataset.toggle;
      if (k === 'facility') _showFacility = el.checked;
      else if (k === 'person') _showPerson = el.checked;
      else if (k === 'cross-edges') _showCrossEdges = el.checked;
      // Toggle changes don't need a re-layout — just re-render.
      render().catch((err) => console.error(err));
    });
  });
  _container.querySelector('#net-restart').addEventListener('click', () => {
    _layout = null;
    render().catch((err) => console.error(err));
  });
}

export async function renderNetworkView() {
  if (!_container) return;
  try {
    await render();
  } catch (e) {
    console.error('knowledge map render failed', e);
  }
}

export function invalidateNetworkData() {
  _layout = null;
}
