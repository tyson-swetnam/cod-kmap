# cod-kmap Methods

## Overview

**cod-kmap** (Coastal Observatory Data — Knowledge Map) is a structured,
queryable inventory of coastal and marine observing facilities across North
America, Latin America, and the Northern Caribbean. It is designed for
researchers, program officers, educators, and decision-makers who need a
single reference surface for the geography of observing infrastructure,
its institutional ownership, its funding flows, and its thematic focus.

The project combines:

1. **Open data sourcing** — every record is traced back to a public website
   (agency page, university directory, consortium member list, grant
   database).
2. **A portable database** — DuckDB tables + Parquet exports, designed so the
   same schema runs in Python (ingest) and in the browser (DuckDB-Wasm).
3. **A static web map** — MapLibre GL JS + vector tiles, deployable to
   GitHub Pages with no server backend.

**Current snapshot:** **118 facilities**, **15 countries**, weighted toward
the United States (federal, state, university, NGO). Coverage for Canada,
Mexico, the Caribbean, and South America is intentionally a smaller seed
that later research waves will deepen.

---

## Project Goals

- **Single canonical record per facility** — deduplicated across acronyms,
  alternate names, and parent-org reshuffles.
- **Many-to-many relationships are first-class** — a single facility can
  belong to several networks, host several research themes, and be funded
  by several agencies simultaneously.
- **Provenance over opinion** — every fact is linked to a URL and a
  retrieval date. The same agent can correct or supersede an earlier record,
  but history is preserved in `ingest_runs`.
- **Low-friction re-use** — the DuckDB file and Parquet exports are drop-in
  replacements for anyone who wants to run their own queries, build their
  own map, or stitch the data into a larger catalog.

---

## Subagent Pipeline

The dataset is produced by a fleet of cooperating agents, each with a
narrow scope and a written brief (see `agents/<ID>-*.md`). Agents fall
into three families:

| Family | IDs | Role |
|--------|-----|------|
| Research | R1–R9 | Discover facilities and capture their metadata |
| Database | D1–D3 | Define schema, vocabularies, and ingestion rules |
| Frontend | F1–F4 | Build the user-facing map, filters, and deployment |

### Research agents

| ID | Coverage |
|----|----------|
| R1 | US federal agencies (NOAA, USGS, NASA, EPA, BOEM, USACE, USFWS, NPS, NRL, NSF) |
| R2 | US university marine labs and institutes (WHOI, Scripps, MBARI, and NAML members) |
| R3 | Multi-institutional networks and consortia (IOOS regional associations, OOI, LTER, NERRS, Sea Grant, GOOS) |
| R4 | US state/local agencies, nonprofits, and foundations |
| R5 | Canadian federal (DFO/BIO), academic, and not-for-profit facilities |
| R6 | Mexico and Central America (CICESE, UNAM-ICML, INAPESCA, STRI Panama) |
| R7 | South American coastal observatories (INVEMAR, IMARPE, INIDEP, CENPAT, FURG) |
| R8 | Northern Caribbean islands (CARICOOS PR/USVI, Bahamas, Cuba, Jamaica, DR) |
| R9 | Funding-flow cross-cut (USAspending, NSF Award Search, NIH RePORTER) |

### Database-design agents

| ID | Deliverable |
|----|-------------|
| D1 | `schema/schema.sql` — DuckDB DDL for facilities, locations, funders, research areas, networks, and provenance |
| D2 | `scripts/ingest.py`, `scripts/geocode.py`, `scripts/qa.py` — normalization, geocoding, and QA pipeline |
| D3 | `schema/vocab/*.csv` — controlled vocabularies for facility types, research areas, and networks |

### Frontend-development agents

| ID | Deliverable |
|----|-------------|
| F1 | `web/src/map.js` — MapLibre map with coastline overlay, per-facility points, and per-network hulls |
| F2 | `web/src/filters.js` — facet filters for type, country, area, network + free-text search |
| F3 | `web/src/db.js` — DuckDB-Wasm loader with GeoJSON fallback |
| F4 | `.github/workflows/deploy.yml` — GitHub Pages deploy + scheduled data refresh |

