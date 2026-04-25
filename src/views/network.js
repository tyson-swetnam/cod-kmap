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

// Layout tuning. Polygon area must be roughly proportional to area
// weight (n_facilities), so we size the supernode squares as
// side = SUPERNODE_SCALE * sqrt(weight) — a true cartogram.
//
// CRITICAL: with the previous (small) SUPERNODE_SCALE + a 50 px floor,
// dense areas (coastal-processes 70 facilities packed tightly) ended
// up with TINY Voronoi cells while sparse areas (great-lakes 2
// facilities far apart) got HUGE cells — visually inverted from the
// cartogram metric. Boosted scale to 24 and dropped the floor; we
// now also pepper interior 'decoration anchor' points inside every
// area's square so Voronoi cells tile the full square area, not just
// the immediate neighbourhood of real nodes. Result: polygon area
// closely tracks sqrt(weight)² = weight, as the paper intends.
const SUPERGRAPH_TICKS = 400;
const SUBGRAPH_TICKS   = 120;
const SUPER_PADDING    = 14;     // px gap between adjacent squares
const PERIMETER_PAD    = 0.18;   // anchor ring at 1+pad of layout bbox half-width
const PERIMETER_NODES  = 18;     // outer anchors around the entire layout
const SUPERNODE_SCALE  = 24;     // side = scale * sqrt(weight)
const SUPERNODE_MIN    = 28;     // minimum side so 1-facility areas remain visible
const DECOR_GRID       = 5;      // 5×5 = 25 decoration anchors per area square
const DECOR_JITTER     = 0.18;   // ±18% random jitter so cell boundaries aren't gridlike


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

    // One row per person with primary area + their importance metrics.
    // Importance combines:
    //   - n_pubs      : SUM(n_publications) across all areas the person
    //                   has work in (from person_area_metrics)
    //   - n_coauth    : SUM(n_co_authors)  across all areas
    //   - facility_funding_usd
    //                 : SUM(facility_area_funding.total_usd_nominal)
    //                   across every facility the person works at —
    //                   their "associated funding base" (a person at
    //                   WHOI gets WHOI's $1.5B, a person at a small
    //                   NEP gets ~$5M).
    // Used downstream for node-radius scaling so prolific +
    // well-funded researchers are visually larger.
    people: `
      WITH per_pa AS (
        SELECT person_id,
               SUM(n_publications)     AS n_pubs,
               SUM(n_co_authors)       AS n_coauth,
               SUM(total_citations)    AS total_citations
        FROM person_area_metrics
        GROUP BY person_id
      ),
      per_fund AS (
        SELECT fp.person_id,
               SUM(faf.total_usd_nominal) AS facility_funding_usd
        FROM facility_personnel fp
        JOIN facility_area_funding faf ON faf.facility_id = fp.facility_id
        GROUP BY fp.person_id
      )
      SELECT p.person_id AS id,
             p.name,
             p.orcid,
             p.openalex_id,
             p.homepage_url,
             g.primary_area_id        AS area_id,
             COALESCE(pa.n_pubs, 0)   AS n_pubs,
             COALESCE(pa.n_coauth, 0) AS n_coauth,
             COALESCE(pa.total_citations, 0) AS total_citations,
             COALESCE(pf.facility_funding_usd, 0) AS facility_funding_usd
      FROM   people p
      JOIN   person_primary_groups g ON g.person_id = p.person_id
      LEFT  JOIN per_pa  pa ON pa.person_id = p.person_id
      LEFT  JOIN per_fund pf ON pf.person_id = p.person_id
      WHERE  g.primary_area_id IS NOT NULL`,

    // Facility ↔ person via facility_personnel (intra+inter polygon).
    fac_pers: `
      SELECT facility_id AS source, person_id AS target,
             COUNT(*) AS w
      FROM   facility_personnel
      GROUP  BY facility_id, person_id`,

    // Per-person role/title/institution lookup for tooltips. A person
    // can hold roles at multiple facilities — we list-aggregate so the
    // tooltip can show each affiliation. Prefer key-personnel rows so
    // 'Director' / 'Principal Investigator' surfaces above 'Staff'.
    person_affiliations: `
      SELECT fp.person_id,
             list(struct_pack(
               role        := fp.role,
               title       := fp.title,
               facility_id := f.facility_id,
               facility    := COALESCE(f.acronym || ' — ' || f.canonical_name,
                                       f.canonical_name),
               is_key      := fp.is_key_personnel
             ) ORDER BY fp.is_key_personnel DESC, fp.role) AS roles
      FROM facility_personnel fp
      JOIN facilities f ON f.facility_id = fp.facility_id
      GROUP BY fp.person_id`,

    // Person → primary facility for the hierarchy layout. A person
    // might work at >1 facility; we pick their first key-personnel
    // row, falling back to alphabetic role if no key-flag set.
    person_primary_facility: `
      WITH ranked AS (
        SELECT person_id, facility_id,
               ROW_NUMBER() OVER (
                 PARTITION BY person_id
                 ORDER BY is_key_personnel DESC, role, facility_id
               ) AS rk
        FROM facility_personnel
      )
      SELECT person_id, facility_id
      FROM ranked WHERE rk = 1`,

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

  // Build lookup tables for hierarchy + tooltip enrichment.
  const affilsBy = new Map(out.person_affiliations.map(
    (r) => [r.person_id, r.roles || []]));
  const primaryFacBy = new Map(out.person_primary_facility.map(
    (r) => [r.person_id, r.facility_id]));
  for (const p of out.people) {
    p.affiliations = affilsBy.get(p.id) || [];
    p.primary_facility_id = primaryFacBy.get(p.id) || null;
  }
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
      // True cartogram: side ∝ sqrt(weight) so AREA ∝ weight.
      // Min side just keeps 1-facility areas visible at all zoom levels.
      side: Math.max(SUPERNODE_MIN, SUPERNODE_SCALE * Math.sqrt(a.weight)),
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
    .map((p) => {
      // Composite "importance" weight per the user's request:
      // prioritize funding + collaborators, then publications.
      // Coefficients chosen so a well-funded heavy collaborator (~$50M
      // facility, 30 co-authors, 50 pubs) lands around weight ≈ 18,
      // while a junior researcher (no funding, 0 co-authors, 5 pubs)
      // lands at ≈ 2.2 — both visible, but very different sizes.
      const fundM = (p.facility_funding_usd || 0) / 1e6;
      const w = 0.6 * Math.sqrt(p.n_pubs || 0)
              + 1.2 * Math.sqrt(p.n_coauth || 0)
              + 0.7 * Math.sqrt(fundM);
      return {
        id: p.id, name: p.name, kind: 'person',
        orcid: p.orcid, openalex_id: p.openalex_id,
        homepage_url: p.homepage_url, area_id: areaId,
        n_pubs: p.n_pubs, n_coauth: p.n_coauth,
        total_citations: p.total_citations,
        facility_funding_usd: p.facility_funding_usd,
        importance: w,
      };
    });
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

