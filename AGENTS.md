# Agent guide — cod-kmap

This repository builds the **COD Knowledge Map**: a directory of coastal
research facilities, people, funding, publications and datasets across the
Americas, published at <https://tyson-swetnam.github.io/cod-kmap/>.

There are two audiences for this file:

* **Coding agents working in this repository** — read the whole thing, and
  `CLAUDE.md` for the gotchas that will actually bite you.
* **Agents that only want to *read* the published data** — you do not need
  this repo at all. Go to
  [`llms.txt`](https://tyson-swetnam.github.io/cod-kmap/llms.txt) or
  [`docs/for_ai_agents.md`](https://tyson-swetnam.github.io/cod-kmap/docs/for_ai_agents.md).

## Reading the published corpus

| Address | What you get |
| --- | --- |
| `https://tyson-swetnam.github.io/cod-kmap/llms.txt` | Linked outline of every doc page and every queryable table, with column lists |
| `https://tyson-swetnam.github.io/cod-kmap/llms-full.txt` | The whole documentation corpus in one file |
| `https://tyson-swetnam.github.io/cod-kmap/docs/<file>.md` | Any documentation page as raw Markdown (`text/markdown`) |
| `https://tyson-swetnam.github.io/cod-kmap/public/parquet/<table>.parquet` | Any table, queryable in place over HTTP range requests |
| `https://tyson-swetnam.github.io/cod-kmap/public/parquet/schema.json` | Machine-readable columns, types and row counts for every table |
| `https://raw.githubusercontent.com/tyson-swetnam/cod-kmap/main/docs/<file>.md` | The same Markdown from `raw.githubusercontent.com`, for sandboxes that allow GitHub but not `github.io` |

`docs/for_ai_agents.md` is the full guide; `docs/data_endpoints.md` is the
per-table reference.

## Repo shape

Two stacks, one repository:

1. **Python data pipeline** (`scripts/`, `schema/`, `data/`) — ingests JSON
   from the research subagent specs in `agents/` into DuckDB, then exports
   Parquet + GeoJSON into `public/`.
2. **Static MapLibre + DuckDB-Wasm site** (`index.html`, `src/`, `public/`) —
   ES modules and a CDN importmap, deployed to GitHub Pages with **no build
   step**. Do not introduce npm or Vite.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/rebuild_db_from_parquet.py   # ALWAYS run this first after a pull
python scripts/ingest.py                    # data/raw/R*/*.json -> db/cod_kmap.duckdb
python scripts/qa.py                        # data-quality gate; exits non-zero on failure
python scripts/export_parquet.py            # -> db/parquet/, public/parquet/, public/facilities.geojson
python scripts/build_web_overlays.py        # -> public/overlays/*.geojson + manifest.json
python scripts/generate_agent_surface.py _site   # llms.txt, llms-full.txt, robots.txt, sitemap.xml, schema.json

python -m http.server 5173                  # serve the site at http://localhost:5173/
```

There is no test framework. `scripts/qa.py` is the only correctness gate — add
new invariants there rather than introducing pytest.

## Rules for coding agents

1. **Run `rebuild_db_from_parquet.py` before anything that opens the DB.** The
   DuckDB on-disk format is not portable across versions and `db/*.duckdb` is
   gitignored. The canonical committed artifact is `db/parquet/*.parquet`.
2. **New Parquet files need `git add -f`.** `db/parquet/*.parquet` and
   `public/parquet/*.parquet` are gitignored but tracked, so a *new* table is
   silently skipped by a normal `git add` and the live site 404s while
   everything works locally. Non-Parquet files in those directories (such as
   `public/parquet/schema.json`) are not ignored and need no `-f`.
3. **Helper views live in two places.** `schema/schema.sql` defines 15 views;
   views do not survive a Parquet export, so the **six** the web app needs are
   re-created in the browser by the `helperViews` array in `src/db.js`. Add a
   new web-facing view to **both**, or the SQL tab loses it. Eight of the rest
   work only against a local DuckDB; the ninth, `funding_links`, is
   materialised by the export and ships as a parquet *table*.
   `docs/for_ai_agents.md` publishes that three-way split (HTTP-queryable
   Parquet / browser-only views / repo-only views), so keep it in step.
4. **Run Arrow results through `arrowToPlain()` / `unwrapRow()`** from
   `src/db.js` before view code touches them. DuckDB-Wasm returns Arrow
   Vectors that have `.length` but fail `Array.isArray()`.
5. **Never resolve a person by name alone.** Link on ORCID or `openalex_id`
   equality only. A missing identifier is far better than a wrong one — leave
   seed ID cells blank. (`scripts/wipe_bad_openalex_attributions.py` exists
   because a name-only resolver attached cardiologists to marine labs.)
6. **`person_id` is `sha1(f"{name.lower()}|{orcid}|{email.lower()}")[:16]`**
   (`scripts/load_facility_personnel.py`). Two older scripts use a different
   formula; use this one for anything new.
7. **Keep the vocabularies in sync.** `schema/vocab/` is canonical (used by
   ingest and QA); `public/vocab/` is served to the browser for filter labels.
   They have drifted before.
8. **Do not edit `data/raw/synthesis-networks/`** — it is a verbatim
   MIT-licensed snapshot of COMPASS-DOE/synthesis-networks. Treat it as
   upstream.
9. **`COMMIT_*.sh` are one-shot commit drivers**, gitignored, not source. They
   are not documentation of current state.
10. **Adding a page to `docs/`?** Add it to `DOC_PAGES` in
    `src/views/docs.js` (hand-maintained tab order) *and* to `DOC_META` in
    `scripts/generate_agent_surface.py` (title + description for `llms.txt`).
    The generator warns about either omission; treat the warning as an error.
11. **Do not add YAML frontmatter to `docs/*.md`.** The in-browser Markdown
    renderer in `src/views/docs.js` has no frontmatter handling — `---`
    becomes an `<hr>` and the YAML renders as visible paragraphs. OKF-style
    metadata for these pages lives in `DOC_META` instead.
12. **New `qa.py` invariants for the CSV/JSON-seeded tables must be gated on
    the table being non-empty.** `refresh-data.yml` runs `ingest.py`, which
    re-executes `schema.sql` and empties them in the CI database; their data
    lives in committed Parquet that ingest never touches.

## Cross-repo: the authoritative robots.txt

`scripts/generate_agent_surface.py` writes `/cod-kmap/robots.txt`, but crawlers
only honour robots.txt at a **host root**. This site is published under a path
prefix, so that file is a pointer for agents that look for one there — the
policy that binds is `https://tyson-swetnam.github.io/robots.txt`, which lives
in the separate **`tyson-swetnam.github.io`** repository.

That root file is already fully permissive and already carries a pointer block
for `/intro-gpt/`. To give cod-kmap the same treatment, add to it:

```
Sitemap: https://tyson-swetnam.github.io/cod-kmap/sitemap.xml

# --- AI agents and harnesses: COD Knowledge Map (/cod-kmap/) ---
# A MapLibre + DuckDB-Wasm app: the rendered page is an empty shell without
# JavaScript, but every document and data table is a plain static file.
#   Machine-readable outline:  https://tyson-swetnam.github.io/cod-kmap/llms.txt
#   Full corpus (one file):    https://tyson-swetnam.github.io/cod-kmap/llms-full.txt
#   Agent guide:               https://tyson-swetnam.github.io/cod-kmap/docs/for_ai_agents.md
#   Data endpoints:            https://tyson-swetnam.github.io/cod-kmap/docs/data_endpoints.md
#   Documentation as Markdown: append the filename to .../cod-kmap/docs/
#   Queryable Parquet tables:  .../cod-kmap/public/parquet/<table>.parquet
#   Table schemas (JSON):      .../cod-kmap/public/parquet/schema.json
```

The origin's OKF bundle entry at
`https://tyson-swetnam.github.io/okf/sites/cod-kmap.md` also predates this work
and mentions none of these endpoints; it is worth adding the same pointers
there.

## What actually ships

`.github/workflows/deploy.yml` stages only `index.html`, `favicon.svg`,
`src/`, `public/` and `docs/`, then runs
`scripts/generate_agent_surface.py`. Anything outside those paths — `agents/`,
`scripts/`, `schema/`, `data/`, this file — is **not** on the live site.
Reference it by its `github.com` URL, not a site URL.
