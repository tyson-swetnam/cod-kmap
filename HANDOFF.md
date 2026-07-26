# Handoff — COD team, scholars, and dataset layers

**Status:** built and merged to `main` (PRs #8, #9, #10). `scripts/qa.py` passes
on a clean rebuild.

**Update 2026-07-26 — the enrichment run in §3 has been executed.** See
`BIBLIOMETRIC_ENRICHMENT_RUN.md` for what it produced and what it broke on the
way. Four defects had to be fixed before it would complete: OpenAlex now
requires an `api_key` rather than the `mailto=` polite pool (all six calling
scripts rewired through the new `scripts/openalex_auth.py`); the ORCID
employer matcher accepted unrelated organisations; its name-only fallback
picked arbitrarily among namesakes; and the harvest's stage A could never
populate the rising cohort. It also removed 12 pre-existing misattributed
`openalex_id`s and 8 ORCIDs — see `scripts/wipe_misattributed_identifiers.py`.
The sections below are kept as written for context; §3's "why it hasn't
happened" and §5's "the `--harvest` path has never executed" are now historical.

Still outstanding: nobody has rendered the three tabs in a browser (§5).

This file is the working brief for picking that up in a fresh session. It sits
at the repo root rather than in `docs/` deliberately: `docs/` is copied to the
public site by `.github/workflows/deploy.yml`, and this is operator-facing.

`CLAUDE.md` covers the repo's general shape and gotchas. This covers only what
is specific to these three layers and to the enrichment run.

---

## 1. What exists

Three data layers and three tabs, all previously proposed in
`docs/reference_documents_report.md` and now built.

| Layer | Tables | Tab | Built by |
|---|---|---|---|
| COD project org chart | `cod_wbs`, `cod_team_members` | `/team` | `scripts/build_cod_team_lake.py` |
| Coastal-science scholar roster | `community_scholars` | `/scholars` | `scripts/build_community_scholars.py` |
| Curated dataset catalogue | `coastal_datasets`, `dataset_endpoints` | `/data` | `scripts/load_coastal_datasets.py` |

Current contents:

- **40** named COD team members across 50 role rows, **52** WBS elements, **16**
  unfilled positions, **1** group-staffed element. PI Skip Van Bloem; Co-PIs
  Hank Loescher, Allison Myers-Pigg, Tyson Swetnam; an 11-member Science
  Leadership Committee.
- **339** community scholars in three cohorts (pre-eminent / most-active /
  rising, overlapping), 19 countries.
- **72** datasets with **242** access endpoints, 62 exposing a machine-readable
  service (31 ERDDAP, 50 REST, 21 THREDDS, plus OPeNDAP / OGC / STAC / S3).

Method and provenance for all three: `docs/team_scholars_datasets_methods.md`
(also served on the site under Docs → Team, Scholars & Data).

### The team layer is a real DuckLake

`scripts/build_cod_team_lake.py` writes `db/cod_team.ducklake` plus
`db/ducklake_data/`, so each run of the roster lands as a snapshot:

```sql
ATTACH 'ducklake:db/cod_team.ducklake' AS teamlake;
SELECT * FROM teamlake.snapshots();
SELECT * FROM teamlake.cod_team_members AT (VERSION => 1);
```

Both paths are gitignored, for the same reason `db/cod_kmap.duckdb` is: the
DuckDB on-disk format is not portable across versions, and the whole thing
regenerates from two CSVs. **Committed parquet is the shared artifact**, and is
what DuckDB-Wasm reads in the browser.

The extension is optional — `pip install duckdb-extensions
duckdb-extension-ducklake` (the wheel bundles the binary, so it works without
reaching `extensions.duckdb.org`). Without it the script says so and writes
plain tables; the parquet output is identical, you just lose the snapshot log.

---

## 2. Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install duckdb-extensions duckdb-extension-ducklake   # optional, for the lake
python scripts/rebuild_db_from_parquet.py                 # committed parquet -> local DB
python scripts/qa.py                                      # must exit 0 before you start
```

Verified working on Python 3.11 with duckdb 1.5.4.

`db/cod_kmap.duckdb` is gitignored and does not exist in a fresh clone — the
rebuild step is not optional.

---

## 3. The enrichment run (the actual next job)

### Why it hasn't happened

The environment this was built in returns HTTP 403 for `api.openalex.org` and
`pub.orcid.org`. So every research agent that could not verify an identifier
left it null rather than guessing, and **only 8 of 339 scholars carry an
ORCID**. All metric columns on both `community_scholars` and the Team tab are
empty.

That is the correct state to be in, not a defect: a wrong ORCID misattributes a
researcher's entire publication record, and this repo has already had to undo
exactly that (`scripts/wipe_bad_openalex_attributions.py`, which cleaned up
after a name-only resolver attached cardiologists to marine laboratories).

### Before you start

Resolve the OpenAlex topic ids. `data/datasets/coastal_topics.csv` ships with
18 rows whose ids are the literal sentinel `RESOLVE`:

```bash
python scripts/build_community_scholars.py --resolve-topics
# paste the printed T##### ids back into data/datasets/coastal_topics.csv
```

The harvest **refuses to run** while any sentinel remains, and that is
deliberate: the topic set defines the cohorts, so a published roster should be
traceable to explicit ids rather than to whatever a search returned that day.
`--allow-unresolved-topics` is the escape hatch, and it prints a warning that
the resulting roster is not reproducible.

### The chain

Order matters in three places (see the notes under it):

```bash
export OPENALEX_EMAIL=tswetnam@arizona.edu    # OpenAlex polite pool

python scripts/enrich_people_orcid.py         # strict 3-rule matcher
python scripts/enrich_people_openalex.py      # publications + topics
python scripts/enrich_people_gscholar.py      # Scholar ids via OpenAlex/ORCID
python scripts/backfill_publication_topics.py
python scripts/compute_person_areas.py        # needs publication_topics
python scripts/compute_collaborations.py --export-parquet
python scripts/compute_primary_groups.py      # MUST precede area_metrics
python scripts/compute_area_metrics.py        # h-index, citations, composite_z
python scripts/init_people_tables.py --export-parquet
python scripts/build_cod_team_lake.py         # re-snapshots people.parquet
python scripts/build_community_scholars.py --harvest
python scripts/qa.py

git add -f db/parquet/*.parquet public/parquet/*.parquet
```

Three easy mistakes, all of which were present in the docs at some point:

1. **`compute_primary_groups.py` before `compute_area_metrics.py`.** Two of the
   metric tables join `facility_primary_groups`, which only the groups script
   produces.
2. **`compute_collaborations.py` needs `--export-parquet`.** Without it the
   co-author counts never leave the local database.
3. **`init_people_tables.py --export-parquet` is not optional.** Nothing else in
   the chain exports `publications`, `authorship`, `person_areas` or
   `publication_topics`, so harvested publications would never reach the site.
   It is `CREATE TABLE IF NOT EXISTS` throughout, so it will not wipe anything.

Also note `enrich_people_openalex.py` writes to the `people` table but exports
no parquet — that is why `build_cod_team_lake.py` runs near the end, to
re-snapshot `people.parquet` after all three enrichers have written.

### What the harvest does

`build_community_scholars.py --harvest` runs five checkpointed stages under
`data/raw/community_scholars/` (gitignored), roughly 1,250 requests, minutes in
the polite pool:

| Stage | What |
|---|---|
| A | Per topic, group `/works` by author → candidate set |
| B | Hydrate authors 50 at a time |
| C | For the shortlist, count coastal works all-time and over 5 years, find first pub year |
| D | Assign cohorts locally and deterministically |
| E | Link to `people`, write the table, refresh parquet |

`--stage A|B|C` bounds a run and `--resume` reuses the caches.

**Treat the first run as a shakedown — this path has never executed.** Three
silent-failure modes were found by review and hardened, but "hardened against"
is not "observed working":

- The author-id batch filter against `/authors` is confirmed in this repo only
  against `/works`. If it is unsupported there the request still returns 200
  with an empty result, so the code now treats an empty first batch as
  unsupported, says so, and falls back to one request per author.
- Affiliation is read from `last_known_institutions` (plural list) with a
  fallback to the older singular object; `h_index`/`i10_index` likewise fall
  back from `summary_stats` to top level.
- Stage A no longer writes its checkpoint when every topic request failed —
  otherwise `--resume` would learn that "nothing found" meant "already done".

Sanity-check after each stage: stage A should yield on the order of 1–2k
candidate ids, stage B should hydrate nearly all of them, and stage D prints how
many authors passed the coastal-share gate. A zero at any of those is a bug, not
a finding.

### Done looks like

```bash
python scripts/qa.py    # exits 0
```

and non-null metrics:

```sql
SELECT COUNT(*) FILTER (WHERE h_index IS NOT NULL) AS measured,
       COUNT(*) AS total
FROM community_scholars;
```

Once measured rows exist, `qa.py` additionally enforces cohort sizes of
100 / 100 / 50 (±10) over those rows — curated-only rows stay flagged on
purpose and are excluded from that count.

---

## 4. Rules that must not be broken

These are load-bearing; several exist because they were violated once already.

**Never resolve a person by name.** `scripts/wipe_bad_openalex_attributions.py`
and `wipe_medicine_attributions.py` exist because a name-only OpenAlex resolver
attached cardiologists to marine laboratories. New code links on ORCID or
`openalex_id` equality only. The harvest additionally gates on a coastal-topic
share (≥10 works and ≥15% of output).

There is exactly one place names are compared — `reconcile()` in
`build_community_scholars.py`, deciding whether two rows *already in the roster*
describe one researcher. That never attributes a publication, so it cannot
reproduce the failure above. It also requires spelled-out given names to agree,
because an earlier version keyed on first-initial-plus-surname and merged
**"Y. Stacy Zhang" with "Y. Joseph Zhang"** — different people.

**A missing identifier beats a wrong one.** Leave seed ID cells blank rather
than guessing.

**`person_id` is `sha1(f"{name.lower()}|{orcid}|{email.lower()}")[:16]`**
(`scripts/load_facility_personnel.py:54`). Two older scripts use a different
formula; use this one. Note the trap: filling in a blank `orcid` or `email` cell
in `data/seed/cod_team_members.csv` re-keys that person and abandons their
enriched `people` row.

**New parquet needs `git add -f`.** `db/parquet/*.parquet` and
`public/parquet/*.parquet` are gitignored but tracked, so a plain `git add`
silently skips new files — everything works locally and the live site 404s.

**New `qa.py` invariants for these tables must no-op on an empty table.** The
weekly `refresh-data.yml` runs `ingest.py`, which re-executes `schema.sql` and
empties them in CI; their data lives in committed parquet that ingest never
touches. (A *missing* table is now reported, though — absent and empty are no
longer treated alike.)

**`schema/vocab/` and `public/vocab/` must stay in sync**, and SQL views must be
defined in both `schema/schema.sql` and `src/db.js` `helperViews`, because views
do not survive a parquet round-trip.

---

## 5. Known-unverified surface

**No browser has ever rendered these three tabs.** The build environment blocks
`esm.sh`, so the CDN importmap could not resolve and the app would not boot; a
Playwright run confirmed it, with every view empty. Verification was DuckDB
executing each view's SQL and Node executing each render path against real query
results. That caught real bugs but cannot catch anything needing a live page.

Worth clicking first, because a Node harness stubbed rather than exercised them:

- `/team` — WBS section headers expand and collapse (`.team-wbs-head`).
- `/data` — the ⧉ button on an endpoint badge writes to the clipboard
  (`navigator.clipboard`, with a `document.execCommand` fallback for
  non-secure contexts).
- `/scholars` — the three cohort chips filter, and deselecting all of them
  re-selects all rather than emptying the page.
- All three — typing in a search box keeps focus after each keystroke (the
  re-render replaces the input; focus and caret are restored explicitly).
- Deep links: `#/scholars/<scholar_id>`, `#/data/<dataset_id>`,
  `#/people/<person_id>` should scroll to and highlight one card.

**The `--harvest` path has never executed** (section 3).

---

## 6. Open questions for a human

- **Two chart affiliations.** The 2026 org chart prints Rodrigo Vargas at
  *Arizona* and Christine Angelini at *AECOM*; both are recorded here with their
  institutional affiliation (U Delaware, U Florida) and the chart's text noted
  in `data/seed/cod_team_members.csv`. Worth confirming against the award
  documents.
- **Names read off the chart image** may have spelling errors — the chart
  renders "Maclamore" where Clemson lists Eric McLamore.
- **CODISS** is in the dataset catalogue at `confidence: low` with a note: the
  acronym could not be verified against a public data endpoint, and the DHS S&T
  system it appears to name is not open data. Keep, correct, or drop.
- **Scholar roster size.** 339 is a deliberately wide candidate pool; the
  harvest narrows it to a ranked 100 / 100 / 50. If you want the shipped roster
  to *be* ~250, that is a curation decision to make after the harvest.

---

## 7. File map

```
data/seed/cod_wbs.csv                  WBS tree (hand-transcribed from the chart)
data/seed/cod_team_members.csv         one row per (person, WBS element, role)
data/seed/community_scholars_seed.json curated scholar roster
data/datasets/coastal_datasets.json    dataset catalogue (source of truth)
data/datasets/coastal_topics.csv       OpenAlex topic set — the reproducibility anchor

scripts/build_cod_team_lake.py         seeds -> DuckLake -> parquet, syncs people
scripts/build_community_scholars.py    --seed (offline) | --harvest (OpenAlex)
scripts/load_coastal_datasets.py       JSON -> tables -> parquet
scripts/qa.py                          the only test gate; add invariants here
scripts/rebuild_db_from_parquet.py     committed parquet -> local DB

src/views/team.js                      /team
src/views/scholars.js                  /scholars
src/views/datasets.js                  /data
src/db.js                              tables array + helper views (add both)

docs/team_scholars_datasets_methods.md method + provenance (public)
schema/schema.sql                      DDL for all five new tables + v_cod_team_enriched
```

There is no test framework. `scripts/qa.py` is the correctness gate — 35
assertions, of which about half cover these three layers, and each of those was
verified by injecting the defect it targets and confirming the gate caught it.
Add new invariants there rather than introducing pytest.
