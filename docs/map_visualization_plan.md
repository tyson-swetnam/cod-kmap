# Plan: Re-visualize the Network as a country-like map (MVG)

Source paper: Hossain, Moradi, Mondal & Kobourov, *Map Visualizations for
Graphs with Group Restrictions*, GI '25 (DOI 10.1145/3769872.3769900).
Reference implementation: https://github.com/vga-usask/Map-Visualizations-For-Graphs

The paper is by the same UA group that built kmap.arizona.edu, so the
algorithms it describes are *exactly* what we want to mimic for cod-kmap.
This plan adapts their two algorithms (KMap + PCL) to our dataset and
recommends a primary grouping dimension.

---

## 1. What the paper proposes (in two paragraphs)

A **Map Visualization with Group restriction (MVG)** treats categorical
groups in a network as country-like polygonal regions: every group gets
exactly one polygon, polygon area is proportional to group size, polygon
adjacency reflects inter-group connectivity, and node positions inside a
polygon reflect intra-group structure plus pull from external connections.

The paper presents two algorithms. **KMap** is the simpler/faster one
(currently deployed at kmap.arizona.edu): build a group-group supergraph,
embed it as packed squares sized by group weight, lay each subgraph out
inside its square, then run GMap (Voronoi-based partitioning) to grow
the squares into country-shaped polygons. **PCL (Polygon Constrained
Layout)** takes the polygons KMap produces, throws away the per-square
subgraph layout, and re-distributes nodes via a custom force simulation
with three new force terms (boundary-aware central gravity, corner
gravity to fill the polygon, external gravity that pulls nodes toward
adjacent polygons they connect to). PCL produces fewer edge crossings
and better reveals between-group structure but takes ~10× longer to
converge.

---

## 2. Why this fits cod-kmap

Our current `src/views/network.js` is a generic D3 force-directed graph
with seven node kinds (network, area, type, region, funder, facility,
person) plus ring-positioned categorical anchors. It works, but at our
data scale (210 facilities + 173 people + 35 areas + 32 networks + 86
funders + 200+ regions ≈ 700 visible nodes when all toggles are on) it
becomes a hairball. Specific problems:

- No spatial coherence: groups don't sit together on the map.
- Group sizes invisible: a research area with 80 facilities looks the
  same as one with 5.
- Inter-group structure obscured: edges cross the whole canvas.
- Filter changes re-jiggle the whole layout, so users lose mental model
  between toggles.

An MVG fixes all four. The paper's KMap deployment at UA is the
existence proof — same scale, same grouped-categorical-graph problem,
similar science-collaboration domain.

---

## 3. Choice of primary grouping (the user's question)

The MVG works best when each node has **exactly one** group assignment.
Our schema has multiple categorical dimensions per facility/person, so
we need to pick one as the country-grouping. Here's the analysis:

### Option A — Research area (35 polygons) — **recommended primary**

Pro:
- **Most semantically meaningful**: researchers identify with a research
  domain (kelp forests, ocean acidification, salt marshes) more than
  with a network or funder.
- **We already have weighted assignments**: the publication-topic
  pipeline (commit `9753dea`) emits per-person weighted area scores in
  `person_areas`. We can derive a "primary area" per person via
  `argmax(weight)` and per facility via aggregating personnel + direct
  `area_links` weights.
- **Natural area-size variation**: marine-ecosystems is the biggest
  area (will be a big polygon), great-lakes / tsunamis-and-coastal-hazards
  small — produces a visually interesting cartogram.
- **Inter-area edges expose interdisciplinary collaboration** — one of
  the things the paper's UA users specifically value.

Con:
- Multi-membership ambiguity (a facility working on coral reefs AND
  ocean acidification has to live in one polygon). Resolved by always
  assigning to the highest-weighted area, but this is a real loss of
  information vs. the current multi-edge view.
- 35 polygons is on the upper end of what's readable at one zoom level.
  The paper's UA deployment uses ~226 polygons (departments) so this
  is fine, but we should consider grouping the smallest sub-areas
  (parent-child via `research_areas.parent_id`) into their parent area
  if their facility count is below a threshold (say 3).

