# Team, Scholars & Data — methods and provenance

This page documents how three of cod-kmap's tabs are built: the **Team**
tab (the COD project org chart), the **Scholars** tab (the coastal ocean
science research community), and the **Data** tab (curated coastal
datasets and their access endpoints).

All three were proposed in
[the reference documents report](#/docs/reference-documents-report) and
built in July 2026. Each is regenerable from committed seed files — no
step depends on state that only exists on one machine.

---

## 1. COD project team (the Team tab)

### Where the data comes from

The project organisational chart (2026 revision) is transcribed by hand
into two CSVs:

| File | Contents |
|---|---|
| `data/seed/cod_wbs.csv` | 52 work-breakdown elements: code, parent, title, display order |
| `data/seed/cod_team_members.csv` | 67 rows — one per (person, WBS element, role) |

A person who leads several WBS elements gets one row each, which is how
the chart's "5.0 / 5.1 / 5.6" pattern survives being flattened into a
table. The Team tab recombines them into a single card.

The roster covers 41 named people and 16 unfilled positions. Unfilled
positions carry `status = 'tbd'` or `'tbh'` and no person identity; they
are rendered muted rather than hidden, because an empty slot is
information about the project rather than missing data.

### Institution slugs

`institution_slug` mirrors the chart's colour legend and is validated
against `COD_INSTITUTIONS` in both the build script and `scripts/qa.py`, so
a typo cannot silently produce an uncoloured chip:

```
clemson  yale  unm  battelle  vcu  pnnl  unl  obfs  uga  arizona
delaware  usc  uidaho  montana-state  alabama  florida
coastal-carolina  charleston  other-university  agency  company  various
```

### Two chart labels to confirm

The chart's own text disagrees with the affiliations these two people are
otherwise associated with. Both are recorded in the seed CSV's `notes`
column and should be confirmed against the award documents:

- **Rodrigo Vargas** — the chart says *Arizona*; he is otherwise
  associated with the University of Delaware.
- **Christine Angelini** — the chart says *AECOM*; she is otherwise
  associated with the University of Florida.

### Building it

```bash
python scripts/build_cod_team_lake.py
```

This is idempotent: re-running with unchanged seeds produces identical
tables and identical parquet.

### Why a DuckLake

The team roster is the one table in this repo that is expected to change
by revision rather than by re-harvest — people join, positions get filled,
the WBS gets reorganised — and "what did the team look like at proposal
time?" is a question worth being able to answer. So the build writes into
a **DuckLake** catalogue:

```
db/cod_team.ducklake      the catalogue
db/ducklake_data/         DuckLake-managed parquet
```

Every run lands as a snapshot, so the history is queryable:

```sql
ATTACH 'ducklake:db/cod_team.ducklake' AS teamlake;
SELECT * FROM teamlake.snapshots();
SELECT * FROM teamlake.cod_team_members AT (VERSION => 1);
```

Both paths are gitignored, for the same reason `db/cod_kmap.duckdb` is:
DuckDB's on-disk format is not portable across versions, and the whole
thing is regenerable from two CSVs. The **shared** artifact is still plain
parquet in `db/parquet/` and `public/parquet/`, which is also what
DuckDB-Wasm reads in the browser. Snapshot history is a local convenience,
not shared state.

Rows are staged and moved with a single `INSERT … SELECT` per table.
DuckLake records one snapshot per statement, so inserting row-by-row would
bury each run's real change under a hundred single-row snapshots.

The extension is optional. Install it without needing
`extensions.duckdb.org`:

```bash
pip install duckdb-extensions duckdb-extension-ducklake
```

If it is unavailable the script says so and writes plain tables into the
main database instead. The parquet output is identical either way, so a
fallback run is not a degraded run — you just lose the snapshot log.

### Syncing into `people`

Named members are upserted into `people` on
`person_id = sha1(lower(name) | orcid | lower(email))[:16]` — the same
formula as `scripts/load_facility_personnel.py`, so the same human seeded
through either path collapses onto one row.

Every enrichable column is written with
`COALESCE(excluded.x, people.x)`, so a blank cell in the seed CSV never
wipes a value that the enrichment scripts resolved earlier.

### Filling in profiles and metrics

The seed CSV ships with Google Scholar IDs for the PI and Co-PIs and blank
ORCID / OpenAlex columns for everyone else. **A missing identifier is much
better than a wrong one**: attaching the wrong ORCID to a researcher
misattributes their entire publication record, and this repo has already
had to undo exactly that (see
`scripts/wipe_bad_openalex_attributions.py`, which cleaned up after a
name-only resolver attached cardiologists to marine laboratories).

To fill them in, run the existing enrichment chain against a network that
can reach `api.openalex.org` and `pub.orcid.org`:

```bash
export OPENALEX_EMAIL=you@example.org        # OpenAlex polite pool
python scripts/enrich_people_orcid.py        # strict 3-rule matcher
python scripts/enrich_people_openalex.py     # publications + topics
python scripts/enrich_people_gscholar.py     # Scholar ids via OpenAlex/ORCID
python scripts/backfill_publication_topics.py
python scripts/compute_person_areas.py
python scripts/compute_collaborations.py
python scripts/compute_area_metrics.py       # h-index, citations, composite_z
python scripts/compute_primary_groups.py
python scripts/build_cod_team_lake.py        # re-snapshot people.parquet
```

Until that runs, the Team tab shows profile links but no metrics, and says
so at the foot of the page.

---

## 2. Coastal ocean science scholars (the Scholars tab)

`community_scholars` is a field-wide roster: who defined coastal ocean
science, who is publishing most in it right now, and who is coming up.

It is deliberately a **separate table from `people`**. `people` is the
staff of catalogued facilities; these 346 researchers mostly do not work
at one, and mixing a bibliometric cohort into the facility directory would
distort every per-facility metric on the Stats tab.

### Cohorts

| Flag | Meaning |
|---|---|
| `is_preeminent` | Established, field-defining, highly cited |
| `is_most_active` | Among the most prolific in the last five years |
| `is_rising` | Early career, rapidly growing impact |

A scholar can carry more than one flag. Each has its own rank column,
which is what the Scholars tab orders on.

**The curated roster is a candidate pool, not the final cohorts.** It holds
346 names because a wider pool gives the harvest more to rank and makes it
less likely that a genuinely leading researcher is missing entirely. The
harvest then pins the cohorts to the sizes in `COHORTS` — 100 pre-eminent,
100 most-active, 50 rising — so the measured roster is roughly 250 people,
and `scripts/qa.py` enforces those sizes once measured rows exist. Until
then the tab shows the full pool and says so.

### Two ways the table gets populated

**Curated (what ships).** 346 scholars researched across ten sub-fields —
physical oceanography, estuarine ecology, coastal geomorphology, sea
level, blue carbon, HABs and water quality, ocean observing, coastal
hazards and engineering, fisheries and MPAs, and the social dimension —
each with an affiliation, sub-field topics, and a one-line rationale.

```bash
python scripts/build_community_scholars.py --seed
```

Rows are marked `source = 'websearch-curated'` with metric columns NULL,
and the tab labels them "bibliometrics pending" rather than rendering
zeros. `confidence` records how well the identity was corroborated;
`low` means the identity check did not complete, not that the person is
doubtful.

**Harvested (measured).** Needs `api.openalex.org`:

```bash
export OPENALEX_EMAIL=you@example.org
python scripts/build_community_scholars.py --harvest
```

Five stages, each checkpointed under `data/raw/community_scholars/`
(gitignored) so `--resume` is cheap and `--stage A|B|C` bounds a run:

| Stage | What it does |
|---|---|
| A | Per topic, group `/works` by author to find who publishes most in it; union across topics |
| B | Hydrate authors 50 at a time: ORCID, summary stats, last known institution |
| C | For the shortlist only, count coastal-topic works all-time and over five years, and find each author's first publication year |
| D | Assign cohorts locally and deterministically |
| E | Link to `people`, write the table, refresh parquet |

About 1,250 requests, minutes in the polite pool.

### The topic set is the reproducibility anchor

Cohorts are defined relative to `data/datasets/coastal_topics.csv` — 18
OpenAlex topics spanning the sub-fields above. Change that file and the
cohorts change, which is why it is committed alongside the roster it
produced.

Topic IDs are resolved once and pasted back in:

```bash
python scripts/build_community_scholars.py --resolve-topics
```

The harvest **refuses to run** while any row still holds the `RESOLVE`
sentinel, so a published roster is always traceable to explicit topic IDs
rather than to whatever a search happened to return that day.

### Identity rules

Two rules, both there because this repo has been burned before:

1. **Candidates are OpenAlex author IDs from the start.** No name matching
   ever happens, at any stage.
2. **A coastal-share gate**: an author needs at least 10 works in the
   topic set *and* at least 15% of their total output inside it. This is
   what stops a prolific researcher in an unrelated field from ranking on
   total h-index after one coastal paper.

A scholar is linked to an existing `people` row only on ORCID or
`openalex_id` equality — never on name.

### Curated and harvested rows reconcile

A curated scholar matched by ORCID or Google Scholar ID keeps their
curation rationale and gains measured metrics. An unmatched curated
scholar is **kept**, not dropped: a hand-picked expert should not vanish
because a threshold did not like them.

---

## 3. Curated coastal datasets (the Data tab)

`data/datasets/coastal_datasets.json` is the single source of truth. The
loader deletes and re-inserts both tables on every run, so editing the
JSON and re-running is the entire update workflow:

```bash
python scripts/load_coastal_datasets.py
python scripts/load_coastal_datasets.py --dry-run      # validate only
python scripts/load_coastal_datasets.py --check-urls   # probe endpoints (needs network)
```

### What's in it

82 datasets with 258 access endpoints, 72 of them exposing a
machine-readable service:

- the programs named in the Design Flow diagram — MarineGEO, NERRS/CDMO,
  C-CAP, Coastal Zone Management, Digital Coast, IOOS, NASA coastal
  products and the Sea Level Change portal, EPA NCCA, the Critical Zone
  network, Coastal Carbon, coastal LTER, OOI, NEON, CODISS, CDIP
- all 11 IOOS regional associations, each with its own ERDDAP
- federal archives and APIs — NDBC, CO-OPS, NCEI, CoastWatch, USGS CMHRP
  and ScienceBase, PO.DAAC, Earthdata/CMR, OBIS, GBIF, BCO-DMO, HydroShare
- a few international counterparts (Copernicus Marine, EMODnet)

### Endpoints are the point

A dataset without an access endpoint is exactly what this catalogue exists
to prevent, and `scripts/qa.py` fails if one appears. Each endpoint carries
a type from a fixed vocabulary:

```
erddap  thredds  opendap  ogc-wms  ogc-wfs  ogc-api
rest-api  s3  ftp  portal  doi  stac
```

Everything except `portal` is a machine-readable service. The Data tab
renders one badge per endpoint — clicking opens it, the ⧉ button copies the
URL — and colours `portal` badges muted so the API endpoints read first.

Datasets are also categorised, which is what the tab filters on:

```
observing-system  monitoring-program  data-portal  remote-sensing
synthesis-network  model-output  archive  mapping-product
```

### Curation rules

- Every dataset needs at least one endpoint and should have a `portal`
  landing page alongside any machine endpoint.
- `network_id` links to `schema/vocab/networks.csv` where a slug exists.
  Do not invent network slugs to make a link work; the loader nulls an
  unknown one and warns.
- `confidence` is `high` for a verified endpoint on the provider's own
  site, `medium` where the program is real but the access route is spread
  across several services, `low` where no public endpoint could be
  confirmed at all (CODISS is the one such entry — the DHS system it names
  is not open data).
- `program` labels must be consistent, because the tab groups on them. The
  catalogue was assembled from two independent research passes that named
  the same programs differently; the labels are canonicalised so real
  families group together instead of splitting into singletons.
- URLs are recorded as the provider publishes them. `--check-urls` probes
  them but is off by default, since it needs outbound network access that
  CI does not have.

---

## Verifying all three

```bash
python scripts/rebuild_db_from_parquet.py   # committed parquet -> local DB
python scripts/qa.py                        # must exit 0
python -m http.server 5173                  # then open /#/team, /#/scholars, /#/data
```

`scripts/qa.py` carries 13 invariants for these tables — exactly one PI,
at least three Co-PIs and ten committee members, named members resolving
to `people` rows, unfilled positions carrying no person, WBS and
parent-code closure, cohort flags agreeing with cohort ranks, ORCID and
OpenAlex ID shape, no dataset without an endpoint, endpoint foreign-key
closure, and vocabulary membership throughout — plus a column-presence
check per table, which catches the schema-versus-loader drift that has
already bitten the people tables once.

Every block is skipped when its table is empty, so the weekly
ingest-only refresh (where these tables have no rows, because their data
lives in committed parquet that `ingest.py` never touches) stays green.
