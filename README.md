# cod-kmap

Knowledge Map for the Coastal Observatory Design — a DuckDB/DuckLake-backed directory of
coastal research organizations across North America, Latin America, and the
northern Caribbean, published as an interactive MapLibre GL + DuckDB-Wasm map
on GitHub Pages.

## What this repo contains

| Path | Purpose |
|------|---------|
| `agents/` | Specifications for the subagents that collect data, design the schema, and build the UI. Start with `agents/README.md`. |
| `schema/schema.sql` | Canonical logical schema (D1): every table, view and constraint. The lake is created column-only from it and `scripts/qa.py` enforces the constraints. |
| `schema/vocab/` | Controlled vocabularies for facility types, research areas, and networks (D3). |
| `scripts/` | Ingest, geocode, QA, and Parquet export pipeline (D2). `cod_db.py` is the one way into the DuckLake store; `export_parquet.py` is the only Parquet writer. `build_r10_from_spatial.py` harmonizes the R10 point layers into facility records; `build_web_overlays.py` bundles polygon overlays for the map. |
| `scripts/synthesis-networks/` | R Markdown analysis imported from COMPASS-DOE/synthesis-networks (see R10). |
| `data/raw/R*/` | Raw JSON produced by each research subagent (gitignored but tracked, like the Parquet: stage a new file with `git add -f`; `ingest.py` reads it). |
| `data/raw/synthesis-networks/` | Verbatim import of the COMPASS-DOE/synthesis-networks dataset (MIT). |
| `network_synth_spatial_analysis/` | GeoJSON point and polygon layers for the R10 ingest and map overlays (LTER, LTREB, MarineGEO, Sentinel, NERR, NEP, NMS, NPS, EPA, NEON, CCAP). |
| `index.html`, `src/` | MapLibre GL + `@duckdb/duckdb-wasm` static site (ES modules + CDN importmap — no build step). |
| `public/` | Static data the site fetches at runtime: `facilities.geojson`, Parquet tables under `parquet/`, vocab CSVs under `vocab/`, polygon overlays under `overlays/`. |
| `db/parquet/` | The canonical committed copy of every table (full person registry, db-only research tables), written by `export_parquet.py` and loaded by `rebuild_lake_from_parquet.py`. |
| `db/cod_kmap_catalog.sqlite`, `db/lake/` | The local DuckLake store of record (SQLite catalog + data files). Gitignored; rebuilt from `db/parquet/`. |
| `docs/` | Human-authored Markdown, served raw at `/docs/*.md` and rendered by the Docs tab. Start with `docs/for_ai_agents.md` and `docs/data_endpoints.md`. |
| `AGENTS.md` | Contribution rules for coding agents working in this repo. |
| `.github/workflows/` | GitHub Pages deploy (`deploy.yml`), weekly data refresh (`refresh-data.yml`) and weekly URL health sweep (`url-health.yml`). |

## Subagent pipeline

```
Wave 1  D1 schema   +  D3 vocabulary
Wave 2  R1 Federal US, R2 US universities, R3 Networks/consortia,
        R4 US state/local/NGO, R5 Canada, R6 Mexico + Central America,
        R7 South America, R8 Northern Caribbean
Wave 3  R9 Funding flows (cross-cut, runs after Wave 2)
        R10 COMPASS synthesis-networks import
Wave 4  D2 Ingest → DuckLake (db/cod_kmap_catalog.sqlite + db/lake/) → db/parquet, public/parquet
Wave 5  F1 Map, F2 Filters, F3 Data access, F4 Deploy
Wave 6  Verification + iteration
```

Each `agents/<ID>-*.md` spec describes scope, authoritative sources, inputs,
outputs, method, and known-landmark checks used by QA.

## Local development

```bash
# Python pipeline (Python >= 3.11)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt               # pins duckdb==2.0.0

python scripts/rebuild_lake_from_parquet.py   # FIRST after a clone or pull: db/parquet -> local lake
python scripts/ingest.py                      # upsert data/raw/R*/*.json into the lake
python scripts/qa.py                          # integrity + data-quality gate (non-zero on failure)
python scripts/export_parquet.py              # lake -> db/parquet, public/parquet, public/facilities.geojson
python scripts/build_web_overlays.py          # bundles polygon overlays into public/overlays

# Web UI — no build needed. Serve the repo root with any static server:
python -m http.server 5173
# then open http://localhost:5173/
```

