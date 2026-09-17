# Data endpoints

Every piece of data this site displays is a static file with a stable URL. This
page is the reference for all of them: what each endpoint is, how to read it,
and worked recipes in DuckDB, Python, R and plain curl.

For orientation and the trust/provenance signals, see
[For AI agents](for_ai_agents.md). For how the data was assembled, see
[Methods](METHODS.md).

## URL conventions

Everything is served under a single base:

```
BASE = https://tyson-swetnam.github.io/cod-kmap/
```

Two path prefixes matter, and they are not symmetric:

```
Documentation    BASE + docs/<file>.md                  (text/markdown)
Data             BASE + public/<...>                    (the "public/" segment survives)

  tables         BASE + public/parquet/<table>.parquet
  table schemas  BASE + public/parquet/schema.json
  map points     BASE + public/facilities.geojson
  overlays       BASE + public/overlays/<layer>.geojson
  overlay index  BASE + public/overlays/manifest.json
  vocabularies   BASE + public/vocab/<name>.csv
```

Mirrors of the documentation, for sandboxes that allow `github.com` but not
`github.io`:

```
RAW = https://raw.githubusercontent.com/tyson-swetnam/cod-kmap/main/
      RAW + docs/<file>.md
```

Note that `schema/`, `scripts/`, `agents/` and `data/` are **not** deployed —
the release workflow stages only `index.html`, `favicon.svg`, `src/`, `public/`
and `docs/`. Link those from `github.com`, never from the site.

## Documentation endpoints

Sixteen Markdown pages plus one BibTeX file. Each is fetchable at
`BASE + docs/<file>` and mirrored at `RAW + docs/<file>`. The Docs tab of the
app fetches these very files and renders them client-side, so the raw Markdown
is the source, not a derived export.

| File | Page |
| --- | --- |
| `for_ai_agents.md` | For AI agents — endpoints, trust signals, what to do if you cannot fetch this host |
| `data_endpoints.md` | This page |
| `cod_purpose_and_msi_handout.md` | Purpose and scope of the Coastal Observatory Design proposal |
| `METHODS.md` | How the dataset was assembled: sources, dedup rules, known gaps |
| `team_scholars_datasets_methods.md` | Build provenance for the org chart, people roster and dataset catalogue |
| `person_registry.md` | The identity model: `canonical_id`, tiering, and its caveats |
| `VALIDATION_REPORT.md` | Identifier-validation audit run and its findings |
| `REFERENCES.md` | The COD Zotero library as a readable list |
| `references.bib` | The same library as flat BibTeX (97 entries) |
| `reference_documents_report.md` | Inventory of COD background reading, with proposed schema extensions |
| `map_visualization_plan.md` | Design of the Network tab's MVG cartogram |
| `NETWORK_FIX_METRICS.md` | Crossing-pair metrics for a cartogram layout change |
| `funding_pipeline_plan.md` | Funding ingest design and the passes still to run (draft) |
| `suitability_roadmap.md` | Roadmap for a site-suitability layer that is not built yet (draft) |
| `personnel_gap_research_plan.md` | How facility leadership was sourced, and the NULL rule |
| `orcid_enrichment_plan.md` | The strict ORCID matcher and its accept rules |
| `google_scholar_enrichment_plan.md` | Why Google Scholar ids are mostly absent |

