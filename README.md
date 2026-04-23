# cod-kmap

Knowledge Map for the Coastal Observatory Design — a DuckDB-backed directory of
coastal research organizations across North America, Latin America, and the
northern Caribbean, published as an interactive Leaflet map on GitHub Pages.

## What this repo contains

| Path | Purpose |
|------|---------|
| `agents/` | Specifications for the subagents that collect data, design the schema, and build the UI. Start with `agents/README.md`. |
| `schema/schema.sql` | Canonical DuckDB schema (D1). |
| `schema/vocab/` | Controlled vocabularies for facility types, research areas, and networks (D3). |
| `scripts/` | Ingest, geocode, QA, and Parquet export pipeline (D2). `build_r10_from_spatial.py` harmonizes the R10 point layers into facility records; `build_web_overlays.py` bundles polygon overlays for the map. |
| `scripts/synthesis-networks/` | R Markdown analysis imported from COMPASS-DOE/synthesis-networks (see R10). |
| `data/raw/R*/` | Raw JSON produced by each research subagent (gitignored except `.gitkeep`). |
| `data/raw/synthesis-networks/` | Verbatim import of the COMPASS-DOE/synthesis-networks dataset (MIT). |
| `network_synth_spatial_analysis/` | GeoJSON point and polygon layers for the R10 ingest and map overlays (LTER, LTREB, MarineGEO, Sentinel, NERR, NEP, NMS, NPS, EPA, NEON, CCAP). |
| `web/public/overlays/` | Simplified polygon overlays the map UI can toggle on demand (NERR reserves, NEP programs, Marine Sanctuaries, Marine Monuments, NPS coastal, NEON domains, EPA regions). |
| `db/` | Built `cod_kmap.duckdb` and Parquet exports (gitignored). |
| `web/` | Vite + Leaflet + `@duckdb/duckdb-wasm` static site. |
| `.github/workflows/` | GitHub Pages deploy + weekly data-refresh workflows. |

## Subagent pipeline

```
Wave 1  D1 schema   +  D3 vocabulary
Wave 2  R1 Federal US, R2 US universities, R3 Networks/consortia,
        R4 US state/local/NGO, R5 Canada, R6 Mexico + Central America,
        R7 South America, R8 Northern Caribbean
Wave 3  R9 Funding flows (cross-cut, runs after Wave 2)
        R10 COMPASS synthesis-networks import
Wave 4  D2 Ingest → db/cod_kmap.duckdb
Wave 5  F1 Map, F2 Filters, F3 Data access, F4 Deploy
Wave 6  Verification + iteration
```

Each `agents/<ID>-*.md` spec describes scope, authoritative sources, inputs,
outputs, method, and known-landmark checks used by QA.

## Local development

```bash
# Python pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/ingest.py          # loads data/raw/R*/*.json into db/cod_kmap.duckdb
python scripts/qa.py              # runs data-quality assertions
python scripts/export_parquet.py  # writes db/parquet and web/public/parquet

# Web UI
cd web && npm install && npm run dev
```

## Deployment

Push to `main`; `.github/workflows/deploy.yml` builds `web/` and publishes to
GitHub Pages. The weekly `refresh-data.yml` workflow re-runs the ingest
pipeline and opens a PR with refreshed Parquet + GeoJSON artifacts.

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
