# cod-kmap Methods

## Overview

**cod-kmap** is a knowledge-map of coastal and marine observing facilities and
the overlay polygons they operate within — National Marine Sanctuaries, Marine
National Monuments, National Estuarine Research Reserves, National Estuary
Programs, NPS coastal units, NEON ecological domains, and EPA administrative
regions. Every point is linked to the polygons that contain it, so the
database can answer questions like "what facilities sit inside the Florida
Keys NMS?" or "which EPA region administers this lab?" with a plain SQL join.

Coverage is weighted toward the United States (federal, state, university,
NGO) with secondary coverage for Canada, Mexico, Central and South America,
and the Northern Caribbean. Everything is open source; see the [GitHub
repo](https://github.com/tyson-swetnam/cod-kmap).

---

## The web application

The site has four tabs in the top bar.

### Map

The default view. Renders a MapLibre-GL vector map (OpenFreeMap positron
basemap) with two classes of data stacked on top:

- **Facility points** — one colored dot per facility, colored by
  `facility_type` (federal, state, local-gov, university-marine-lab,
  university-institute, nonprofit, foundation, network, international-*,
  industry, vessel, observatory, virtual). Click for a popup that shows
  the full name, acronym, type, country, parent org, research areas,
  networks, overlay regions the facility sits inside, and funders.
- **Overlay polygons** — seven layers toggled from the left sidebar under
  **Map overlays**:
  - Coastal boundaries: *NERR reserves*, *National Estuary Program*
  - Marine protected areas: *Marine Sanctuaries*, *Marine Monuments*,
    *NPS Coastal Units*
  - Context layers: *NEON Ecological Domains*, *EPA Regions*
  Click any polygon for a popup with the full canonical name, acronym,
  designating year, manager, state, a description, and a link to the
  site's authoritative website.

Below the map, a collapsible **"Facilities in view"** panel lists every
facility whose HQ coordinate sits inside the current map viewport. Pan or
zoom, and the count + list update automatically. This is the fastest way
to drill into a region: filter by type in the sidebar, zoom to a
coastline, and the bottom panel shows you exactly what's there.

Controls in the upper-right:
- **+ / −** MapLibre zoom buttons.
- **⛶ Fit** zooms the camera to the bounding box of the currently-matched
  facilities.

A collapsible **Legend** sits in the lower-left: facility type colors on
top, currently-visible overlay colors below.

### Browse

A sortable table of every filter-matched facility — name, acronym, type,
country, parent org, URL. Click a column header to sort ascending /
descending; click a row to open the facility's website. Useful when you
want a flat catalog rather than a map.

### Stats

Bar charts summarizing the current filter-matched set, grouped by:
- Facility type
- Country
- Research area (only populated when the DuckDB-Wasm query has loaded;
  the fallback GeoJSON doesn't carry research_areas).

### Docs

This page.

---

## Filters + viewport semantics

The left sidebar holds the facility filters: **Search**, **Research area**,
**Network**, **Facility type**, **Country / territory**. Every checkbox
change drives a single `refresh()` that:
1. Re-runs the DuckDB-Wasm query with the new filter predicate.
2. Replaces the MapLibre source data so the map redraws immediately.
3. Updates the Browse table and Stats charts (so switching tabs is
   instant, regardless of which tab you were on when you filtered).
4. Recomputes the "Facilities in view" bottom panel.

Clicking **Clear all filters** resets only the facility facets — it leaves
your overlay toggles alone.

Two independent planes of visibility apply:

- **Filter match** — does a facility pass the current sidebar predicate?
- **Viewport match** — does its HQ point fall inside the current map
  bounds?

The sidebar status bar shows the filter-match count. The bottom panel
header shows the viewport-match count (a subset of the filter match).
Zooming or panning updates only the latter.

---

## Data model

Canonical DDL is in `schema/schema.sql`. Summary:

**Core entities**

- **facilities** — one row per facility, keyed by `facility_id`.
  Columns: `canonical_name`, `acronym`, `parent_org`, `facility_type`,
  `country`, `region`, `hq_address`, `hq_lat`, `hq_lng`, `url`, `contact`,
  `established`, `created_at`.
- **locations** — per-facility points (one HQ row by default, plus any
  additional field stations / buoys / vessels).
- **funders** + **funding_links** — funding organizations and the M:N
  edges to facilities.

**Vocabularies** (seeded from `schema/vocab/*.csv`)

- **facility_types** — slug + label for each facility type.
- **research_areas** — hierarchical research themes (physical
  oceanography, marine ecosystems, coastal processes, etc.) with GCMD
  URIs where available.
- **networks** — observing networks, consortia, and overlay systems
  (IOOS, OOI, LTER, NERRS, NMS, NEP, NEON, marine-monument, nps-coastal,
  epa-region, …).

**Many-to-many links**

- **area_links** — facility ↔ research_area.
- **network_membership** — facility ↔ network.

**Regions (overlay polygons as first-class records)**

- **regions** — one row per overlay polygon, keyed by
  `region_id = hash(network_id || lower(name))`. Carries the same kind of
  metadata a facility does: `name`, `acronym`, `kind`
  (sanctuary/monument/nerr-reserve/nep-program/nps-unit/neon-domain/
  epa-region), `network_id` (FK to networks), `url`, `manager`,
  `designated`, `state`, `description`, `source_file`, `source`.
- **region_area_links** — region ↔ research_area, many-to-many.
- **facility_regions** — derived by point-in-polygon; one row per
  (facility, region) containment with `relation = 'within'` and
  `distance_km = 0.0`. This is what powers the *Inside:* row in facility
  popups.

**Helper views** (web UI reads these)

- **v_facility_map** — the map's source view: id, name, acronym, type,
  country, lat, lng, url, parent_org.
- **v_facility_enriched** — one row per facility with aggregated
  research areas, networks, funders, *and regions*.
- **v_region_enriched** — one row per region with aggregated research
  areas, contained facilities, and a facility count.

**Provenance**

- **provenance** — source URL, agent ID, date retrieved, confidence.
- **ingest_runs** — one row per ingest invocation, for reproducibility.

---

## Overlay data sources

All polygon overlays are derived from the COMPASS-DOE/synthesis-networks
spatial archive bundled under `network_synth_spatial_analysis/` (MIT
licensed). `scripts/build_web_overlays.py` extracts, simplifies, and
re-projects each layer to GeoJSON under `public/overlays/`.
`scripts/enrich_overlays.py` then merges fragmented same-name polygons
into single MultiPolygons and adds the curated per-site metadata (full
name, acronym, website, manager, designating year, description).

| Overlay | Source |
|---|---|
| Marine Sanctuaries (13) | `Land_Cover/NMS_boundaries/` |
| Marine Monuments (4) | `MarineMonuments/Monuments.shp` |
| NERR Reserves (28) | `SH_ALL_RB/GIS_Process/<acronym>/` (latest year per reserve) |
| NEP Programs (28) | `NEP_BoundariesFY19/NEP_Boundaries2019.shp` |
| NPS Coastal Units (44) | `MPAI_MarineNationalParks/NPS.shp` |
| NEON Domains (20) | `Land_Cover/NEON_domains/NEONDomains_0/` |
| EPA Regions (10) | `EPA_Locations/EPA_Regions__Region_Boundaries.shp` |

Every polygon links through to the site's authoritative website; see
e.g. Hawaiian Islands Humpback Whale NMS →
[`hawaiihumpbackwhale.noaa.gov`](https://hawaiihumpbackwhale.noaa.gov/).

---

## Subagent pipeline

Facility records are collected by a pool of research "agents" (each one
is a prompted subtask that emits a validated JSON file). Each agent
writes to `data/raw/<ID>/facilities_*.json`; `scripts/ingest.py`
deduplicates across all agents using a three-step check (URL match →
fuzzy name match → haversine distance < 5 km) and picks the
highest-confidence fields during merge.

| ID | Role |
|----|------|
| R1 | US federal agencies (NOAA, USGS, NASA, BOEM, EPA, USACE, plus the 10 EPA regional HQ offices) |
| R2 | US university marine labs and institutes |
| R3 | Multi-institutional networks and consortia (IOOS, OOI, LTER) |
| R4 | US state/local agencies and NGOs |
| R5 | Canadian facilities (DFO, universities) |
| R6 | Mexico and Central America |
| R7 | South American coastal observatories |
| R8 | Northern Caribbean island facilities |
| R9 | Funding flows and funder-facility linkages |
| D1 | Schema — `schema/schema.sql` |
| D2 | Ingestion — `scripts/ingest.py` + `scripts/populate_regions.py` |
| D3 | Controlled vocabularies — `schema/vocab/` |
| F1 | MapLibre vector map — layers, popups, type colors |
| F2 | Sidebar filter widgets + viewport-linked bottom panel |
| F3 | Data access — DuckDB-Wasm over parquet + GeoJSON fallback |
| F4 | GitHub Pages deployment, hard cached-bundle invalidation |

---

## Ingest pipeline

```
data/raw/R*/facilities_*.json
        │
        ▼  scripts/ingest.py
        │    - dedup (URL / fuzzy name / 5 km haversine)
        │    - geocode missing addresses (Nominatim, on-disk cache)
        │    - load vocab from schema/vocab/*.csv
        │    - INSERT OR REPLACE into facilities / locations / area_links /
        │      network_membership / funders / funding_links
        │    - (if shapely installed) call populate_regions
        │
        ▼  scripts/populate_regions.py
        │    - read every public/overlays/*.geojson
        │    - insert 1 row per polygon into `regions`
        │    - seed `region_area_links` from per-kind heuristics
        │    - STRtree point-in-polygon: every facility × every region
        │      → `facility_regions` containment edges
        │
        ▼  scripts/export_parquet.py
             - COPY each table TO db/parquet/<table>.parquet
             - mirror to public/parquet/ for DuckDB-Wasm HTTP-range reads
             - emit public/facilities.geojson as a lightweight fallback
```

`scripts/qa.py` runs a bounding-box / enum / FK / provenance audit and
writes `data/raw/validation-report.md`.

---

## In-browser data access

The app first paints from a lightweight `public/facilities.geojson`
fallback so something is on the screen in under a second. In parallel, it
downloads DuckDB-Wasm, opens the parquet files in `public/parquet/` over
HTTP range requests (no server needed), and re-runs the query to pick up
research areas, networks, funders, and region membership that aren't in
the lightweight fallback. Once DuckDB is up, every filter change
re-issues the full SQL without re-downloading any parquet chunks.

Parquets loaded into the browser DuckDB:
`facilities, locations, funders, funding_links, research_areas,
area_links, networks, network_membership, regions, region_area_links,
facility_regions`.

---

## Deduplication details

`scripts/ingest.py`'s merge logic:

1. **Exact URL match** — two records with the same canonical `url` are
   always merged.
2. **Fuzzy name match** — RapidFuzz `token_set_ratio ≥ 92` across
   `canonical_name`.
3. **Proximity check** — if (1) or (2) triggered, also require
   haversine distance between reported HQs < 5 km before merging.

When merging, each field is kept from the record with the higher
confidence value in its `provenance` block (high > medium > low).
List-valued fields (`locations`, `research_areas`, `networks`,
`funders`) are unioned.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Data storage | DuckDB (local `db/cod_kmap.duckdb`) + Parquet exports |
| Spatial linkage | [shapely](https://shapely.readthedocs.io/) 2.x with STRtree |
| Ingest | Python 3.11, `duckdb`, `rapidfuzz`, `geopy`, `shapely` |
| In-browser query | DuckDB-Wasm via esm.sh CDN, HTTP range reads |
| Map | MapLibre-GL 4.7.1, OpenFreeMap positron tiles |
| Front end | Vanilla JS ES modules, no bundler |
| Hosting | GitHub Pages — repo root served directly |

---

## Known gaps + future work

- **CCAP 2010 raster land cover** — `network_synth_spatial_analysis/Job510705_2010_CCAP/`
  has the `.img` sidecars but the raster files themselves are
  gitignored (too large for GitHub without LFS). Not yet rendered as a
  raster tile layer.
- **Per-NERR salt-marsh habitat** — `network_synth_spatial_analysis/SH_ALL_RB/`
  has habitat subtypes + elevation under each reserve. We currently
  consume only the reserve boundary; habitat layers would need a
  per-reserve drill-down UI.
- **NPS administrative regions** (7 regional offices, distinct from NPS
  coastal units). Not rendered.
- **California wetland potential** — `network_synth_spatial_analysis/Land_Cover/ca_wetland_potential/`.
  Available as a shapefile; not yet on the map.
- **Funder coverage** is still thin; R9 is gradually filling in
  `funding_links`.
- A handful of facilities in R7/R8 (South America + Caribbean) are
  missing coordinates and do not appear on the map.

---

## License + attribution

Code: MIT. Data: see per-source attribution in every overlay popup and
in the repo's `LICENSE` and `network_synth_spatial_analysis/` license
files. Upstream spatial archive: COMPASS-DOE/synthesis-networks (MIT).
Basemap: [OpenFreeMap](https://openfreemap.org/) positron, © OpenMapTiles,
data © OpenStreetMap contributors.