To read all of them in one request, fetch
[`llms-full.txt`](https://tyson-swetnam.github.io/cod-kmap/llms-full.txt).

## The Parquet tables

46 tables, 23.18 MB in total, one Parquet file each. GitHub Pages honours HTTP
range requests, so a Parquet client reads the footer and only the column chunks
a query touches — you do not download a table to query it.

The authoritative, always-current list of columns and types is
[`public/parquet/schema.json`](https://tyson-swetnam.github.io/cod-kmap/public/parquet/schema.json),
regenerated from the actual files on every deploy;
[`llms.txt`](https://tyson-swetnam.github.io/cod-kmap/llms.txt) carries the same
column lists in prose. The catalogue below is the curated view: row counts,
sizes and the join keys you need. **Row counts and sizes are from the
2026-09-17 export** and will drift; `schema.json` is the source of truth.

Tables marked `*` are published and externally queryable but are not registered
by the site's own `src/db.js`, so the in-app SQL console cannot see them.

### Facilities and places

The spine of the dataset. `facilities` is one row per catalogued site; `regions` is one row per overlay polygon; `facility_regions` is the spatial-containment edge between them.

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `facilities` | 3,519 | 205 KB | 15 | facility_id (pk), ror |
| `locations` | 3,566 | 230 KB | 7 | location_id (pk), facility_id |
| `facility_types` | 18 | 1 KB | 3 | slug (pk) |
| `provenance` * | 3,534 | 65 KB | 6 | record_id (pk) |
| `regions` | 147 | 15 KB | 13 | region_id (pk), network_id |
| `facility_regions` | 366 | 5 KB | 4 | facility_id + region_id |
| `region_area_links` | 385 | 3 KB | 2 | region_id + area_id |

### Research areas and networks

Controlled-vocabulary topic and consortium membership for facilities.

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `research_areas` | 40 | 2 KB | 4 | area_id (pk), parent_id |
| `research_areas_active` | 40 | 2 KB | 5 | area_id (pk) |
| `area_links` | 9,624 | 72 KB | 2 | facility_id + area_id |
| `networks` | 35 | 2 KB | 4 | network_id (pk) |
| `network_membership` | 688 | 12 KB | 3 | facility_id + network_id |
| `area_coverage_matrix` | 147 | 2 KB | 4 | area_id |

### People

Three human layers unified by `person_registry`: facility staff (`people`), the COD project team (`cod_team_members`), and the coastal-science scholar roster (`community_scholars`).

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `person_registry` | 10,095 | 1008 KB | 34 | canonical_id (pk); orcid, openalex_id, affiliation_ror, person_id, scholar_id |
| `person_identity_source` | 10,728 | 323 KB | 8 | canonical_id |
| `people` | 280 | 32 KB | 18 | person_id (pk), orcid, openalex_id |
| `facility_personnel` | 246 | 32 KB | 12 | person_id + facility_id |
| `registry_facilities` | 263 | 5 KB | 6 | canonical_id + facility_id (matched on ror) |
| `cod_wbs` | 52 | 3 KB | 6 | wbs_code (pk), parent_code, lead_person_id |
| `cod_team_members` | 67 | 8 KB | 15 | member_id (pk), person_id, wbs_code |
| `community_scholars` | 523 | 102 KB | 30 | scholar_id (pk), person_id, orcid, openalex_id |
| `scholar_area_assignments` * | 442 | 17 KB | 5 | canonical_id |
| `person_areas` | 1,065 | 14 KB | 5 | person_id + area_id |
| `person_area_metrics` | 2,704 | 50 KB | 8 | person_id + area_id |

### Publications and collaboration

Bibliometric layer harvested from OpenAlex, plus the co-authorship graphs and the identifier-validation audit trail.

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `publications` | 12,505 | 1.43 MB | 16 | publication_id (pk), openalex_id |
| `authorship` | 8,772 | 85 KB | 5 | person_id + publication_id |
| `publication_topics` | 356,201 | 3.41 MB | 7 | publication_id + concept_id |
| `collaborations` | 116 | 4 KB | 6 | person_a_id + person_b_id |
| `registry_collaborations` | 5,300 | 40 KB | 7 | canonical_id pair |
| `coauthor_edges` | 213,021 | 10.34 MB | 21 | edge_id (pk), canonical_id_a + canonical_id_b |
| `coauthor_candidates` | 43,355 | 3.24 MB | 25 | candidate_id (pk), seen_with_canonical_id |
| `person_validation` | 40,380 | 1.93 MB | 16 | validation_id (pk), canonical_id, check_id, run_id |

### Funding

Award-level funding events and their rollups. Amounts are NOMINAL USD: no inflation adjustment is applied anywhere in the published tables.

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `funders` | 91 | 4 KB | 6 | funder_id (pk) |
| `funding_events` | 3,634 | 203 KB | 18 | event_id (pk), funder_id, facility_id |
| `funding_links` | 3,634 | 80 KB | 7 | funder_id + facility_id |
| `facility_area_funding` | 54 | 5 KB | 12 | area_id + facility_id |
| `funder_area_funding` | 45 | 3 KB | 7 | area_id + funder_id |
| `cpi_index_us` * | 0 | 1 KB | 3 | year |

### Coastal datasets

Curated catalogue of external coastal datasets and their machine-readable access endpoints.

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `coastal_datasets` | 72 | 45 KB | 22 | dataset_id (pk), parent_dataset_id, network_id |
| `dataset_endpoints` | 242 | 16 KB | 6 | dataset_id |
| `dataset_facilities` | 48 | 5 KB | 9 | dataset_id + facility_id |

### Knowledge-map layout

Precomputed groupings and coordinates that drive the Network tab's MVG cartogram. Derived artifacts, not source data.

| Table | Rows | Size | Cols | Join keys |
| --- | --- | --- | --- | --- |
| `facility_primary_groups` | 3,519 | 61 KB | 4 | facility_id + primary_area_id |
| `person_primary_groups` | 280 | 8 KB | 5 | person_id + primary_area_id |
| `mvg_node_layout` * | 713 | 64 KB | 11 | source_id |
| `mvg_area_polygons` * | 21 | 27 KB | 9 | (none) |
| `mvg_layout_metrics` * | 2 | 8 KB | 14 | (none) |

### Reading the catalogue

- **`facilities` mixes two populations.** Of its 3,519 rows, **210 are research
  organisations** and **3,309 are protected-area units** — the three
  `protected-area-federal` / `-state` / `-private` types. Filter on
  `facility_type` (resolved through `facility_types.slug`, or
  `vocab/facility_types.csv`) and never read the row count as a count of
  research facilities:

  ```sql
  WHERE facility_type NOT LIKE 'protected-area-%'   -- research organisations
  ```
- **`funding_links` is a projection of `funding_events`,** kept for backwards
  compatibility and materialised as its own file. Same 3,634 rows, 7 columns
  instead of 18. Use `funding_events` unless you specifically want the narrow
  shape.
- **`cpi_index_us` is intentionally empty.** The deflator series is not loaded,
  so no real-dollar view can be computed. All amounts elsewhere are nominal USD.
- **Core-tier tables are floors.** `person_registry`,
  `person_identity_source`, `registry_collaborations` and
  `registry_facilities` publish only the core tier; the full population stays
  in the local DuckDB. Aggregates from them undercount.
- **`person_validation` is append-only** — one row per (registry row, check,
  run). Filter to the latest `run_id` before counting.
- **`coauthor_candidates` is a review queue,** not a personnel table. Nothing
  in it is an assertion about a person's affiliation.
- **`publication_topics` double-counts.** OpenAlex lists a work under every
  topic it carries, so summing across a topic set inflates totals.

## GeoJSON endpoints

### Facility points

`BASE + public/facilities.geojson` — 1.01 MB, 3,519 point features. This is the
map's first-paint fallback, and the quickest way to get coordinates without a
Parquet reader. Feature properties: `id`, `name`, `acronym`, `type`, `country`,
`parent_org`, `url`.

```bash
curl -s https://tyson-swetnam.github.io/cod-kmap/public/facilities.geojson \
  | jq -r '.features[] | select(.properties.country=="CA")
           | [.properties.name, .geometry.coordinates[1], .geometry.coordinates[0]]
           | @tsv'
```

### Polygon overlays

`BASE + public/overlays/manifest.json` is the discovery endpoint — 15 layers,
about 6.4 MB of GeoJSON in total. Each manifest key `<id>` resolves to
`BASE + public/overlays/<id>.geojson`. The manifest gives each layer's label,
colour and category; the newer layers also declare their authoritative source
and feature count.

| Layer id | Features | Size | Category | Default |
| --- | --- | --- | --- | --- |
| `nerr-reserves` | 28 | 157 KB | coastal | on |
| `nep-programs` | 28 | 234 KB | coastal | on |
| `marine-sanctuaries` | 13 | 172 KB | marine | on |
| `marine-monuments` | 4 | 7 KB | marine | on |
| `nps-coastal` | 44 | 73 KB | marine | on |
| `neon-sites` | 61 | 99 KB | context | on |
| `coastal-nps-units` | 144 | 513 KB | coastal-terrestrial | off |
| `coastal-fws-units` | 197 | 882 KB | coastal-terrestrial | off |
| `coastal-usfs-special` | 91 | 130 KB | coastal-terrestrial | off |
| `coastal-wilderness` | 67 | 394 KB | coastal-terrestrial | off |
| `coastal-state-protected` | 1,816 | 2.69 MB | coastal-terrestrial | off |
| `coastal-ngo-private` | 1,003 | 940 KB | coastal-terrestrial | off |
| `ramsar-us` | 40 | 24 KB | coastal-terrestrial | off |
| `neon-domains` | 20 | 109 KB | context | off |
| `epa-regions` | 10 | 67 KB | context | off |

"Default" is whether the app paints the layer on first load; heavy or
cluttering layers default off (`DEFAULT_OFF` in `src/overlays.js`). It has no
bearing on fetching them.

The `regions` Parquet table carries one row per overlay polygon with its
attributes and network linkage, but **not** its geometry. Join
`regions.region_id` to the overlay features when you need both.

## Vocabulary endpoints

Three CSVs, served for the app's filter labels and usable as join tables. They
are byte-identical to `schema/vocab/` in the repository, which is canonical.

| File | Header | Rows | Joins to |
| --- | --- | --- | --- |
| `public/vocab/facility_types.csv` | `slug,label,description` | 18 | `facilities.facility_type` |
| `public/vocab/research_areas.csv` | `slug,label,gcmd_uri,parent_slug` | 40 | `research_areas.slug` |
| `public/vocab/networks.csv` | `slug,label,aliases,level,url` | 35 | `networks.slug` |

`research_areas.csv` carries a `gcmd_uri` per row, so topics can be mapped onto
NASA's GCMD keyword vocabulary.

## Recipes

### DuckDB — query in place

```sql
INSTALL httpfs; LOAD httpfs;

-- Facility counts by type, resolved through the vocabulary
SELECT ft.label AS facility_type, count(*) AS n
FROM 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/facilities.parquet' f
JOIN 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/facility_types.parquet' ft
  ON ft.slug = f.facility_type
GROUP BY 1
ORDER BY n DESC;
```

To avoid repeating the base URL, bind the names once and then use bare table
names — which also lets you run queries copied verbatim out of the site's SQL
tab:

```sql
INSTALL httpfs; LOAD httpfs;
SET VARIABLE base = 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/';

CREATE OR REPLACE VIEW facilities AS
  SELECT * FROM read_parquet(getvariable('base') || 'facilities.parquet');
CREATE OR REPLACE VIEW funding_events AS
  SELECT * FROM read_parquet(getvariable('base') || 'funding_events.parquet');
CREATE OR REPLACE VIEW funders AS
  SELECT * FROM read_parquet(getvariable('base') || 'funders.parquet');

SELECT f.canonical_name, fe.fiscal_year,
       sum(fe.amount_usd) AS total_usd_nominal, count(*) AS n_awards
FROM funding_events fe
JOIN facilities f USING (facility_id)
WHERE fe.fiscal_year IS NOT NULL
GROUP BY 1, 2
ORDER BY total_usd_nominal DESC
LIMIT 20;
```

### Python — pandas, one column at a time

```python
import pandas as pd

BASE = "https://tyson-swetnam.github.io/cod-kmap/public/parquet/"

fac = pd.read_parquet(BASE + "facilities.parquet",
                      columns=["facility_id", "canonical_name",
                               "country", "facility_type", "hq_lat", "hq_lng"])
print(fac.value_counts("country").head(10))
```

### Python — duckdb, joins without downloading

```python
import duckdb

BASE = "https://tyson-swetnam.github.io/cod-kmap/public/parquet/"
con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")

df = con.execute(f"""
    SELECT p.name, p.orcid, fp.role, f.acronym
    FROM '{BASE}facility_personnel.parquet' fp
    JOIN '{BASE}people.parquet'     p USING (person_id)
    JOIN '{BASE}facilities.parquet' f USING (facility_id)
    WHERE fp.is_key_personnel AND p.orcid IS NOT NULL
    ORDER BY f.acronym, p.name
""").df()
print(df.head())
```

### R — arrow or duckdb

```r
library(arrow)
base <- "https://tyson-swetnam.github.io/cod-kmap/public/parquet/"
fac  <- read_parquet(paste0(base, "facilities.parquet"))
table(fac$country)

# or, for joins:
library(duckdb)
con <- dbConnect(duckdb())
dbExecute(con, "INSTALL httpfs; LOAD httpfs;")
dbGetQuery(con, sprintf(
  "SELECT country, count(*) n FROM '%sfacilities.parquet' GROUP BY 1 ORDER BY n DESC",
  base))
```

### curl — schemas and non-Parquet data

```bash
BASE=https://tyson-swetnam.github.io/cod-kmap

# What columns does a table have?
curl -s $BASE/public/parquet/schema.json | jq '.tables.facilities.columns'

# Which tables are biggest?
curl -s $BASE/public/parquet/schema.json \
  | jq -r '.tables | to_entries | sort_by(-.value.size_bytes)[:5]
           | .[] | "\(.key)\t\(.value.n_rows) rows"'

# Documentation as Markdown
curl -s $BASE/docs/METHODS.md | head -40

# Overlay layers on offer
curl -s $BASE/public/overlays/manifest.json | jq 'keys'
```

Do not pipe a `.parquet` URL through a tool that converts responses to text —
Parquet is binary and will be corrupted silently. Plan with `schema.json`, then
query with a real Parquet client.

## Reproducing the full database locally

The published tables are a subset: the person registry ships only its core
tier, and `db/parquet/` in the repository holds several tables that are not
deployed at all. To get everything:

```bash
git clone https://github.com/tyson-swetnam/cod-kmap
cd cod-kmap
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/rebuild_db_from_parquet.py   # db/cod_kmap.duckdb from committed Parquet
duckdb db/cod_kmap.duckdb
```

`rebuild_db_from_parquet.py` is required rather than optional: the DuckDB
on-disk format is not portable across versions, so the `.duckdb` file is
gitignored and `db/parquet/*.parquet` is the committed artifact. Rebuilding also
re-creates the helper views from
[`schema/schema.sql`](https://github.com/tyson-swetnam/cod-kmap/blob/main/schema/schema.sql),
which do not exist over HTTP at all — see
[For AI agents](for_ai_agents.md) for why.