### Option B — Network (32 polygons) — **good secondary**

Pro:
- Almost-clean single membership: each facility belongs to ≥1 network
  (LTER, NERR, NMS, IOOS RI, OOI, Sea Grant, etc.). Most facilities
  are in 1–2 networks.
- Network labels are **less ambiguous** than area labels (no fuzzy
  "primary" assignment needed).
- **Spatially coherent groupings**: NERRs all coastal, NMS all ocean,
  EPA NEPs all estuarine — the maps will tend to visually cluster by
  geography, which is intuitive.

Con:
- Less interesting analytically: networks already exist as labels
  in the current view, the map just makes them prettier.
- Some facilities aren't in any network (would need an "unaffiliated"
  catch-all polygon — ugly).

### Option C — Facility type (10 polygons) — **too few groups**

Pro: Single-membership is exact. Each facility has one `facility_type`.
Con: Only 10 groups → doesn't feel like a "map," reduces to a treemap
with curvy borders. Also redundant with current node coloring.

### Option D — Funding agency (~10 dominant funders) — **interesting third option**

Pro: Now that we have funding_events for ~56 facilities, we can derive
"primary funder" = funder with the largest sum(amount_usd) per facility.
NSF / NOAA / EPA / NIH / DOD / NPS would be the dominant polygons;
foundations and state agencies would be smaller ones. Tells a *funding
ecology* story — "where does the money come from?"
Con: Coverage gap: 154 of 210 facilities don't have funding data yet,
so they'd all sit in an "Unknown funder" polygon (ugly).

### Recommendation

**Ship `area` as the default grouping, and make the grouping a UI
toggle** so users can switch to `network`, `facility_type`, or
`funder` from the same view. This matches the paper's design philosophy
("groups must be predefined") while adapting to the multi-faceted
nature of cod-kmap. Internally each grouping computes its own
polygons + cached layout.

---

## 4. Implementation plan (phased)

### Phase 1 — Primary-group resolution (1 evening)

Add a `primary_area_id` (and friends) materialized view per facility
and per person. Algorithm for facility:

```sql
-- Facility's primary research area =
--   weighted vote across (1) direct area_links (weight 2.0)
--                        (2) personnel-mediated person_areas weights
WITH facility_area_score AS (
  SELECT al.facility_id, al.area_id, 2.0 AS score
  FROM   area_links al
  UNION ALL
  SELECT fp.facility_id, pa.area_id, pa.weight * pa.evidence_count AS score
  FROM   facility_personnel fp
  JOIN   person_areas       pa ON pa.person_id = fp.person_id
)
SELECT facility_id,
       arg_max(area_id, total_score) AS primary_area_id
FROM   (SELECT facility_id, area_id, SUM(score) AS total_score
        FROM   facility_area_score
        GROUP  BY facility_id, area_id)
GROUP  BY facility_id;
```

Repeat for `network`, `funder`, `facility_type` (the last is
already a column on `facilities`).

Output: parquet file `db/parquet/facility_primary_groups.parquet` with
one row per facility, columns `facility_id, primary_area_id,
primary_network_id, primary_funder_id, primary_facility_type`. Lets the
front end pick a grouping at render time without re-querying.

Optional refinement: parent-area collapse. For each `area_id` whose
facility-count < 3, replace its `primary_area_id` with
`research_areas.parent_id` if non-null. Keeps the polygon count
manageable (target ≈ 25 areas after collapse).

### Phase 2 — Port KMap algorithm to JS (2–3 days)