// Decoration anchors per area: invisible nodes that own Voronoi cells
// inside the area's square, ensuring the resulting merged polygon
// closely matches the square's area (cartogram-correct sizing) instead
// of letting cells leak into sparse neighbours. Tagged with the area
// id so polygon-clipping rolls them up; tagged kind='__decor' so the
// renderer skips them.
function decorationAnchors(square, areaId) {
  const cx = square.x, cy = square.y;
  const half = square.side / 2;
  const out = [];
  const N = DECOR_GRID;
  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      // Cell-center (i+0.5)/N spans 0..1; map to ±half.
      const fx = (i + 0.5) / N - 0.5;
      const fy = (j + 0.5) / N - 0.5;
      // Random jitter so the resulting cell boundaries are irregular,
      // not a visible grid pattern. PRNG seeded by (area,i,j) so the
      // layout is reproducible across re-renders.
      const seed = (areaId.charCodeAt(0) || 0) * 31 + i * 7 + j * 13;
      const jx = (((seed * 9301 + 49297) % 233280) / 233280 - 0.5) * 2;
      const jy = (((seed * 4391 + 12347) % 233280) / 233280 - 0.5) * 2;
      const x = cx + (fx + jx * DECOR_JITTER) * 2 * half;
      const y = cy + (fy + jy * DECOR_JITTER) * 2 * half;
      out.push({
        id: `__decor_${areaId}_${i}_${j}`,
        kind: '__decor',
        area_id: areaId,
        x, y,
      });
    }
  }
  return out;
}