---

## Data Model

Core tables (see `schema/schema.sql` for full DDL):

- **facilities** — one canonical row per facility.
  `facility_id` PK, `canonical_name`, `acronym`, `parent_org`,
  `facility_type` (enum from vocab), `country`, `hq_lat`, `hq_lng`,
  `established_year`, `url`, `contact_email`.
- **locations** — 1..N physical sites per facility (FK `facility_id`).
- **funders** — organizations that provide funding (agencies, foundations).
- **funding_links** — M:N between facilities and funders, with optional
  `amount_usd`, `year`, and `award_id`.
- **research_areas** — controlled-vocabulary themes, hierarchical,
  mapped to GCMD Science Keywords where possible.
- **area_links** — M:N between facilities and research areas.
- **networks** — named observing networks (IOOS RAs, OOI, LTER, NERRS, etc.).
- **network_membership** — M:N between facilities and networks.
- **provenance** — one row per (facility, source_url, retrieval_date,
  agent_id, confidence). Ensures every assertion is traceable.
- **ingest_runs** — bookkeeping: timestamp, agent_id, git_sha, row counts.

---

## Controlled Vocabularies

Vocab CSV files live in `schema/vocab/`:

- **`facility_types.csv`** — slug + label + one-line description for each
  facility type. Includes: `federal-lab`, `federal-field-station`,
  `university-marine-lab`, `state-agency`, `local-gov`, `nonprofit`,
  `foundation`, `network`, `consortium`, `virtual-observatory`.
- **`research_areas.csv`** — hierarchical research themes (e.g.
  `physical-oceanography`, `biological-oceanography`, `coastal-hazards`,
  `harmful-algal-blooms`, `ocean-acidification`, `fisheries-ecology`),
  mapped to GCMD Earth Science Keywords.
- **`networks.csv`** — canonical abbreviation + full name + parent program
  for each observing network (IOOS, RAs: CARICOOS, NANOOS, SCCOOS, CeNCOOS,
  AOOS, PacIOOS, GCOOS, GLOS, MARACOOS, NERACOOS, SECOORA; OOI; LTER;
  NERRS; Sea Grant; GOOS; OBIS).

A `VERSION` file in the same folder is bumped whenever a vocab changes,
so downstream joins know whether they are working against a stale mapping.

---

## Shared JSON Record Schema

Every research agent emits records against the schema documented in
`agents/README.md`. The minimal shape:

```json
{
  "canonical_name": "NOAA Pacific Marine Environmental Laboratory",
  "acronym": "PMEL",
  "parent_org": "NOAA OAR",
  "facility_type": "federal-lab",
  "country": "US",
  "hq_address": "7600 Sand Point Way NE, Seattle, WA 98115",
  "hq_lat": 47.6825,
  "hq_lng": -122.2539,
  "established_year": 1973,
  "url": "https://www.pmel.noaa.gov/",
  "contact_email": "",
  "research_areas": ["physical-oceanography", "ocean-acidification",
                     "harmful-algal-blooms"],
  "networks": ["GOOS", "IOOS"],
  "funders": [
    { "funder": "NOAA OAR", "relation": "parent-agency",
      "amount_usd": null, "year": null, "award_id": null }
  ],
  "locations": [
    { "name": "PMEL Seattle HQ", "lat": 47.6825, "lng": -122.2539 },
    { "name": "PMEL Newport field station", "lat": 44.6253,
      "lng": -124.0636 }
  ],
  "provenance": [
    { "source_url": "https://www.pmel.noaa.gov/about",
      "retrieval_date": "2026-04-15",
      "agent_id": "R1",
      "confidence": "high" }
  ]
}
```

Agents validate their output against this schema before handing it to the
ingest pipeline.

---

## Deduplication and Geocoding

`scripts/ingest.py` normalizes each incoming record, then applies a
three-step deduplication check:

1. **Exact URL match** — the canonical `url` field after stripping
   `http(s)://`, `www.`, and trailing slashes.
2. **Fuzzy name match** — `rapidfuzz` token-sort ratio > 88 against
   `canonical_name`, with a second pass over known acronym aliases.
