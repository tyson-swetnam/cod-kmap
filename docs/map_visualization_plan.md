# Knowledge map — visualization design

The **Network** tab in cod-kmap renders the dataset as a country-style
map: each research area is a polygon, polygon area is proportional to
the number of facilities working in that area, and facilities and
researchers sit inside the polygon they belong to. Edges between
polygons reveal interdisciplinary collaboration.

This page documents the design choices and the algorithm behind the
view.

## What the layout shows

A **Map Visualization with Group restriction (MVG)** treats categorical
groups in a network as polygonal regions:

- Each group gets exactly one polygon.
- Polygon area is proportional to group size (facility count).
- Polygon adjacency reflects connectivity between groups.
- Node positions inside a polygon reflect intra-group structure plus
  pull from external collaborations.

The algorithm follows Hossain, Moradi, Mondal & Kobourov, *Map
Visualizations for Graphs with Group Restrictions*, Graphics Interface
2025 ([DOI 10.1145/3769872.3769900](https://doi.org/10.1145/3769872.3769900)).
A reference implementation by the same University of Arizona group
powers [kmap.arizona.edu](https://kmap.arizona.edu).

## Choice of grouping

Researchers and facilities have multiple categorical attributes. We
chose **research area** (35 polygons) as the default grouping for
three reasons:

- It is the most semantically meaningful grouping for science users.
- We have weighted assignments per person and facility from publication
  topics.
- Polygon sizes vary naturally — *marine ecosystems* dominates,
  *Great Lakes* and *tsunamis-and-coastal-hazards* are small —
  producing a visually informative cartogram.

Other dimensions are exposed as a grouping toggle:

| Grouping        | Polygons | Notes |
|-----------------|---------:|-------|
| Research area   | 35       | Default. Most analytically meaningful. |
| Network         | 32       | Single-membership; clean borders. |
| Facility type   | 10       | Fewer polygons; useful for high-level summary. |
| Funding agency  | ~10      | Surfaces the funding landscape per facility. |

## Algorithm

The layout is computed in three steps:

1. **Supergraph embedding.** Build one super-node per group (weight =
   number of nodes in the group, edge weight = number of cross-group
   links). Embed with a force layout, then pack each super-node as a
   square sized by the square root of its weight.
2. **Per-group subgraph layout.** Run an independent force simulation
   on the nodes inside each group, then scale and translate to fit
   inside its square.
3. **Polygon partitioning.** Compute a Voronoi diagram over all nodes
   and merge cells that share a group, producing one polygon per
   group. Smooth the boundaries with a single Chaikin pass.

A higher-fidelity refinement (PCL — Polygon-Constrained Layout) adds
boundary-aware central gravity, corner gravity, and external gravity
toward adjacent polygons. PCL produces fewer edge crossings but
takes ~10× longer to converge; we expose it as a power-user toggle.

## Interaction

- **Click a polygon** to zoom in and dim the others; a panel shows the
  group name, member count, top funders, top researchers, and total
  funding.
- **Click a node** to highlight all its edges (intra-group dark,
  cross-group light) and open the facility or researcher card.
- **Filter chips** (country, type, area subset, funder subset) hide
  matching nodes while preserving the polygon shapes — so changing a
  filter doesn't re-jiggle the layout.

## Performance

The full graph (~700 nodes when every toggle is on) renders in under
a second with the default fast layout, then upgrades to PCL in the
background. Polygon fills and node-link edges render to a `<canvas>`;
selection / hover state and polygon labels render to SVG on top.
Switching back to a previous grouping reads from a per-grouping cache
and is instant.

## Open design questions

- Should the layout collapse small sub-areas into their parent area
  (e.g. *salt-marshes* + *tidal-wetlands* under *estuaries-and-
  wetlands*) when the per-area facility count is below a threshold?
  The current view keeps all 35 areas; collapsing reduces clutter at
  the cost of taxonomic detail.
- Should filter-driven hiding shrink the polygon for the filtered
  group, or grey it out at proportional opacity? Today: grey it out.
