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

The roster covers 40 named people, 16 unfilled positions, and one
group-staffed element. `status` distinguishes them:

| status | meaning | synced into `people`? |
|---|---|---|
| `active` | a named individual | yes |
| `tbd` / `tbh` | an unfilled position, rendered muted | no |
| `collective` | work staffed by a group, not a person — the chart's "NEON Staff" box | no |

Unfilled positions are rendered muted rather than hidden, because an empty
slot is information about the project rather than missing data. `collective`
exists so a staffing pool is not stored in `people` as though it were a
human: it was, briefly, and the enrichment scripts would have gone looking
for its publications.

### Institution slugs

`institution_slug` mirrors the chart's colour legend and is validated
against `COD_INSTITUTIONS` in both the build script and `scripts/qa.py`, so
a typo cannot silently produce an uncoloured chip:

```
clemson  yale  unm  battelle  vcu  pnnl  unl  obfs  uga  arizona
delaware  usc  uidaho  montana-state  alabama  florida
coastal-carolina  charleston  other-university  agency  company  various
```

### Two chart labels worth confirming

For two people the chart's printed affiliation differs from the one they are
otherwise associated with. The seed records the institutional affiliation and
notes the chart's text, so the discrepancy is visible rather than silently
resolved — worth checking against the award documents:

| Person | Recorded here | The 2026 chart prints |
|---|---|---|
| Rodrigo Vargas | University of Delaware | Arizona |
| Christine Angelini | University of Florida | AECOM |

Some names were also read off the chart image, so spellings are worth a pass
— the chart renders "Maclamore" where Clemson lists Eric McLamore.

### Building it

```bash
python scripts/build_cod_team_lake.py     # seeds -> cod_wbs, cod_team_members, people (lake)
python scripts/qa.py
python scripts/export_parquet.py          # lake -> db/parquet, public/parquet
```

This is idempotent: a run whose seeds already match the lake changes
nothing and writes no snapshot, so the next export is byte-identical.
`--dry-run` validates the seeds without writing.

### Roster history

The team roster is the one table in this repo that is expected to change
by revision rather than by re-harvest — people join, positions get filled,
the WBS gets reorganised — and "what did the team look like at proposal
time?" is a question worth being able to answer.

The build writes `cod_wbs` and `cod_team_members` into the project's one
DuckLake store of record (`db/cod_kmap_catalog.sqlite` + `db/lake/`,
opened through `scripts/cod_db.py`), in **one transaction per run**. A run
that changes the roster lands as one snapshot whose commit message starts
`build_cod_team_lake:`, so the roster's history is the lake's own history:

```python
import sys; sys.path.insert(0, "scripts")
import cod_db
con = cod_db.connect(read_only=True)
con.sql("""SELECT snapshot_id, snapshot_time, author, commit_message
             FROM lake.snapshots()
            WHERE commit_message LIKE 'build_cod_team_lake:%'
            ORDER BY 1 DESC""").show()
con.sql("SELECT * FROM cod_team_members AT (VERSION => 12)").show()
```

`python scripts/build_cod_team_lake.py --history` prints the same log with
the per-table row changes of each snapshot.

Until October 2026 the layer kept its own separate catalogue
(`db/cod_team.ducklake` + `db/ducklake_data/`, attached as `teamlake`, with a
plain-table fallback when the `duckdb-extension-ducklake` wheel was
missing). That copy is retired; there is one lake now.

The lake is gitignored local state: snapshot ids mean nothing across
clones, and it is rebuilt from the committed `db/parquet/` with
`scripts/rebuild_lake_from_parquet.py`. The **shared** artifact is the
parquet in `db/parquet/` and `public/parquet/`, which is also what
DuckDB-Wasm reads in the browser. Snapshot history is a local convenience,
not shared state.

Each table is rewritten only when its staged rows differ from the lake,
with a `DELETE` and a single `INSERT … SELECT`. DuckLake records one
snapshot per autocommit statement, so inserting row-by-row outside a
transaction would bury each run's real change under a hundred single-row
snapshots.

### Syncing into `people`

Named members are upserted into `people` on
`person_id = sha1(lower(name) | orcid | lower(email))[:16]` — the same
formula as `scripts/load_facility_personnel.py`, so the same human seeded
through either path collapses onto one row.

