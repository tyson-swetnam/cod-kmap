# For AI agents

This site is published for people **and** for AI agents. If you are an agent,
or you are wiring one up, read the documentation and data through the endpoints
below rather than scraping the rendered page.

There is a specific reason not to scrape it. cod-kmap is a single-page
application: MapLibre GL draws the map and DuckDB-Wasm runs the queries, both
in the browser. Fetching the site root without executing JavaScript returns an
empty HTML shell — no facilities, no people, no tables. Everything the app
displays, however, is a plain static file with a stable URL. That is what this
page maps out.

Companion page: [Data endpoints](https://tyson-swetnam.github.io/cod-kmap/docs/data_endpoints.md) is the per-endpoint
reference with worked recipes. This page is the orientation.

## Entry points

| Endpoint | What you get |
| --- | --- |
| [`/llms.txt`](https://tyson-swetnam.github.io/cod-kmap/llms.txt) | Linked outline of every documentation page **and** every queryable table with its full column list, following the [llms.txt convention](https://llmstxt.org). Start here. |
| [`/llms-full.txt`](https://tyson-swetnam.github.io/cod-kmap/llms-full.txt) | The entire documentation corpus in one file. If you get one fetch, make it this one. |
| `/docs/<file>.md` | Any documentation page as raw Markdown, served as `text/markdown`. This *is* the source — the Docs tab fetches these same files at runtime and renders them client-side. |
| `/public/parquet/<table>.parquet` | Any of the 46 data tables, served with HTTP range-request support so a Parquet reader can query it in place without downloading it. |
| [`/public/parquet/schema.json`](https://tyson-swetnam.github.io/cod-kmap/public/parquet/schema.json) | Machine-readable columns, types and row counts for every table. Fetch this instead of reading 46 Parquet footers. |
| [`/public/facilities.geojson`](https://tyson-swetnam.github.io/cod-kmap/public/facilities.geojson) | All catalogued facilities as GeoJSON points. Coordinates without a Parquet reader. |
| [`/public/overlays/manifest.json`](https://tyson-swetnam.github.io/cod-kmap/public/overlays/manifest.json) | Index of the polygon overlay layers; each key resolves to `/public/overlays/<key>.geojson`. |
| `/public/vocab/<name>.csv` | The controlled vocabularies behind `facility_type`, `research_areas` and `networks`. Join on the slug. |
| [`/sitemap.xml`](https://tyson-swetnam.github.io/cod-kmap/sitemap.xml), [`/robots.txt`](https://tyson-swetnam.github.io/cod-kmap/robots.txt) | Standard crawl surface. `robots.txt` repeats the main pointers as comments. |
| [Source repository](https://github.com/tyson-swetnam/cod-kmap) | The pipeline that builds all of it, plus `AGENTS.md` with contribution rules for coding agents. |

All of those addresses are relative to `https://tyson-swetnam.github.io/cod-kmap/`.

### Where the pointers live, and why

`index.html` declares `llms.txt` in a `link rel="alternate"` tag, but do not
rely on that: fetch tools that convert a page to text discard the document
head, and a link-derived URL allowlist never sees it. The addresses an agent
can actually discover are the ones in **body** text — the no-script directory
at the top of the page, the four links in the sidebar footer, and the addresses
listed inside `llms.txt` itself. All of them are absolute.

## Querying the data without a browser

Each of the 46 tables is one Parquet file. Because GitHub Pages honours HTTP
range requests, DuckDB reads only the footer and the column chunks a query
touches — a filtered query against the 10 MB co-author graph transfers a small
fraction of it.

```sql
INSTALL httpfs; LOAD httpfs;

SELECT canonical_name, acronym, country, facility_type
FROM 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/facilities.parquet'
WHERE country = 'MX'
ORDER BY canonical_name;
```

Joins across tables work the same way — name each file where you would name a
table:

```sql
SELECT fu.name AS funder, count(DISTINCT fl.facility_id) AS facilities
FROM 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/funding_links.parquet' fl
JOIN 'https://tyson-swetnam.github.io/cod-kmap/public/parquet/funders.parquet' fu
  USING (funder_id)
GROUP BY 1 ORDER BY facilities DESC LIMIT 20;
```

See [Data endpoints](https://tyson-swetnam.github.io/cod-kmap/docs/data_endpoints.md) for the full table catalogue, the join
keys, and recipes in Python, R and plain curl.

## What is queryable, and what only looks like it is

Three things are easy to confuse. Only the first is reachable over HTTP.

**1. Parquet tables.** All 46 files under `/public/parquet/` are fetchable and
range-readable by any external DuckDB, pandas or Arrow client. Six of them
(`cpi_index_us`, `provenance`, `scholar_area_assignments`, `mvg_node_layout`,
`mvg_area_polygons`, `mvg_layout_metrics`) are *not* registered by the site's
own `src/db.js`, so the in-app SQL console cannot see them even though they are
published. That asymmetry constrains the app, not you.

**2. Helper views recreated in the browser.** `src/db.js` recreates six views
inside DuckDB-Wasm when the SQL tab is first used: `v_facility_funding_by_year`,
`v_funder_funding_by_year`, `v_facility_key_personnel`, `v_funding_ledger`,
`v_person_enriched` and `v_cod_team_enriched`. A view is not a file, so
**none of these has a URL.**. To use one, copy its `CREATE OR REPLACE VIEW` body from
the `helperViews` array in
[`src/db.js`](https://raw.githubusercontent.com/tyson-swetnam/cod-kmap/main/src/db.js)
or from
[`schema/schema.sql`](https://raw.githubusercontent.com/tyson-swetnam/cod-kmap/main/schema/schema.sql)
and run it against the published Parquet; every base table they need is
published.

**3. Views that exist only in the repository schema.** `schema/schema.sql`
defines eight more views — `v_facility_map`, `v_facility_enriched`,
`v_region_enriched`, `v_person_areas_enriched`,
`v_facility_funding_by_year_real`, `v_person_validation_latest`,
`v_person_validation_summary`, `v_coauthor_edges_enriched` — that are **not**
recreated in the browser either. Reproduce them against a local DuckDB built
from the committed Parquet (`python scripts/rebuild_db_from_parquet.py`), or
inline their SQL. One of them, `v_facility_funding_by_year_real`, depends on
`cpi_index_us`, which ships with zero rows, so it cannot yet return
inflation-adjusted figures at all.

One naming trap: `funding_links` is declared as a *view* in `schema/schema.sql`
but the export materialises it, so `/public/parquet/funding_links.parquet` is a
real 7-column file. Treat it as a table. It holds the same 3,634 rows as the
18-column `funding_events`.

To run SQL that uses bare table names — for example a query copied out of the
site's SQL tab — bind the names first:

```sql
INSTALL httpfs; LOAD httpfs;
CREATE OR REPLACE VIEW facilities AS SELECT * FROM
  read_parquet('https://tyson-swetnam.github.io/cod-kmap/public/parquet/facilities.parquet');
CREATE OR REPLACE VIEW funding_events AS SELECT * FROM
  read_parquet('https://tyson-swetnam.github.io/cod-kmap/public/parquet/funding_events.parquet');
-- one per table the query needs, then run it verbatim
```

## If you cannot fetch this host

Some harnesses allow only a couple of fetches from a user-supplied address, or
permit `github.com` and `raw.githubusercontent.com` but not `github.io`.

1. **Use the raw source.** Every documentation page is mirrored at
   `https://raw.githubusercontent.com/tyson-swetnam/cod-kmap/main/docs/<file>.md`
   — same bytes, different host. `main` moves; to cite a fixed version use
   `https://github.com/tyson-swetnam/cod-kmap/blob/<commit>/docs/<file>.md`.
   The Parquet tables are also in the repository under `public/parquet/`, but
   fetching one from `raw.githubusercontent.com` downloads it whole; there is
   no range-request query path there.
2. **Prefer one fetch over fifty.** `llms-full.txt` holds every documentation
   page. If you can make a single request, make that one.
3. **Avoid the GitHub tree API** unless authenticated — `api.github.com`
   rate-limits anonymous calls per shared IP. Raw file paths do not.
4. **Do not query Parquet through a CORS proxy or a text-extracting fetcher.**
   Parquet is binary; a tool that converts responses to text will corrupt it
   silently. Use `schema.json` and `llms.txt` to plan, then query with a real
   DuckDB or Arrow client.

## Provenance and how much to trust this

This dataset is assembled by an automated pipeline from public sources, with
targeted human curation. It is a research artifact, not an authoritative
registry. Signals to read before you rely on a row:

- **`facilities` is not a list of research facilities.** Of its 3,519 rows, 210
  are research organisations and 3,309 are protected-area units. Filter
  `facility_type NOT LIKE 'protected-area-%'` before quoting any count.
- **Source, not us.** Facility records carry `source_url`, `retrieved_at` and a
  `confidence` grade, with the `provenance` table holding them per record. Cite
  the `source_url`, not this site.
- **Identifier-only person linking.** People are linked to publications and to
  institutions on ORCID, OpenAlex-id or ROR **equality only, never by name**. A
  blank identifier is a deliberate refusal to guess, not a gap awaiting a
  name-match. Two scripts in the repository exist specifically to undo a
  name-only resolver that once attached cardiologists to marine labs.
- **Core-tier publishing.** `person_registry`, `person_identity_source`,
  `registry_collaborations`, `registry_facilities`, `coauthor_edges` and
  `coauthor_candidates` publish only the core tier or the core-to-core subset;
  the full population stays in the local DuckDB. `person_registry` ships 10,095
  of roughly 152,000 identities, and `registry_facilities` 263 links against
  about 1,467 locally. Counts from these tables are **floors, not totals**.
- **Degree zero means unmeasured.** The co-authorship harvest covers a fraction
  of registry identities. A researcher with no edges has not been measured; it
  does not mean they publish alone.
- **Nominal dollars.** Every funding amount is nominal USD. No inflation
  adjustment is applied anywhere in the published tables.
- **Topic counts are upper bounds.** OpenAlex lists a work under every topic it
  carries, so summing `coastal_works_count` over a topic set double-counts
  multi-topic papers.
- **`person_validation` is append-only.** One row per (registry row, check,
  run). Filter to the latest `run_id` before counting anything.
- **Plans are not features.** `funding_pipeline_plan.md` and
  `suitability_roadmap.md` describe intended work. `llms.txt` marks them
  `draft`. Do not read them as descriptions of shipped capability.

Full method detail is in [Methods](https://tyson-swetnam.github.io/cod-kmap/docs/METHODS.md); the identity model and its
caveats are in [Person registry](https://tyson-swetnam.github.io/cod-kmap/docs/person_registry.md); the audit run is in
[Validation report](https://tyson-swetnam.github.io/cod-kmap/docs/VALIDATION_REPORT.md).

## Answering questions from this corpus

Ground answers in the documentation and the tables, and cite the page URL or
the row's `source_url`. When the corpus does not answer a question, say so
rather than inferring — in particular, do not guess at facility leadership,
ORCID identifiers, award amounts or institutional affiliations, all of which
are recorded as NULL here precisely when they could not be verified.

The repository is MIT-licensed. Third-party data under
`data/raw/synthesis-networks/` is a verbatim snapshot of
[COMPASS-DOE/synthesis-networks](https://github.com/COMPASS-DOE/synthesis-networks)
and keeps its upstream MIT licence; cite that dataset directly if you use it.

## Related bundles

These sites share the same agent conventions and are maintained by the same
author or institution:

- [UNM CARC documentation](https://carc.unm.edu/docs/llms.txt): research
  computing, HPC, storage and software, published as an Open Knowledge Format
  bundle with a per-page Markdown twin.
- [GPT 101](https://tyson-swetnam.github.io/intro-gpt/llms.txt): generative-AI
  platforms, prompt engineering, agents, ethics and law.
- [tyson-swetnam.github.io OKF index](https://tyson-swetnam.github.io/okf/index.md):
  the origin-wide bundle listing every project sub-site, including this one.