// Pack facility sub-circles inside an area's square, then scatter
// each facility's people inside the corresponding circle. Returns a
// flat list of (facility nodes + person nodes + decoration anchors)
// that the Voronoi step consumes. A side-effect map tracks each
// facility's circle position + radius so the renderer can draw the
// translucent sub-polygon ring per institution.
async function layoutAndFit(d3, members, edges, square, facCircles) {
  // Even an empty area gets decoration anchors so its polygon
  // still appears at the right cartogram size.
  if (!members.length) {
    return decorationAnchors(square, square.id);
  }

  const cx = square.x, cy = square.y;
  const facs = members.filter((m) => m.kind === 'facility');
  const peo  = members.filter((m) => m.kind === 'person');

  // ── 1. Pack facility sub-circles inside the square ──────────────
  // Each facility's "weight" = 1 (itself) + n_personnel-at-facility,
  // so an institution with many researchers gets a larger sub-circle.
  // Radius ∝ sqrt(weight) to make AREA ∝ weight.
  const peopleAt = new Map();
  for (const p of peo) {
    if (!p.primary_facility_id) continue;
    peopleAt.set(p.primary_facility_id,
      (peopleAt.get(p.primary_facility_id) || 0) + 1);
  }

  // If facility-list is empty (rare; can happen if all primary_area
  // facilities have no personnel listed), invent a single phantom
  // circle covering the whole square so people still get placed.
  const bubbles = facs.length
    ? facs.map((f) => ({
        id: f.id, name: f.name, acronym: f.acronym, country: f.country,
        f_type: f.f_type, url: f.url, area_id: f.area_id,
        weight: 1 + (peopleAt.get(f.id) || 0),
        kind: 'facility',
      }))
    : [{ id: `__phantom_${square.id}`, name: '', kind: 'facility',
         area_id: square.id, weight: 1 }];

  const totalWeight = bubbles.reduce((s, b) => s + Math.sqrt(b.weight), 0);
  const innerR = (square.side / 2) * 0.84;  // 16% inset from square edge
  // Per-bubble radius. Min 5 px so single-person facilities are visible;
  // max ~innerR so a giant institution can't dwarf the whole area.
  const RFAC = 0.62 * innerR / Math.max(totalWeight, 1);
  for (const b of bubbles) {
    b.r = Math.min(innerR * 0.65, Math.max(5, RFAC * Math.sqrt(b.weight) * 1.5));
    // Seed at a random point inside the inner circle.
    const a = Math.random() * 2 * Math.PI;
    const r = Math.random() * (innerR - b.r);
    b.x = cx + r * Math.cos(a);
    b.y = cy + r * Math.sin(a);
  }
  const bubSim = d3.forceSimulation(bubbles)
    .alphaDecay(0.05)
    .force('center', d3.forceCenter(cx, cy).strength(0.08))
    .force('charge', d3.forceManyBody().strength(-12))
    .force('collide',
      d3.forceCollide().radius((d) => d.r + 1.6).strength(1).iterations(2))
    .stop();
  for (let i = 0; i < 220; i++) bubSim.tick();

  // Clamp every bubble back inside the inner circle (the simulation
  // doesn't enforce containment); push toward center if it's drifted
  // outside. A few iterations because pushing one bubble can shove
  // its neighbour out.
  for (let pass = 0; pass < 30; pass++) {
    let moved = false;
    for (const b of bubbles) {
      const dx = b.x - cx, dy = b.y - cy;
      const d = Math.hypot(dx, dy) || 1e-6;
      const overshoot = d + b.r - innerR;
      if (overshoot > 0) {
        const k = (innerR - b.r) / d;
        b.x = cx + dx * k;
        b.y = cy + dy * k;
        moved = true;
      }
    }
    // Also re-resolve overlap via simple push.
    for (let i = 0; i < bubbles.length; i++) {
      for (let j = i + 1; j < bubbles.length; j++) {
        const a = bubbles[i], b = bubbles[j];
        const dx = b.x - a.x, dy = b.y - a.y;
        const minD = a.r + b.r + 1.6;
        const d = Math.hypot(dx, dy) || 1e-6;
        if (d < minD) {
          const push = (minD - d) / 2;
          const nx = dx / d, ny = dy / d;
          a.x -= nx * push; a.y -= ny * push;
          b.x += nx * push; b.y += ny * push;
          moved = true;
        }
      }
    }
    if (!moved) break;
  }

  // Record the bubble layout in the side-effect map for the renderer.
  for (const b of bubbles) {
    facCircles.set(b.id, { x: b.x, y: b.y, r: b.r,
                            area_id: square.id,
                            name: b.name, acronym: b.acronym,
                            country: b.country, f_type: b.f_type,
                            url: b.url,
                            n_people: peopleAt.get(b.id) || 0 });
  }

  // ── 2. Place each facility node at its bubble center; people inside ──
  for (const f of facs) {
    const b = facCircles.get(f.id);
    if (b) { f.x = b.x; f.y = b.y; }
  }
  // People scattered inside their primary facility's circle. Use a
  // golden-angle spiral so positions are deterministic and even.
  const PHI = Math.PI * (3 - Math.sqrt(5));   // golden angle
  const peoPerFac = new Map();
  for (const p of peo) {
    const fid = p.primary_facility_id;
    const b = (fid && facCircles.get(fid)) || facCircles.get(bubbles[0].id);
    if (!b) continue;
    const k = peoPerFac.get(b) || 0;
    peoPerFac.set(b, k + 1);
    const n = (peopleAt.get(fid) || 1);
    // Deterministic spiral inside the bubble's inner 70%.
    const t = (k + 0.5) / Math.max(n, 1);
    const r = b.r * 0.62 * Math.sqrt(t);
    const a = (k + 1) * PHI;
    p.x = b.x + r * Math.cos(a);
    p.y = b.y + r * Math.sin(a);
  }

  // ── 3. Append decoration anchors so Voronoi tiles the area square ──
  // Anchors live in the gap between facility bubbles + the square
  // perimeter. They share the area_id so the outer polygon stretches
  // to the full square; they do NOT carry a facility_id, so they
  // don't end up inside any facility's sub-polygon should we ever
  // compute one.
  return [...facs, ...peo, ...decorationAnchors(square, square.id)];
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

  // Per-area subgraph layout. Now hierarchical — facilities are
  // packed as sub-circles inside each area's square, and people sit
  // inside their primary facility's circle. The facCircles map is
  // populated as a side-effect for the renderer.
  const facCircles = new Map();
  const allNodes = [];
  for (const a of data.areas) {
    const square = squares.get(a.id);
    if (!square) continue;
    const members = membersOfArea(a.id, data);
    const edges = intraEdgesOfArea(members, data);
    const placed = await layoutAndFit(d3, members, edges, square, facCircles);
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
    facCircles,
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

    // Layer 1.5: facility sub-circles inside each area polygon.
    // Renders as translucent rings so users see the institution
    // hierarchy without obscuring the people inside. Drawn only when
    // the Facilities toggle is on.
    if (_showFacility && _layout.facCircles && _layout.facCircles.size) {
      const circleData = [..._layout.facCircles.entries()]
        .filter(([, c]) => !String(c.area_id).startsWith('__phantom_'))
        .map(([id, c]) => ({ id, ...c }));
      root.append('g').attr('class', 'mvg-facircles')
        .selectAll('circle').data(circleData).enter().append('circle')
        .attr('cx', (d) => d.x).attr('cy', (d) => d.y)
        .attr('r', (d) => d.r)
        .attr('fill', (d) => colorOf.get(d.area_id) || '#94a3b8')
        .attr('fill-opacity', 0.10)
        .attr('stroke', (d) => colorOf.get(d.area_id) || '#64748b')
        .attr('stroke-opacity', 0.55)
        .attr('stroke-width', 1)
        .attr('stroke-dasharray', '2,2')
        .style('cursor', 'pointer')
        .on('mouseenter', function (ev, d) {
          d3.select(this).attr('fill-opacity', 0.22);
          showTip(tip, ev, facilityCircleTipHtml(d));
        })
        .on('mouseleave', function () {
          d3.select(this).attr('fill-opacity', 0.10);
          hideTip(tip);
        })
        .on('click', (ev, d) => {
          if (d.url) window.open(d.url, '_blank', 'noopener');
        });
    }

    // Layer 2: cross-area edges. Visibility tied to the same
    // Facilities / People toggles that control node visibility:
    //   - Facility ↔ facility edges shown when Facilities is on
    //   - Person-bridging edges (person↔person + person↔facility)
    //     shown when People is on
    // Person-bridging layer is drawn ON TOP in sky-blue so the
    // interdisciplinary collaboration signal pops.
    const nodeIdx = new Map(_layout.nodes.map((n) => [n.id, n]));
    const isPersonEdge = (e) => {
      const a = nodeIdx.get(e.source); const b = nodeIdx.get(e.target);
      return (a && a.kind === 'person') || (b && b.kind === 'person');
    };
    const facEdges = _layout.crossEdges.filter((e) => !isPersonEdge(e));
    const perEdges = _layout.crossEdges.filter(isPersonEdge);

    if (_showFacility && facEdges.length) {
      root.append('g').attr('class', 'mvg-edges-fac')
        .attr('stroke', '#94a3b8')
        .attr('stroke-opacity', 0.18)
        .attr('fill', 'none')
        .selectAll('line').data(facEdges).enter().append('line')
        .attr('x1', (e) => (nodeIdx.get(e.source) || {}).x)
        .attr('y1', (e) => (nodeIdx.get(e.source) || {}).y)
        .attr('x2', (e) => (nodeIdx.get(e.target) || {}).x)
        .attr('y2', (e) => (nodeIdx.get(e.target) || {}).y)
        .attr('stroke-width', (e) => 0.35 + Math.log(1 + e.w) * 0.3);
    }
    if (_showPerson && perEdges.length) {
      root.append('g').attr('class', 'mvg-edges-per')
        .attr('stroke', '#0ea5e9')
        .attr('stroke-opacity', 0.55)
        .attr('fill', 'none')
        .selectAll('line').data(perEdges).enter().append('line')
        .attr('x1', (e) => (nodeIdx.get(e.source) || {}).x)
        .attr('y1', (e) => (nodeIdx.get(e.source) || {}).y)
        .attr('x2', (e) => (nodeIdx.get(e.target) || {}).x)
        .attr('y2', (e) => (nodeIdx.get(e.target) || {}).y)
        .attr('stroke-width', (e) => 0.6 + Math.log(1 + e.w) * 0.6);
    }

    // Layer 3: nodes. Person radii scale with the composite importance
    // weight (funding base × co-authors × publications) so prominent
    // researchers are visually large; facility radii stay constant
    // (they're already represented by the polygon sizing).
    const visibleNodes = _layout.nodes.filter((n) =>
      (n.kind === 'facility' && _showFacility) ||
      (n.kind === 'person' && _showPerson));
    const personRadius = (d) => {
      // weight ranges roughly 0..25; map to 3..10 px via sqrt curve.
      const w = d.importance || 0;
      return Math.min(10, 3 + Math.sqrt(w) * 1.0);
    };
    const nodeG = root.append('g').attr('class', 'mvg-nodes');
    nodeG.selectAll('circle').data(visibleNodes).enter().append('circle')
      .attr('cx', (d) => d.x).attr('cy', (d) => d.y)
      .attr('r', (d) => d.kind === 'person'
        ? personRadius(d)
        : (NODE_RADIUS[d.kind] || 3))
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
  // Person tooltip: name + role(s) + institution(s), then the
  // metrics that drove their node size. We deliberately omit
  // person_id / openalex_id / orcid from the visible chrome — those
  // are used only for the click-through link below.
  const lines = [`<strong>${escapeHtml(d.name)}</strong>`];

  const affils = Array.isArray(d.affiliations) ? d.affiliations : [];
  if (affils.length) {
    // Show up to 2 affiliations; collapse the rest into "+N more".
    const shown = affils.slice(0, 2);
    for (const a of shown) {
      const role = a.title || a.role || '';
      const fac  = a.facility || '';
      lines.push(`<small>${escapeHtml(role)}${role && fac ? '<br>' : ''}${escapeHtml(fac)}</small>`);
    }
    if (affils.length > shown.length) {
      lines.push(`<small style="color:#94a3b8">+${affils.length - shown.length} more affiliation${affils.length - shown.length === 1 ? '' : 's'}</small>`);
    }
  }

  const metrics = [];
  if (d.n_pubs)   metrics.push(`${d.n_pubs} pubs`);
  if (d.n_coauth) metrics.push(`${d.n_coauth} co-authors`);
  if (d.facility_funding_usd) {
    const m = d.facility_funding_usd / 1e6;
    metrics.push(`$${m >= 100 ? Math.round(m) : m.toFixed(1)}M facility funding`);
  }
  if (metrics.length) {
    lines.push(`<small style="color:#7dd3fc">${metrics.join(' · ')}</small>`);
  }
  return lines.join('<br>');
}

function facilityCircleTipHtml(c) {
  const sub = [c.acronym, c.country, (c.f_type || '').replace(/-/g, ' ')]
    .filter(Boolean).join(' · ');
  const peopleLine = c.n_people
    ? `<br><small style="color:#7dd3fc">${c.n_people} researcher${c.n_people === 1 ? '' : 's'} mapped here</small>`
    : '';
  return `<strong>${escapeHtml(c.name || c.id)}</strong>` +
    (sub ? `<br><small>${escapeHtml(sub)}</small>` : '') +
    peopleLine +
    (c.url ? '<br><small style="color:#7dd3fc">click to open website</small>' : '');
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
          <p class="network-sub">Country-like map of cod-kmap. Each outer
          polygon is one research area (parent-collapsed when &lt; 3 facilities);
          polygon area is proportional to facility count. Inside each area,
          dashed sub-circles are individual institutions sized by their
          personnel count; researchers (sky-blue dots, sized by funding +
          collaborators + publications) sit inside their primary institution.
          Toggling Facilities or People also toggles their cross-area edges:
          gray lines = facility-facility shared programs, sky-blue lines =
          researchers bridging two areas (interdisciplinary potential).
          Hover for details, click to open homepage / ORCID. Algorithm:
          KMap from Hossain et al. GI&nbsp;'25 with hierarchical institution
          sub-polygons.</p>
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