The working database is a [DuckLake](https://ducklake.select/):
`db/cod_kmap_catalog.sqlite` (catalog) plus `db/lake/` (data files), both
gitignored. Every script opens it through `scripts/cod_db.py`, and every run
of a loader is one snapshot with a commit message, so its history is
queryable (`lake.snapshots()`, `AT (VERSION => n)`). The lake is disposable
local state: `db/parquet/*.parquet` is the committed, canonical copy of every
table, and `public/parquet/` is the filtered subset the site serves (core-tier
registry rows, no db-only tables, contact details redacted). Both are written
only by `scripts/export_parquet.py`, so the edit loop is always: change the
lake, run `qa.py`, run `export_parquet.py`. A new Parquet file must be staged
with `git add -f` (the folders are gitignored but tracked).

To experiment without touching `db/`, point any script at a scratch lake:

```bash
export COD_LAKE_CATALOG=/tmp/scratch/cat.sqlite   # data defaults to /tmp/scratch/lake
python scripts/rebuild_lake_from_parquet.py
python scripts/qa.py && python scripts/export_parquet.py   # writes /tmp/scratch/export/
```

`CLAUDE.md` ("DuckLake store", "Critical gotchas") has the details.

## Using the published data

Every table the site displays is a Parquet file served with HTTP range-request
support, so you can query one in place without downloading it:

```sql
INSTALL httpfs; LOAD httpfs;
SELECT canonical_name, country, facility_type
FROM 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/facilities.parquet'
LIMIT 10;
```

- **[`docs/data_endpoints.md`](docs/data_endpoints.md)** — every endpoint, all
  46 tables with join keys, and recipes in DuckDB, Python, R and curl.
- **[`docs/for_ai_agents.md`](docs/for_ai_agents.md)** — how agents should
  consume the site, and the trust/provenance signals to read first.
- **`/llms.txt`**, **`/llms-full.txt`** — machine-readable index and the whole
  documentation corpus in one file.
- **`/public/parquet/schema.json`** — columns, types and row counts for every
  table.

## Deployment

Push to `main`; `.github/workflows/deploy.yml` stages `index.html`,
`favicon.svg`, `src/`, `public/` and `docs/`, runs
`scripts/generate_agent_surface.py` to emit `robots.txt`, `sitemap.xml`,
`llms.txt`, `llms-full.txt` and `public/parquet/schema.json`, then publishes to
GitHub Pages. There is no Node build step — the site runs plain ES modules with
a CDN importmap for MapLibre GL and DuckDB-Wasm. The generator runs with
`--strict`, so a new page in `docs/` or a new Parquet table fails the build
until it is registered (see `AGENTS.md`).

Two scheduled workflows keep the data current, each by opening a PR rather
than pushing:

- `refresh-data.yml` (Sundays) rebuilds the lake from `db/parquet`, re-runs
  the loaders whose inputs are committed (`ingest.py --skip-geocode`,
  `build_cod_team_lake.py`, `load_coastal_datasets.py`,
  `link_dataset_facilities.py`) and the derived-table chain, then `qa.py`
  and `export_parquet.py`.
- `url-health.yml` (Mondays) HEADs every link URL into the `url_health`
  table (`check_url_health.py`), clears person homepages that are really
  facility landing pages (`url_hygiene.py`), then `qa.py` and
  `export_parquet.py`.

## External datasets

`data/raw/synthesis-networks/` mirrors the dataset released with
Myers-Pigg et al., *Advancing the understanding of coastal disturbances with a
network-of-networks approach* (Ecosphere). Source:
https://github.com/COMPASS-DOE/synthesis-networks (MIT). See
`agents/R10-synthesis-networks.md` for the integration plan and
`data/raw/synthesis-networks/UPSTREAM_README.md` for attribution.

## License

MIT — see `LICENSE`. Third-party data under `data/raw/synthesis-networks/`
retains its upstream MIT license (see `UPSTREAM_LICENSE` in that directory).