The paper's reference repo
(https://github.com/vga-usask/Map-Visualizations-For-Graphs) is Python.
Port the three steps:

**Step 1 — supergraph embedding as packed squares.**
- Build `G_c`: one supernode per group, weight = number of nodes in
  group, edge weight = number of cross-group edges between every pair.
- Lay out with d3-force using `forceManyBody` repulsion + `forceLink`
  attraction, then tag each supernode with its (x, y).
- Run a square-packing pass: each supernode becomes a square of side
  `sqrt(weight) * SCALE`, then iteratively de-overlap with the algorithm
  in Dwyer et al. (cited as [6] in the paper). d3-force has a
  `forceCollide` that approximates this; we'll use it as a starting
  point and refine if overlap remains.

**Step 2 — per-group subgraph layout, scale-and-fit.**
- For each group `g`, run an independent d3-force simulation on just
  `g`'s nodes and edges.
- Compute the bounding box of the simulated layout, scale it to fit
  the supernode's square, translate so the centers align.

**Step 3 — GMap polygon partitioning.**
- Compute Voronoi diagram over all nodes (d3-delaunay is already a
  transitive dep via D3).
- Merge Voronoi cells whose seed nodes belong to the same group →
  one polygon per group.
- Add "anchor" nodes around the perimeter (the paper's trick to avoid
  sharp outer-boundary corners), then Voronoi → unbounded outer cells
  get clipped against the anchor hull.
- Smooth polygon boundaries with one or two passes of Chaikin's algorithm
  (paper doesn't specify but UA KMap visibly uses something like this).

Output: a function `computeMVG(nodes, edges, groupKey)` that returns
`{ polygons: [...], nodes: [{id, x, y, group}], edges: [{source,target,weight}] }`.

### Phase 3 — PCL refinement (1–2 days)

After Phase 2 produces polygons, swap the per-group square-layout
positions for the PCL force simulation:

- **Modified central gravity** per polygon: gravity strength on a node
  = `kg · m_v · r̂ / d(v, O)` where `d(v, O)` is the distance from
  the node to the closest polygon-edge intersection along the line
  from v to the polygon center. Stronger pull near boundary.
- **Corner gravity**: each polygon corner exerts an attractive force
  on nodes inside the polygon. Stretches nodes to fill the polygon
  shape.
- **External gravity**: for each cross-polygon edge `(v in P, w in Q)`,
  apply an attractive pull on `v` toward `Q.center`, scaled by
  `m_w / m_v` (number of `v`'s neighbors in Q vs. in P).
- **Polygon-area-aware attraction**: target edge length =
  `sqrt(polygon_area / nodes_in_polygon)`. Replaces vanilla d3-force
  link distance.
- **Boundary-clamp** every tick: if a node ends up outside its polygon,
  project it back to the nearest interior point. The paper uses a
  "soft" version (just stronger gravity) but a hard clamp is more
  reliable for ≤10s render targets.

Make this opt-in via a toggle: KMap is fast and good for "look up
where group X lives;" PCL is slow but good for "show me the actual
collaboration structure."

### Phase 4 — New "Map" tab + interaction model (1–2 days)

- New view module `src/views/map.js` alongside the existing `network.js`.
  Same data sources; different layout algorithm.
- Tab order: Map, Network, Browse, SQL, Stats, Docs. (Map becomes the
  primary network visual; Network stays as a "raw" graph view.)
- UI controls:
  - **Grouping dropdown**: `research-area` | `network` | `funder` |
    `facility-type`. Switching re-runs the MVG pipeline and animates
    polygons.
  - **Layout dropdown**: `KMap (fast)` | `PCL (high quality)`.
  - **Filter chips**: same as Browse — country, type, area subset,
    funder subset. Filtering hides nodes but keeps polygons (greys
    them out at the appropriate proportion).
  - **Click polygon**: zoom in, fade other polygons to 30% opacity,
    show details panel (group name, member count, top funders, top
    PIs, total funding).
  - **Click node**: highlight all its edges (intra in dark, inter in
    light), show facility/person panel.
- Color: polygons inherit the existing `KIND_COLORS` palette per
  group, with a slight desaturation so labels remain readable over
  them. Polygon area-proportional → a built-in cartogram.

### Phase 5 — Performance + caching (1 day)

- **Pre-compute** the polygon set per (grouping × filter-state) and
  cache in `Map<key, MVG>`. Switching back to a previous grouping
  is instant.
- **Canvas, not SVG**, for the polygon fills + node-link edges (200+
  polygons × 700 nodes × 2k+ edges is too much DOM for SVG to stay
  60fps). Use SVG only for the highlight overlay (selected node /
  hovered polygon) and for the polygon labels.
- **Web Worker** for the actual MVG computation so the main thread
  stays responsive. Compute can take 2–10s for the full 700-node
  PCL pass.
- **Initial paint**: render KMap immediately (≤1s), then upgrade to
  PCL in the background. Same UX pattern Google Maps uses for
  road-network detail.

### Phase 6 — Validation against the paper's metrics (½ day)

Implement M1-M7 from §5.3 of the paper as a hidden debug query:

- M1 polygon convexity
- M2 drawing area coverage
- M3 polygon neighbourhood preservation
- M4 polygon area realization
- M6 polygon area coverage
- M7 edge crossings

Stress (M5) is too expensive at our node count to compute live, skip.
Print metric scores in the dev console so we can compare before/after
algorithm tweaks.

---

## 5. Decisions & trade-offs

**Single grouping vs. multi-faceted.** The paper assumes one grouping
per visualization. We explicitly support switching between groupings
because cod-kmap is multi-faceted by design — a facility's
identity is research area + network + funder + type, not just one of
those. The toggle is the deviation from the paper.

**KMap default vs. PCL default.** UA's deployment uses KMap
(per the paper). Same recommendation here for the initial paint, with
"upgrade to PCL" as a power-user toggle. PCL takes 5-10× longer to
converge and that latency is felt every time the user changes
filters.

**Replace Network tab vs. add Map tab.** Add. The existing Network
tab is good for ad-hoc analysis (toggle individual node kinds, see
*all* edges between *all* kinds). The Map tab is curated and
opinionated — one grouping, one polygon per group, optimized for
visual storytelling. Both modes have value.

**Parent-area collapse vs. keep all 35.** Lean toward collapse.
Research areas like `salt-marshes` and `tidal-wetlands` are sub-types
of `estuaries-and-wetlands`; visually merging them when their
facility-count is small reduces polygon clutter and matches how the
underlying taxonomy actually nests.

**Smithsonian / NPS / Sentinel-Site facilities with no current funding
data.** Not a blocker for the area grouping. They'll still get a
polygon assignment via their personnel's research areas. For the
funder grouping they'd land in an "Unknown funder" polygon which is
visually ugly — that's why the area grouping is the recommended
default.

---

## 6. Open questions for you (Tyson)

1. **Default grouping**: is `research-area` the right primary, or do
   you want `network` (cleaner single-membership but less analytically
   interesting)?
2. **Replace or add**: keep the Network tab AND add the Map tab, or
   replace Network entirely?
3. **PCL fidelity**: are you OK with 5-10s render latency on filter
   change, or should we ship KMap-only initially and add PCL later?
4. **Parent-area collapse threshold**: collapse areas with < 3
   facilities into their parent, or keep all 35 even if some polygons
   are tiny?
5. **Scope of the first slice**: should the first cut deliver only the
   research-area grouping (smallest scope, fastest to ship), or all
   four groupings on day one?

Default answers if you don't say otherwise:
1. research-area
2. add (don't replace)
3. KMap-only initially
4. collapse < 3
5. research-area only initially; add the toggle in a follow-up commit

---

## 7. Out of scope for this plan

- **3D / hyperbolic** map projections. The paper's UA system is 2D,
  ours will be too.
- **Animated transitions** between groupings (e.g. polygons morphing
  from area→network). Beautiful but not essential for V1.
- **Map tile underlay** (real geography). The whole point of MVG is
  the geographic *metaphor* without actual geography; mixing in a
  basemap defeats the visual logic.
- **Polygon labels with road-style halos.** d3-textPath + a simple
  SVG label per polygon is plenty.

---

## 8. Estimated total effort

- Phase 1: 1 evening (~3-4 hours)
- Phase 2: 2-3 days (~16-24 hours)
- Phase 3: 1-2 days (~8-16 hours)
- Phase 4: 1-2 days (~8-16 hours)
- Phase 5: 1 day (~6-8 hours)
- Phase 6: ½ day (~4 hours)

Total: ≈ 5-8 working days for a solid V1 with both KMap and PCL,
both research-area and network groupings, and live metric validation.
A research-area-only KMap-only V0 fits in 2-3 days.
