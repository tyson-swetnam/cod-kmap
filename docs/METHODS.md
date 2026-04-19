# cod-kmap Methods

## Overview

cod-kmap is a knowledge-map of coastal and marine observing facilities across
North America, Latin America, and the Northern Caribbean. It collects structured
metadata — location, facility type, parent organization, research focus, funding
source, and network membership — to help researchers and decision-makers
understand the landscape of observing infrastructure.

Current snapshot: **118 facilities**, **15 countries**, with coverage weighted
toward the United States (federal, state, university, and NGO) and secondary
coverage for Canada, Mexico, the Caribbean, and South America.

---

## Subagent Pipeline

Each research agent produces a Markdown brief in `agents/<ID>-*.md`.

| ID | Role |
|----|------|
| R1 | US federal agencies (NOAA, USGS, NASA, BOEM, EPA, USACE) |
| R2 | US university marine labs and institutes |
| R3 | Multi-institutional networks and consortia (IOOS, OOI, LTER) |
| R4 | US state/local agencies and NGOs |
| R5 | Canadian facilities (DFO, MBARI-equivalent, universities) |
| R6 | Mexico and Central America |
| R7 | South American coastal observatories |
| R8 | Northern Caribbean island facilities |
| R9 | Funding flows and funder-facility linkages (in progress) |
| D1 | Schema design — tables, keys, enums; see `schema/schema.sql` |
| D2 | Ingestion pipeline — `scripts/ingest.py` normalization and dedup |
| D3 | Controlled vocabularies; see `schema/vocab/` |
| F1 | Leaflet map view — clustering, popups, type colors |
| F2 | Sidebar filter widgets — facets for type, country, area, network |
| F3 | Data access — DuckDB-Wasm + GeoJSON fallback |
| F4 | Deployment — Vite + GitHub Pages, CI/CD workflow |

---

## Data Sources

**US Federal**
- NOAA CoastWatch, IOOS Program Office (oceanservice.noaa.gov)
- USGS National Water Information System (waterdata.usgs.gov)
- EPA Water Quality Portal (waterqualitydata.us)

**US University / Marine Labs**
- Woods Hole Oceanographic Institution (whoi.edu)
- Scripps Institution of Oceanography (scripps.ucsd.edu)
- MBARI (mbari.org), Horn Point Lab, Moss Landing Marine Labs

**Networks / Consortia**
- IOOS Regional Associations (ioos.us/regions)
- Ocean Observatories Initiative (oceanobservatories.org)
- US LTER Network (lternet.edu)

**International**
- Fisheries and Oceans Canada / DFO (dfo-mpo.gc.ca)
- CONABIO / CONACYT Mexico
- IOC-UNESCO Ocean Biodiversity Information System (obis.org)

---

## Data Model

Core tables (see `schema/schema.sql` for full DDL):

- **facilities** — canonical record per facility; primary key `facility_id`;
  fields: `canonical_name`, `acronym`, `facility_type`, `country`, `hq_lat`,
  `hq_lng`, `url`, `parent_org`.
- **locations** — one row per physical site; FK to `facility_id`.
- **funders** — organizations that provide funding.
- **funding_links** — M:N between facilities and funders.
- **research_areas** — controlled vocabulary of research themes.
- **area_links** — M:N between facilities and research_areas.
- **networks** — named observing networks or consortia.
- **network_membership** — M:N between facilities and networks.
- **provenance** — source URL, agent ID, date scraped for every facility row.

---

## Controlled Vocabularies

Vocab CSV files live in `schema/vocab/`:

- `facility_types.csv` — slug + label for each facility type (federal, state,
  university-marine-lab, nonprofit, network, etc.)
- `research_areas.csv` — hierarchical research themes (physical oceanography,
  biological oceanography, coastal hazards, etc.)
- `networks.csv` — recognized network names and abbreviations

---

## Deduplication and Geocoding

`scripts/ingest.py` normalizes each incoming record, then applies a three-step
deduplication check: (1) exact URL match, (2) fuzzy name match (token-sort
ratio > 88), and (3) haversine distance < 5 km between reported coordinates.
Records passing all three checks are merged; otherwise a new row is inserted.
Coordinates absent from source data are resolved via Nominatim with a local
JSON cache to avoid re-querying the same address.

---

## Verification

`scripts/qa.py` runs the following checks and writes results to
`data/raw/validation-report.md`:

- **Bbox check** — lat/lng within hemisphere bounds for the declared country
- **Enum check** — `facility_type` and `country` values exist in vocab tables
- **FK check** — every `area_links` / `funding_links` row resolves to a valid parent
- **Provenance check** — every facility has at least one provenance row

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Data storage | DuckDB (local) + Parquet exports |
| Ingestion | Python 3.11, `pandas`, `rapidfuzz`, `geopy` |
| In-browser query | DuckDB-Wasm (jsDelivr CDN) |
| Map | Leaflet 1.9 + Leaflet.markercluster |
| Front end | Vanilla JS ES modules, Vite 5 |
| Hosting | GitHub Pages via `gh-pages` branch |

---

## Known Limitations

- **Funder coverage** is thin; R9 is still in progress and most
  `funding_links` rows are absent from the current export.
- **Six facilities** from R7/R8 (South America + Caribbean) are missing
  coordinates and do not appear on the map.
- The DuckDB sandbox environment could not install the **spatial extension**
  (`LOAD spatial`), so spatial SQL queries (ST_DWithin, etc.) are not used;
  haversine distance is computed in Python instead.
- GeoJSON fallback omits `areas` and `networks` fields, so those filters have
  no effect when DuckDB-Wasm fails to load.