The sync is a `MERGE` (`cod_db.merge_upsert`) keyed on `person_id`.
`orcid`, `openalex_id`, `google_scholar_id` and `homepage_url` keep the
lake's value when the seed cell is blank, and `notes` keeps the lake's
value whenever it has one, so a blank cell in the seed CSV never wipes a
value that the enrichment scripts resolved earlier. `email` is never
written to `people`: it still feeds the `person_id` hash, but contact
details stay in the seed.

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
python scripts/compute_collaborations.py     # co-author edges from authorship
python scripts/compute_person_areas.py       # needs publication_topics above
python scripts/compute_primary_groups.py     # MUST precede area_metrics
python scripts/compute_area_metrics.py       # h-index, citations, composite_z
python scripts/build_mvg_layout.py           # knowledge-map layout
python scripts/build_person_registry.py      # unify the three layers on one key
python scripts/compute_registry_collaborations.py   # cross-cohort co-pub edges
python scripts/link_registry_facilities.py   # researcher ↔ site, on ROR equality
python scripts/rank_person_registry.py       # assign core / archive tier
python scripts/qa.py
python scripts/export_parquet.py             # the only step that writes parquet
```

Every script above writes into the lake, one snapshot per run; none of them
writes parquet. `export_parquet.py` publishes everything at the end.

The four registry scripts run last, and in that order: the registry needs
the three source layers populated before it can unify them, the graph and
the facility links need the registry's node ids, and tiering scores
collaboration degree so it has to follow the graph.

Three things about that order are easy to get wrong, and were wrong in an
earlier version of this page:

- **`compute_primary_groups.py` must run before `compute_area_metrics.py`**,
  not after. Two of the metric tables join `facility_primary_groups`, which
  only the groups script produces.
- **`compute_collaborations.py` must also run before
  `compute_area_metrics.py`**, which reads `collaborations` for the per-area
  co-authorship counts.
- **Nothing reaches the site until `export_parquet.py` runs.** Earlier
  versions of this chain needed `--export-parquet` on
  `compute_collaborations.py` and `init_people_tables.py` because each
  script wrote its own parquet. They no longer do; the flag is accepted as a
  no-op, and the single exporter at the end publishes every table.

Then stage the refreshed parquet — it is gitignored but tracked, so a plain
`git add` silently skips it:

```bash
git add -f db/parquet/*.parquet public/parquet/*.parquet
```

Until that runs, the Team tab shows profile links but no metrics, and says
so at the foot of the page.

---

## 2. Coastal ocean science scholars (the Scholars tab)

`community_scholars` is a field-wide roster: who defined coastal ocean
science, who is publishing most in it right now, and who is coming up.

It is deliberately a **separate table from `people`**. `people` is the
staff of catalogued facilities; these researchers mostly do not work at
one, and mixing a bibliometric cohort into the facility directory would
distort every per-facility metric on the Stats tab.

**Separate table, but no longer a separate identity space.** Until the
registry work, being separate tables also meant being unlinked: a
researcher who was both facility staff and a roster scholar was two rows
with two keys, and nothing in the schema could say they were one person.
`person_registry` now resolves `people`, `cod_team_members` and
`community_scholars` into one node set keyed on a persistent identifier,
so cross-cohort questions — who on the project team already publishes
with whom on the roster — are answerable. Eleven people turn out to hold
more than one cohort flag. The source tables keep their own grain and
their own columns; the registry adds the shared key. See
[The Person Registry](#/docs/person-registry).

### Cohorts

| Flag | Meaning |
|---|---|
| `is_preeminent` | Established, field-defining, highly cited |
| `is_most_active` | Among the most prolific in the last five years |
| `is_rising` | Early career, rapidly growing impact |

A scholar can carry more than one flag. Each has its own rank column,
which is what the Scholars tab orders on.

**A rank is only populated once the scholar has been measured.** The curated
roster ships with all three rank columns NULL, and the tab renders the cohort
badge without a number. An earlier version filled them in alphabetically,
since there was nothing else to sort on — which rendered as "Pre-eminent #1"
for a surname beginning with A and read as a finding rather than an artifact.
`scripts/qa.py` enforces the weaker, honest invariant: a rank may not exist
without its flag, ranks must be unique, and every *measured* row in a cohort
must be ranked.

**The curated roster is a candidate pool, not the final cohorts.** A wide
pool gives the harvest more to rank and makes it less likely that a
genuinely leading researcher is missing entirely. The harvest then pins
the cohorts to the sizes in `COHORTS` — 100 pre-eminent, 100 most-active,
50 rising — and `scripts/qa.py` enforces those sizes once measured rows
exist.

The harvest has now run. The table holds **523 rows**, of which
**220 carry harvested metrics**; 303 remain curated-only with null metrics.
The 220 is the cohort total: 100 + 100 + 50 with 30 scholars holding both
the pre-eminent and most-active designations. Curated-only rows are kept
rather than dropped, and the tab labels them "bibliometrics pending"
rather than rendering zeros.

### Two ways the table gets populated

**Curated (the seed).** Scholars researched by hand across ten sub-fields —
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
| E | Link to `people` and write the table into the lake (one snapshot; `export_parquet.py` publishes it) |

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
`openalex_id` equality — never on name. The same rule governs
`person_registry`, which is where that link is now materialised as a
shared node id rather than left implicit.

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

72 datasets with 242 access endpoints, 62 of them exposing a
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
python scripts/rebuild_lake_from_parquet.py   # committed db/parquet -> local lake
python scripts/qa.py                          # must exit 0
python -m http.server 5173                  # then open /#/people, /#/org, /#/data
```

`scripts/qa.py` carries 13 invariants for these tables — exactly one PI,
at least three Co-PIs and ten committee members, named members resolving
to `people` rows, unfilled positions carrying no person, WBS and
parent-code closure, cohort flags agreeing with cohort ranks, ORCID and
OpenAlex ID shape, no dataset without an endpoint, endpoint foreign-key
closure, and vocabulary membership throughout — plus a column-presence
check per table, which catches the schema-versus-loader drift that has
already bitten the people tables once.

Every block is skipped when its table is empty, so QA stays meaningful on
a lake that does not carry these layers (a scratch lake built from a
partial parquet folder, for example). The weekly `refresh-data.yml` no
longer runs into that case: it rebuilds the lake from the committed
`db/parquet/` first, so these tables are populated before `ingest.py`
upserts into it.
