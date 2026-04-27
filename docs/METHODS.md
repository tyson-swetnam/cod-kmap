# cod-kmap — methods

cod-kmap is a knowledge-map of coastal and marine observing
facilities and the protected-area polygons they operate within.
Every facility point is linked to the polygons that contain it, so
the database can answer questions like *"what facilities sit inside
the Florida Keys National Marine Sanctuary?"* or *"which EPA region
administers this lab?"* with a plain SQL join.

Coverage is weighted toward the United States — federal, state,
university, NGO, and protected-area managers — with secondary
coverage for Canada, Mexico, Central and South America, and the
Northern Caribbean. Everything is open source; the source repository
lives at [github.com/tyson-swetnam/cod-kmap](https://github.com/tyson-swetnam/cod-kmap).

## Live application

The map is the primary entry point. Open it in a new tab:
[https://tyson-swetnam.github.io/cod-kmap/](https://tyson-swetnam.github.io/cod-kmap/) ↗

<iframe
  src="https://tyson-swetnam.github.io/cod-kmap/#/"
  title="cod-kmap live demo"
  width="100%"
  height="560"
  loading="lazy"
  style="border:1px solid #d9e2df;border-radius:6px;background:#fafaf7;">
</iframe>

The live application has seven tabs:

- **Map** — a vector basemap with facility points colour-coded by type
  and twelve overlay layers (NERR reserves, National Estuary Programs,
  Marine Sanctuaries, Marine Monuments, NPS Coastal Units, the new
  USFWS Refuges, USFS Research Natural Areas / Experimental Forests,
  National Wilderness Preservation System, state parks and preserves,
  land-trust and NGO holdings, Ramsar sites, and EPA Regions).
- **Browse** — a sortable table of every filter-matched facility.
- **Network** — the country-style knowledge map (see the *Map
  Visualization Plan* doc).
- **People** — the researcher directory with affiliations, publication
  metrics, ORCID, OpenAlex, and Google Scholar links where available.
- **SQL** — an in-browser DuckDB-Wasm query interface against the
  full dataset.
- **Stats** — bar charts summarising the current filter set.
- **Docs** — this page set.

## What's in the dataset

| Item | Count |
|---|---:|
| Facilities (federal, state, university, NGO, protected area) | 3,500+ |
| Researchers in the People directory | 240+ |
| Networks / consortia | 32+ |
| Funders | 80+ |
| Polygon overlays | 12 |

Most of the volume comes from the coastal-terrestrial protected-area
expansion — National Wildlife Refuges, NPS units, USFS Research
Natural Areas, designated wilderness, state parks, and land-trust
preserves — added from the USGS PAD-US authoritative inventory.

## Data model (overview)

The full DDL lives in [`schema/schema.sql`](https://github.com/tyson-swetnam/cod-kmap/blob/main/schema/schema.sql).
A summary:

**Core entities**

- **`facilities`** — one row per facility, keyed by `facility_id`.
  Carries `canonical_name`, `acronym`, `parent_org`, `facility_type`,
  `country`, `region`, `hq_address`, `hq_lat`, `hq_lng`, `url`,
  `contact`, and `established` year.
- **`locations`** — per-facility points (one HQ row by default,
  plus any field stations, buoys, or vessels).
- **`funders`** + **`funding_events`** — funding organisations and
  the per-(funder, facility, award, fiscal year) records.
- **`people`** + **`facility_personnel`** — researchers and the role
  each holds at each facility.

**Vocabularies** (loaded from `schema/vocab/*.csv`)

- **`facility_types`** — slug + label per type.
- **`research_areas`** — hierarchical research themes with GCMD URIs.
- **`networks`** — observing networks, consortia, and overlay systems.

**Many-to-many links**

- **`area_links`** — facility ↔ research area.
- **`network_membership`** — facility ↔ network.

**Regions (overlay polygons as first-class records)**

- **`regions`** — one row per overlay polygon, with `name`, `acronym`,
  `kind`, `network_id`, `url`, `manager`, `designated`, `state`, and
  source attribution.
- **`region_area_links`** — region ↔ research area.
- **`facility_regions`** — derived by point-in-polygon: which
  facilities sit inside which polygons.

**Helper views** (consumed by the front end)

- **`v_facility_map`** — the map's source view (id, name, acronym,
  type, country, lat, lng, url, parent_org).
- **`v_facility_enriched`** — per-facility row with aggregated
  research areas, networks, funders, and regions.
- **`v_region_enriched`** — per-region row with contained-facility
  counts and a member list.

**Provenance**

- **`provenance`** — source URL, agent ID, retrieval date, confidence
  rating per record.
- **`ingest_runs`** — one row per ingest invocation, for
  reproducibility.

## Overlay data sources

Boundary polygons come from the authoritative GIS publishers:

| Overlay | Source | Count |
|---|---|---:|
| NERR Reserves | NOAA National Estuarine Research Reserve System | 28 |
| National Estuary Program boundaries | EPA NEP, FY2019 boundaries | 28 |
| Marine Sanctuaries | NOAA Office of National Marine Sanctuaries | 13 |
| Marine Monuments | NOAA / DOI Marine National Monuments | 4 |
| NPS Coastal Units (legacy) | NPS curated coastal sites | 44 |
| NPS Coastal Units (LRD authoritative) | NPS Land Resources Division boundaries | 144 |
| USFWS Refuges and approved boundaries | USFWS Approved Authoritative | 197 |
| USFS Research Natural Areas / Experimental Forests | USFS EDW Special Interest Management Area | 91 |
| Wilderness (coastal subset) | USFS-hosted Wilderness.net EDW | 67 |
| State parks, WMAs, preserves, aquatic preserves | USGS PAD-US 4.1 (Mang_Type = STAT) | 1,816 |
| Land-trust + NGO + private preserves | USGS PAD-US 4.1 (Mang_Type ∈ NGO, PVT) | 1,003 |
| Ramsar wetlands of international importance (US) | Wikipedia / Ramsar Convention | 40 |
| NEON Ecological Domains (context) | NEON | 20 |
| EPA Regions (context) | EPA | 10 |

Every polygon links through to the site's authoritative website
where one is published.

## Ingest pipeline

```
data/raw/R*/facilities_*.json          (one JSON per research agent)
        │
        ▼  scripts/ingest.py
        │    - dedup (URL match, fuzzy name, 5 km haversine)
        │    - geocode missing addresses (Nominatim, on-disk cache)
        │    - load vocab from schema/vocab/*.csv
        │    - INSERT OR REPLACE into facilities, locations, area_links,
        │      network_membership, funders, funding_links
        │    - call populate_regions for spatial linkage
        │
        ▼  scripts/populate_regions.py
        │    - read every public/overlays/*.geojson
        │    - INSERT 1 row per polygon into `regions`
        │    - seed `region_area_links` from per-kind heuristics
        │    - STRtree point-in-polygon: every facility × every
        │      region → `facility_regions` containment edges
        │
        ▼  scripts/export_parquet.py
             - COPY each table TO db/parquet/<table>.parquet
             - mirror to public/parquet/ for DuckDB-Wasm HTTP-range reads
             - emit public/facilities.geojson as a lightweight fallback
```

`scripts/qa.py` runs a bounding-box, enum, foreign-key, and
provenance audit and writes `data/raw/validation-report.md`.

## In-browser data access

The app first paints from a lightweight `public/facilities.geojson`
fallback so something is on the screen in under a second. In
parallel it downloads DuckDB-Wasm, opens the parquet files in
`public/parquet/` over HTTP range requests (no server needed), and
re-runs the query to pick up research areas, networks, funders, and
region membership. Once DuckDB is up, every filter change re-issues
the SQL without re-downloading any parquet chunks.

Parquets loaded into the browser DuckDB:
`facilities`, `locations`, `funders`, `funding_links`,
`research_areas`, `area_links`, `networks`, `network_membership`,
`regions`, `region_area_links`, `facility_regions`,
`people`, `facility_personnel`, `person_areas`, `person_area_metrics`,
`person_primary_groups`, `publications`, `authorship`,
`publication_topics`, `collaborations`, `provenance`.

## Deduplication

`scripts/ingest.py` merges records using a three-step check:

1. **Exact URL match.** Two records with the same canonical URL
   are always merged.
2. **Fuzzy name match.** RapidFuzz `token_set_ratio ≥ 92` across
   `canonical_name`.
3. **Proximity check.** If (1) or (2) triggered, also require a
   haversine distance < 5 km between reported HQs before merging.

When merging, each field is kept from the record with the higher
confidence rating in its `provenance` block (high > medium > low).
List-valued fields (locations, research areas, networks, funders)
are unioned.

## Tech stack

| Layer | Technology |
|---|---|
| Data storage | DuckDB + Parquet exports |
| Spatial linkage | shapely 2.x with STRtree |
| Ingest | Python 3.11; `duckdb`, `rapidfuzz`, `geopy`, `shapely` |
| In-browser query | DuckDB-Wasm via esm.sh, HTTP range reads |
| Map | MapLibre-GL 4.7.1, OpenFreeMap positron tiles |
| Front end | Vanilla JavaScript ES modules — no bundler |
| Hosting | GitHub Pages — repo root served directly |

## Known gaps and future work

- **CCAP 2010 raster land cover** — we have the metadata but the
  raster files are too large for GitHub without LFS; not yet rendered.
- **Per-NERR salt-marsh habitat** — habitat sub-types and elevation
  exist under each reserve but aren't yet exposed in a per-reserve
  drill-down UI.
- **NPS administrative regions** (the seven regional offices, not
  the coastal units) — not rendered.
- **California wetland potential** — available as a shapefile,
  not yet on the map.
- **Funder coverage** — still being filled in for non-NSF federal
  facilities.
- A handful of South American and Caribbean facilities are missing
  coordinates and don't appear on the map.

## License and attribution

Code is MIT-licensed. Data carries per-source attribution in every
overlay popup and in the repository's `LICENSE` file. Upstream
spatial archive: COMPASS-DOE/synthesis-networks (MIT). Basemap:
[OpenFreeMap](https://openfreemap.org/) positron, © OpenMapTiles,
data © OpenStreetMap contributors.