3. **Spatial proximity** — haversine distance < 5 km between reported
   HQ coordinates.

A record is merged into an existing row only if **all three** conditions
agree. Otherwise a new row is inserted and flagged for human review.

Coordinates absent from source data are resolved via **Nominatim** (OSM
geocoder) against the free-text `hq_address`, with a local JSON cache at
`data/cache/geocode.json` so the same address is never re-queried.
Nominatim's 1 req/sec usage policy is respected by the scripted sleep.

---

## Verification

`scripts/qa.py` runs the following checks and writes a report to
`data/raw/validation-report.md`:

- **Bbox check** — lat/lng within a hemisphere bounding box for the
  declared country. The EC bbox was widened to `(-5.1, 1.7, -92.1, -75.2)`
  so Galápagos Marine Reserve stations pass.
- **Enum check** — `facility_type`, `country`, and each entry in
  `research_areas` / `networks` exist in the vocab tables.
- **Foreign-key check** — every `area_links` / `funding_links` /
  `network_membership` row resolves to a valid parent on both sides.
- **Provenance check** — every facility has ≥ 1 provenance row with a
  live source URL and a retrieval date within the last 365 days.
- **Completeness targets** — at least 11 IOOS RAs, 30 NERRS sites, ~34 Sea
  Grant programs, ~28 NAML members, and ≥ 1 facility per coastal US state
  and per listed Latin American country.

A `duckdb db/cod_kmap.duckdb "SELECT facility_type, count(*) FROM
facilities GROUP BY 1"` smoke test is documented in the project README.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Data storage | DuckDB (local) + Parquet exports |
| Ingestion | Python 3.11, `pandas`, `rapidfuzz`, `geopy` |
| In-browser query | DuckDB-Wasm (esm.sh / jsDelivr CDN) |
| Map | MapLibre GL JS 4.x, OpenFreeMap Positron vector style |
| Overlays | Natural Earth 50m coastline GeoJSON; convex-hull polygons per network |
| Front end | Vanilla JS ES modules, Vite 5, hash-based router |
| Hosting | GitHub Pages (raw files) + GitHub Actions CI |

---

## Map Rendering Notes

The web map deliberately avoids marker clustering: coastal observatories
tend to be tightly clustered around specific ports and estuaries, and
clustering hides exactly the adjacency patterns that are interesting.
Instead, the map uses:

- Zoom-based circle radius interpolation (small dots when zoomed out,
  full-size circles near shoreline-zoom).
- Facility labels that appear only at zoom ≥ 6.
- Convex-hull polygons per network (computed client-side with a monotone
  chain algorithm) that fade out above zoom 7, leaving the individual
  points visible.
- A collapsible layers panel (top-left) so users can toggle the coastline,
  hulls, points, and labels independently.

---

## Known Limitations

- **Funder coverage** is thin; R9 is still in progress and most
  `funding_links` rows are absent from the current export.
- **Six facilities** from R7/R8 (South America + Caribbean) are missing
  HQ coordinates and do not appear on the map.
- The DuckDB sandbox environment used during development could not install
  the **spatial extension**, so spatial SQL (ST_DWithin, ST_Distance) is
  not wired in; haversine distance is computed in Python instead.
- The GeoJSON fallback path (used when DuckDB-Wasm fails to load) omits
  the `areas` and `networks` fields, so those filters have no effect in
  fallback mode.
- The dataset is a **seed**, not a census. Every research wave is scoped
  to a tractable first pass; corrections and additions are welcome via
  GitHub issues or pull requests.

---

## Roadmap

- Close out **R9** to populate `funding_links` with USAspending, NSF, and
  NIH award edges.
- Expand **R4** to cover every coastal US state's dedicated coastal
  management program and the corresponding NGO landscape.
- Backfill missing coordinates for the 6 R7/R8 records.
- Add a **publications** column (via Crossref / OpenAlex) so each facility
  links to its most-cited coastal papers.
- Migrate hulls from convex hulls to alpha-shapes for tighter network
  boundaries.
- Enable vector tile generation so the map remains responsive at 10× the
  current facility count.
