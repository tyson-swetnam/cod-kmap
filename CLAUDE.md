# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

cod-kmap is a **two-stack** project:

1. **Python data pipeline** (`scripts/`, `schema/`, `data/`) — ingests JSON from a fleet of research subagents (`agents/R*-*.md`) and CSV/JSON seeds into a **DuckLake** store of record (DuckDB 2.0, SQLite catalog), then exports Parquet + GeoJSON to `db/parquet/` and `public/`.
2. **Static MapLibre + DuckDB-Wasm site** (`index.html`, `src/`, `public/`) — published to GitHub Pages with **no build step**. ES modules + CDN importmap; do not introduce npm/Vite.

The browser fetches `public/parquet/*.parquet` over HTTP range requests via DuckDB-Wasm, with `public/facilities.geojson` as a first-paint fallback. The browser never touches the lake.

**There is no `db/cod_kmap.duckdb` any more** (nor the separate `db/cod_team.ducklake`). The store is `db/cod_kmap_catalog.sqlite` (catalog) + `db/lake/` (data files), both gitignored, opened only through `scripts/cod_db.py`. See [DuckLake store](#ducklake-store).

## Common commands

```bash
# Python pipeline (run from repo root, in a venv; Python >= 3.11 for duckdb 2.0)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # pins duckdb==2.0.0

python scripts/rebuild_lake_from_parquet.py  # FIRST after a clone or pull — rebuild the lake from db/parquet/ (--strict: abort on a violated constraint)
python scripts/ingest.py               # data/raw/R*/*.json → upsert into the lake
python scripts/ingest.py --skip-geocode  # use .geocode_cache.json only
python scripts/ingest_supplement.py data/raw/R4/facilities_x.json  # add one file without a full ingest (exports itself; --no-export)
python scripts/qa.py                   # data-quality + integrity gate (exits non-zero on failure)
python scripts/export_parquet.py       # lake → db/parquet/* (everything), public/parquet/* (filtered), public/facilities.geojson
python scripts/lake_maintenance.py     # compact files, expire snapshots > 90 days (optional, before export)
python scripts/build_web_overlays.py   # network_synth_spatial_analysis/ → public/overlays/*.geojson + manifest.json

# Web UI
python -m http.server 5173             # then open http://localhost:5173/
```

The canonical post-edit sequence is: **mutate the lake → `qa.py` → `export_parquet.py`**. Nothing a loader, enricher or `compute_*` script writes reaches `db/parquet/` or the site until the export runs. `rebuild_db_from_parquet.py` is a deprecated shim that runs the lake rebuild; `--db` on the ported scripts is a deprecated alias of `--catalog`, and `--export-parquet` / `--skip-export` are accepted no-ops.

There is no test framework. `qa.py` is the only correctness gate; add new invariants there rather than introducing pytest. Its first database check re-enforces every PRIMARY KEY / UNIQUE / FOREIGN KEY / CHECK / NOT NULL in `schema/schema.sql`, plus the derived-table keys and soft references in `cod_db.py`, against the lake (see gotchas). It refuses an empty `facilities` table, and also checks: facility type/country/provenance/bbox, `facilities.ror` is a bare id (`^0[a-z0-9]{8}$`), the COD team / scholar / dataset / registry layers, every `mvg_node_layout.source_id` resolves to `people.person_id` or `person_registry.canonical_id`, the topic crosswalk (`check_topic_crosswalk`), `url_health`, no email in the exported `people.parquet`, every `src/**/*.js` parses as an ES module (needs `node` on PATH; skipped with a warning without it), and no `src/views` SQL returns DECIMAL/HUGEINT.

Recompute derived tables after anything that grows the publication, people or funding graph, in this order: `compute_collaborations` → `compute_person_areas` → `compute_primary_groups` → `compute_area_metrics` (it joins `facility_primary_groups` and reads `collaborations`) → `build_mvg_layout` (reads `person_primary_groups`). Each is one snapshot and writes only the lake.

## DuckLake store

- **Layout.** `db/cod_kmap_catalog.sqlite` is the DuckLake 1.1 catalog (tables, snapshots, commit messages, small inlined writes); `db/lake/` holds its parquet data files. SQLite was chosen over a DuckDB-file catalog because it has no DuckDB storage-version trap and readers never block. **Writers are serialised:** `cod_db.transaction()` holds an exclusive `flock` on `<catalog>.lock` from `BEGIN` to `COMMIT`, so a second writer waits (it prints `[lock] … waiting`). Without it two overlapping transactions could each pass `assert_integrity` and commit a duplicate key or an orphan. A write outside `transaction()` is not serialised. Snapshot ids are local and mean nothing across clones.
- **One way in.** Every script calls `cod_db.connect()` (`read_only=True` for readers and `--dry-run`, `snapshot=n` for time travel). It attaches the lake as `lake` and runs `USE lake`, so unqualified names work. It raises `LakeNotFound` rather than silently creating an empty lake; only the rebuild passes `create=True`. Never `duckdb.connect()` a file of your own.
- **Pointing at another lake** (tests, CI, scratch): `COD_LAKE_CATALOG=/path/cat.sqlite` and optionally `COD_LAKE_DATA=/path/data/` (defaults to `<catalog dir>/lake`), or `--catalog` / `--lake-data` on any script. An explicit `--catalog` ignores `$COD_LAKE_DATA`, and the rebuild refuses to pair a non-default catalog with `db/lake/`. With a non-default catalog, `export_parquet.py` (and the logs of `check_url_health.py` / `url_hygiene.py`) write under `<catalog dir>/export/` instead of the repo; pass `--out .` to publish from one deliberately. Never experiment on `db/` when a scratch lake will do. `COD_AUTHOR` sets the snapshot author (default `$USER`).
- **One run, one snapshot.** Loaders, enrichers and `compute_*` wrap their writes in `with cod_db.transaction(con, "script_name: what changed"):`, which commits once with an author and message and calls `assert_integrity(con, [tables])` before `COMMIT`, so a bad batch rolls back. A block that changes nothing commits nothing. Autocommit statements each make their own snapshot and data file; don't write outside a transaction. Network scripts (`enrich_people_*`, `fetch_funding_*`) buffer during the API walk and flush in a few transactions.
- **History.** From `python` with `sys.path.insert(0, "scripts"); import cod_db; con = cod_db.connect(read_only=True)`:
  ```sql
  SELECT snapshot_id, snapshot_time, author, commit_message FROM lake.snapshots() ORDER BY 1 DESC;
  SELECT * FROM facilities AT (VERSION => 12) WHERE facility_id = '…';            -- table as of snapshot 12
  SELECT change_type, person_id, role FROM ducklake_table_changes('lake', 'main', 'facility_personnel', 12, 15);
  ```
  `cod_db.connect(snapshot=12)` attaches the whole lake read-only at that snapshot. To restore a table, `DELETE` + `INSERT … SELECT … AT (VERSION => n)` in one transaction; `CREATE OR REPLACE TABLE … AS SELECT … AT (…)` drops NOT NULL/DEFAULT.
- **Maintenance.** `scripts/lake_maintenance.py` flushes inlined rows, merges small files, folds delete files, expires snapshots older than `--older-than-days` (default 90; `--no-expire`, `--dry-run`) and deletes unreferenced files. It fails if any table's row count or content hash changes. Expired snapshots can no longer be time-travelled to. Don't run it while another process writes.
- **The lake is disposable; `db/parquet/` is not.** `db/parquet/*.parquet` is the canonical committed copy: every table, including the archive tier of `person_registry` and the db-only research tables (`coauthor_pairs_raw`, `coauthor_unmatched_population`, `ror_resolution_cache`, `split_identity_findings`). `public/parquet/` is the filtered publish target: no db-only tables, core-tier registry rows only (`export_parquet.EXPORT_WHERE`). Both are written **only** by `export_parquet.py`. Unexported lake changes are lost on a rebuild. `cod_db.ensure_schema(con)` adds new `schema.sql` tables/columns/views in place so a schema edit doesn't force a rebuild.

## Critical gotchas

- **Rebuild before anything that opens the lake.** After a clone or pull, run `scripts/rebuild_lake_from_parquet.py`; every other script raises `LakeNotFound` on a fresh clone, and a lake older than the pulled parquet would export stale data over it. It builds from `db/parquet/` by default. `--parquet public/parquet` is for experiments only: a lake built from the filtered copy loses the archive tier and db-only tables on its next export. A spec-1.1 catalog needs DuckDB 2.0; DuckDB 1.x clients cannot open it. Exported parquet still reads in DuckDB 1.1+. The ducklake and sqlite extensions are INSTALLed from `extensions.duckdb.org` on first use, then LOAD offline.

- **Constraints are logical, enforced by QA, not at write time.** DuckLake rejects PRIMARY KEY, UNIQUE, FOREIGN KEY, CHECK, indexes and sequences. `schema/schema.sql` keeps them all, but is executed only in an in-memory "spec" database; the lake is created column-only (types, NOT NULL, DEFAULT — those *are* enforced). `qa.py` (via `cod_db.integrity_violations`) fails on any duplicate key, orphan reference, failed CHECK or column drift from `schema.sql`, and writers call `cod_db.assert_integrity(con, tables)` before COMMIT. The same check covers the derived tables' keys (`cod_db.DERIVED_CONSTRAINTS`) and the references `schema.sql` leaves undeclared (`cod_db.SOFT_REFERENCES`: COD team, scholar, dataset and registry back-references). An unknown table name passed to `assert_integrity` raises. Nothing blocks an `UPDATE` of a referenced key any more — renaming a key orphans its children silently until QA catches it.

- **Upserts are `MERGE`, never `INSERT OR REPLACE` / `INSERT OR IGNORE` / `ON CONFLICT`.** Those need a primary key and fail on the lake. Use `cod_db.merge_upsert(con, table, key_cols, source, …)` (`insert_only=True` for insert-if-absent, `coalesce=` to keep existing non-NULLs). A `MERGE` whose source repeats a key inserts both rows; `merge_upsert` de-duplicates with `DISTINCT ON`. Stage per-row loops in a TEMP table and merge once. Derived tables use `cod_db.replace_table`.

- **Stage through `cod_db.create_temp_like`, never an empty CTAS.** In DuckDB 2.0.0 a DuckDB-native (TEMP or in-memory) table created inside a transaction by `CREATE TEMP TABLE s AS SELECT … LIMIT 0` and then filled with ≥ 122,880 rows (one row group) by `INSERT … SELECT` reads back as **0 rows** — whether the source is a lake table, a TEMP table or a registered Arrow/pandas object — so a `merge_upsert` from it silently writes nothing. `executemany` fills, DuckLake tables and tables declared with explicit column DDL are unaffected; `create_temp_like(con, name, select_sql)` builds that DDL from `DESCRIBE` (commit `58d2914`).

- **DuckDB 2.0 Python API.** `Relation.fetchone()` is removed (use `cod_db.scalar()` / `cod_db.rows()`); arrow lambdas fail (`lambda x: x + 1`, not `x -> x + 1`); `con.execute(q).pl()` / `.arrow()` are removed (use `con.sql(q).pl()` / `.to_arrow_table()`). `execute().fetchone()` then another `execute()` is fine, but a relation-API call (`.sql`, `.table`, `.register`) on a connection with a half-read `execute()` result raises `ResourceInUseException`. Reading a `TIMESTAMPTZ` into Python needs `pytz`.

- **Browser SQL is DuckDB 1.5 SQL.** `index.html` pins `@duckdb/duckdb-wasm@1.33.1-dev65.0` (core v1.5.6; no stable 2.0 wasm exists). Anything in `src/` or that `src/db.js` re-creates must run on 1.5, and `qa.py` fails if a `src/views` query returns DECIMAL/HUGEINT.

- **Views don't survive parquet export.** `schema/schema.sql` defines 15 views; the rebuild and `ensure_schema` create them in the lake, and `src/db.js` re-creates only **nine** of them in the browser (`v_facility_map`, `v_region_enriched`, `v_facility_funding_by_year`, `v_funder_funding_by_year`, `v_facility_key_personnel`, `v_funding_ledger`, `v_person_enriched`, `v_person_areas_enriched`, `v_cod_team_enriched`) — lazily, on first use of the SQL tab. Add a new web-facing view in **both** places or the SQL tab will lose it. The other five exist only in a local lake (the 15th, `funding_links`, is materialised by the export and ships as a parquet *table*; the rebuild keeps it as a view and does not load the file); `docs/for_ai_agents.md` publishes that three-way split, so keep it in step. `scripts/generate_agent_surface.py` derives the list by parsing the `helperViews` array, so it cannot drift on its own.

- **`export_parquet.py` hard-fails on a missing table and is byte-stable.** Every table in `TABLES` is checked before any file is written; a missing one aborts the export with nothing written (`ALLOW_MISSING` is the explicit exception list, empty today). Rows are ordered by key then all columns, every column is written OPTIONAL, the GeoJSON is ordered by id, and files are written via temp + rename. Re-exporting unchanged data gives byte-identical files on any machine, so a diff in `db/` or `public/` is a real change. Files are ZSTD level 3, not DuckDB's default Snappy: its bundled Snappy takes CPU-specific paths, so this ARM box and the x86 CI runners wrote different bytes for identical pages, and a CI refresh rewrote every file. ZSTD was checked byte-identical across x86_64 and aarch64. A new lake table must be added to `TABLES` (and to `DB_ONLY` / `EXPORT_WHERE` if it should not ship whole), or it is reported as not published. Keep derived numeric columns in the types the parquet already holds (cast SUMs to DOUBLE, not HUGEINT/DECIMAL) or a computed lake exports different bytes from a rebuilt one.

- **Contact details are not published.** `people.email` is not written by the loaders (it still feeds the `person_id` hash; the addresses live in the committed seed CSVs), `export_parquet.EXPORT_REDACT` blanks it in **both** outputs, and `qa.py` fails if an exported `people.parquet` carries one. Views and SQL-tab examples must not select it.

- **The OpenAlex topic crosswalk must be copied from data, never typed.** `data/vocab_crosswalk/openalex_to_area.csv` maps OpenAlex topic/concept ids to `research_areas`; `compute_person_areas.py` and `compute_area_metrics.py` join on the id alone. Its first version had recalled ids ("Kelp" carried the id of "Global warming", "Ecosystem" that of "Ecology") and 11,154 tags on 7,329 papers were scored through a wrong id. Take id and name together from `publication_topics`, and quote labels with commas; `qa.py` `check_topic_crosswalk` fails on a row without 5 fields, an unknown area, or an id whose label disagrees with the data.

- **URL hygiene is URL-keyed, never name-keyed.** `scripts/check_url_health.py` HEADs every link (`facilities.url`, `people.homepage_url`, `coastal_datasets.homepage_url`/`.source_url`, `dataset_endpoints.url`) into the `url_health` table plus `data/seed/url_health_check.csv`. `scripts/url_hygiene.py` nulls a `people.homepage_url` only when it is a facility landing page (equals a `facilities.url`, or matches `HOMEPAGE_PATTERNS` and is not in `KEEP_URLS`), logs each decision in `data/seed/url_hygiene_log.csv`, and the UI then falls back to the facility URL. Both run weekly in `url-health.yml`; their CSVs are the reviewable artifact.

- **Arrow LIST/STRUCT columns are not plain JS arrays.** DuckDB-Wasm (1.29 through 1.33.x) returns Arrow Vectors that have `.length` but fail `Array.isArray()`. Always run row results through `arrowToPlain()` / `unwrapRow()` from `src/db.js` before downstream view code touches them — see the comment block in that file for the trap this fixed.

- **Map sources need `Feature` shapes, not raw rows.** `query()` in `src/db.js` wraps each row in `{ type: 'Feature', geometry: { type: 'Point', coordinates: [...] }, properties: ... }` and drops rows with null coordinates. MapLibre silently skips rows that lack `geometry`.

- **Vocabularies are duplicated** between `schema/vocab/` (canonical, used by ingest/QA) and `public/vocab/` (served to the browser for filter labels). Keep them in sync — commit `c08fd86` fixed exactly this drift. `ingest.py` reloads every vocab table from `schema/vocab/*.csv`.

- **The agent surface is generated, and CI fails on drift.** `scripts/generate_agent_surface.py` writes `robots.txt`, `sitemap.xml`, `llms.txt`, `llms-full.txt` and `public/parquet/schema.json` into `_site` at deploy time (it needs `duckdb` only to read Parquet column types — `deploy.yml` installs `duckdb==2.0.0`; without it everything else still builds). `deploy.yml` runs it with `--strict`, so a **new page in `docs/` breaks the build** until it is added to `DOC_META` in that script AND to `DOC_PAGES` in `src/views/docs.js`; a **new Parquet table breaks it** until it is added to `TABLE_GROUPS`. That is deliberate — it is what keeps the published index honest. Nothing it emits is committed; don't hand-edit `llms.txt`.

- **Don't add YAML frontmatter to `docs/*.md`.** `mdToHtml` in `src/views/docs.js` has no frontmatter handling: `---` becomes an `<hr>` and the YAML renders as visible paragraphs on the Docs tab. Page metadata lives in `DOC_META` instead.

- **`COMMIT_*.sh` are one-shot driver scripts**, gitignored, not source. Don't read them as documentation of current state — they are historical commit drivers.

## Pipeline / wave model

Subagents are organized in waves; each `agents/<ID>-*.md` declares scope, sources, inputs, outputs, method, and known-landmark QA checks. The agent specs predate the lake and still name `db/cod_kmap.duckdb`; read that as "the lake".

```
Wave 1  D1 schema  + D3 vocabulary           → schema/, schema/vocab/
Wave 2  R1..R8 regional research agents      → data/raw/R*/facilities_*.json
Wave 3  R9 funding-flows, R10 COMPASS sites  → data/raw/R9/, data/raw/R10/
Wave 4  D2 ingest pipeline                   → DuckLake (db/cod_kmap_catalog.sqlite + db/lake/) → db/parquet/, public/parquet/
Wave 5  F1..F4 frontend + deploy             → src/, public/, .github/workflows/deploy.yml
Wave 6  verification + iteration
```

Every research record must conform to the shared facility JSON schema documented in `agents/README.md` (record_id, canonical_name, facility_type from `schema/vocab/facility_types.csv`, ISO-2 country, hq + locations, research_areas slugs, networks, funders with `relation`, **provenance with source_url + confidence**). D2 dedupes by facility_id, URL, and fuzzy-name + 5 km haversine. `ingest.py` globs `data/raw/R*` (the raw JSON is gitignored but tracked: a new file needs `git add -f`), upserts into an existing lake, normalises `ror` to the bare id, and leaves facilities absent from `data/raw` — and the parquet-only layers (people, registry, R11 protected areas) — alone.

Beyond facilities, the schema and pipeline now cover: regions (overlay polygons + `facility_regions` containment edges), people (`facility_personnel`, `publications`, `authorship`, `person_areas`, `collaborations`), and precomputed MVG groupings (`facility_primary_groups`, `person_primary_groups`, area metrics). The authoritative table lists are `TABLES` in `scripts/export_parquet.py` (export side) and the `tables` array in `src/db.js` (frontend side).

Three further layers are **CSV/JSON-seeded rather than agent-researched** — they have no `agents/` spec and never touch `data/raw/` (see `docs/team_scholars_datasets_methods.md`):

| Layer | Seed | Build script | Tab |
|---|---|---|---|
| COD project org chart | `data/seed/cod_wbs.csv`, `data/seed/cod_team_members.csv` | `scripts/build_cod_team_lake.py` | `/org` |
| Coastal-science scholar roster | `data/seed/community_scholars_seed.json`, `data/datasets/coastal_topics.csv` | `scripts/build_community_scholars.py` (`--seed` offline / `--harvest` needs OpenAlex) | `/people` (scholar cohort) |
| Curated dataset catalogue | `data/datasets/coastal_datasets.json` | `scripts/load_coastal_datasets.py` then `scripts/link_dataset_facilities.py` | `/data` |

- **The COD team layer lives in the main lake.** `scripts/build_cod_team_lake.py` writes `cod_wbs` and `cod_team_members` into the one DuckLake (the separate `db/cod_team.ducklake` + `db/ducklake_data/` and its plain-table fallback are gone), in one transaction per run, and merges named members into `people` (identifiers and notes are fill-only; email is never written). A table is rewritten only when its staged rows differ, with one `INSERT … SELECT` per table, so an unchanged seed commits no snapshot. Roster history is `SELECT … FROM lake.snapshots() WHERE commit_message LIKE 'build_cod_team_lake:%'` (or `--history`); `AT (VERSION => n)` reads an old roster. Publish with `export_parquet.py`.
- **Never resolve a person by name alone.** `scripts/wipe_bad_openalex_attributions.py` and `wipe_medicine_attributions.py` exist because a name-only OpenAlex resolver attached cardiologists to marine labs. New code links on ORCID or `openalex_id` equality only; the scholar harvest additionally gates on a coastal-topic share (≥10 works and ≥15% of output). A missing identifier is far better than a wrong one — leave seed ID cells blank.
- **`person_id` formula is `sha1(f"{name.lower()}|{orcid}|{email.lower()}")[:16]`** (`scripts/load_facility_personnel.py:54`). Two older scripts use a different formula; use this one for anything new.
- **New parquet must be staged with `git add -f`** — `db/parquet/*.parquet` and `public/parquet/*.parquet` are gitignored but tracked, so a new file is silently skipped and the live site 404s while everything works locally. The data workflows stage with `git add -f` for the same reason.
- **New `qa.py` invariants for these tables must be gated on the table being non-empty**, so QA stays meaningful on a lake that does not carry a layer (a scratch lake from a partial parquet folder). The original reason — `refresh-data.yml` running an `ingest.py` that re-executed `schema.sql` and emptied them — is gone: the workflow rebuilds from `db/parquet/` first and `ingest.py` upserts. Some `qa.py` docstrings still give the old reason.

## Frontend layout

- `index.html` — importmap pulls `maplibre-gl` + `@duckdb/duckdb-wasm` (pinned `1.33.1-dev65.0`, core v1.5.6) from esm.sh; loads `src/main.js` as a module.
- `src/main.js` — bootstraps map, filters, overlays, hash-router, then 7 views.
- `src/db.js` — DuckDB-Wasm init (30 s timeout; `whenReady` always resolves), parquet view registration (each table individually try/caught), helper views, `query()` (returns GeoJSON Features), and the Arrow→JS unwrap helpers.
- `src/map.js` — `TYPE_COLORS` is the single source of truth for facility-type colours (must match polygon overlay colours in `public/overlays/manifest.json`).
- `src/overlays.js` — lazy-loads polygon layers via `public/overlays/manifest.json`. `DEFAULT_OFF` controls first-paint visibility (heavy / cluttering layers default off). `src/overlay-facets.js` hides the overlays a network/type filter excludes (`syncOverlaysToFilters`, with a manual override); `node scripts/test_overlay_facets.mjs` tests it.
- `src/views/{list,stats,docs,network,people,orgchart,datasets,sql}.js` — one per top-tab. `/docs` reads markdown from `docs/` at runtime. `people.js` is the single human roster (its source is `person_registry`); it replaced the former `people.js` + `team.js` + `scholars.js` trio, and `orgchart.js` (#/org) keeps only the WBS hierarchy that a roster filter cannot express.

## CI workflows

- `deploy.yml` (push to `main`) — stages only `index.html`, `favicon.svg`, `src/`, `public/`, `docs/`, adds `.nojekyll`, runs `generate_agent_surface.py --strict` (Python 3.12, `duckdb==2.0.0`). Anything outside those paths (agents, scripts, schema, data/raw) is **not** on the live site.
- `refresh-data.yml` (weekly, Sun 06:00 UTC) — rebuild lake from `db/parquet` → `ingest.py --skip-geocode` → `build_cod_team_lake.py` → `load_coastal_datasets.py` → `link_dataset_facilities.py` → the derived-table recompute chain above → `qa.py` → `export_parquet.py`, then a PR with `db/parquet`, `public/parquet` and `public/facilities.geojson`. No network enrichers run.
- `url-health.yml` (weekly, Mon 07:00 UTC) — rebuild lake from `db/parquet` → `check_url_health.py` → `url_hygiene.py` → `qa.py` → `export_parquet.py`, then a PR including the two `data/seed/url_*.csv` logs.

Both data workflows run Python 3.12, rebuild the lake from scratch each run (only pip is cached; the ducklake/sqlite extensions download from extensions.duckdb.org), and open PRs rather than pushing, using plain `git add -f` + `gh pr create` (not `peter-evans/create-pull-request`, whose plain `git add` skipped new ignored parquet). The loaders they re-run commit nothing when their inputs are unchanged and the export is byte-stable, so a week with no input change opens no PR. The PR step needs the repo setting "Allow GitHub Actions to create and approve pull requests". GitHub disables a scheduled workflow after 60 days without repo activity; re-enable with `gh workflow enable "Refresh data"`.

## External datasets

- `data/raw/synthesis-networks/` is a verbatim MIT-licensed snapshot of [COMPASS-DOE/synthesis-networks](https://github.com/COMPASS-DOE/synthesis-networks). Do **not** edit these files; treat them as upstream. R10 (`scripts/build_r10_from_spatial.py`) derives `data/raw/R10/facilities_synthesis_networks.json` from `network_synth_spatial_analysis/` GeoJSON layers.
- `network_synth_spatial_analysis/coastal_protected/` is gitignored except for the bundled outputs that `build_web_overlays.py` produces.
