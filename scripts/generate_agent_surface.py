#!/usr/bin/env python3
"""Generate the deployed site's agent surface: robots.txt, sitemap.xml,
llms.txt, llms-full.txt, and public/parquet/schema.json.

Run after the "Stage static site" step in deploy.yml:

    python3 scripts/generate_agent_surface.py _site

Why this script exists
----------------------
cod-kmap is a single-page MapLibre + DuckDB-Wasm app. Fetching
https://tyson-swetnam.github.io/cod-kmap/ with anything that does not execute
JavaScript returns an empty shell, so the site is effectively invisible to AI
agents, text-only clients, and link-derived URL allowlists. Everything the app
displays, though, is a plain static file: the human-authored Markdown under
/docs/, the Parquet tables under /public/parquet/, the GeoJSON overlays, the
vocabulary CSVs. This script publishes the map from one to the other.

Five outputs, all written into the staged site directory:

  robots.txt      Permissive crawl policy naming the AI fetchers explicitly,
                  plus comment pointers to everything below. See ROBOTS_ROOT:
                  at a path prefix this file is advisory, not authoritative.
  sitemap.xml     Only genuinely fetchable URLs. The app's hash routes
                  (#/people, #/sql) are NOT separate URLs and are excluded.
  llms.txt        llmstxt.org-style outline: every doc page with a real
                  one-line description, then every Parquet table with its
                  column list, then worked DuckDB examples.
  llms-full.txt   Every doc page's full Markdown in one file, so an agent
                  with a one-fetch budget can still read the whole corpus.
  public/parquet/schema.json
                  Columns, types and row counts for every table, as JSON, so
                  an agent can plan a query without parsing Parquet footers.

Dependencies
------------
Standard library, plus duckdb if it is importable. duckdb is used only to read
column names, types and row counts out of the staged Parquet files. Without it
the script still writes every output, minus the column lists and schema.json,
and says so on stderr. deploy.yml installs it, so the live site always has them.

Maintenance
-----------
DOC_META below carries the title and one-line description of each page in
docs/. A page missing from DOC_META, or from DOC_PAGES in src/views/docs.js
(which drives the Docs tab), triggers a warning on stderr; --strict turns any
warning into a non-zero exit so CI can gate on the drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, timezone, datetime

# ── Identity ───────────────────────────────────────────────────────────────
SITE_URL = "https://tyson-swetnam.github.io/cod-kmap"
REPO_URL = "https://github.com/tyson-swetnam/cod-kmap"
RAW_URL = "https://raw.githubusercontent.com/tyson-swetnam/cod-kmap/main"
BRANCH = "main"

NAME = "COD Knowledge Map (cod-kmap)"
DESC = (
    "Knowledge map for the Coastal Observatory Design (COD): coastal research "
    "facilities, people, funding, publications and datasets across the "
    "Americas, published as a MapLibre + DuckDB-Wasm site whose Parquet tables "
    "are queryable directly over HTTP."
)

# AI and agentic fetchers named explicitly in robots.txt. "User-agent: *" with
# "Allow: /" already permits them; naming them states the intent unambiguously,
# which is what the opt-out-by-default crawlers look for. Kept in step with the
# list in UNM-CARC/docs.
AI_AGENTS = [
    "Googlebot", "Google-Extended", "GoogleOther", "Google-CloudVertexBot",
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",
    "ClaudeBot", "Claude-User", "Claude-SearchBot", "anthropic-ai",
    "PerplexityBot", "Perplexity-User",
    "cohere-ai", "Applebot-Extended", "CCBot", "meta-externalagent",
    "Amazonbot", "DuckAssistBot", "MistralAI-User",
]

# Crawlers only honour robots.txt at a host ROOT. This site is published under
# a path prefix, so /cod-kmap/robots.txt is a pointer file for agents that look
# for one, not the authoritative policy — that lives in the tyson-swetnam.github.io
# repository. See the "Cross-repo" section of AGENTS.md.
ROBOTS_ROOT = "https://tyson-swetnam.github.io/robots.txt"

# ── Per-page metadata for llms.txt ─────────────────────────────────────────
# filename -> (title, one-line description, OKF-style type, status).
#
# These descriptions are what makes llms.txt worth fetching, so they say what
# is actually IN each page rather than restating its title. They live here
# rather than in YAML frontmatter because the in-browser Markdown renderer
# (src/views/docs.js) has no frontmatter handling: a `---` block at the top of
# a doc renders as an <hr> followed by visible YAML paragraphs.
#
# Order here is the order in llms.txt. Keep in step with DOC_PAGES in
# src/views/docs.js — the generator warns when the two disagree.
DOC_META: dict[str, tuple[str, str, str, str]] = {
    "for_ai_agents.md": (
        "For AI agents",
        "How agents should consume this site: the llms.txt / raw-Markdown / "
        "Parquet endpoints, what is queryable over HTTP and what is not, trust "
        "and provenance signals, and what to do if you cannot fetch this host.",
        "Reference", "stable",
    ),
    "data_endpoints.md": (
        "Data endpoints",
        "Reference for every published data endpoint: all 46 Parquet tables "
        "with row counts and column lists, the GeoJSON overlays, the vocabulary "
        "CSVs, and worked DuckDB / Python / curl recipes against each.",
        "Reference", "stable",
    ),
    "cod_purpose_and_msi_handout.md": (
        "Purpose & MSI handout",
        "The CCZO proposal brief: NSF Mid-scale RI-1 24-598 deadlines, the four "
        "design pillars, the four Grand Challenge questions, the named PI/co-PI "
        "team, and a theme-to-cod-kmap mapping table.",
        "Report", "stable",
    ),
    "METHODS.md": (
        "Methods",
        "Table-by-table schema summary, the polygon overlay sources with feature "
        "counts, the dedup rules (URL match, RapidFuzz >= 92, 5 km haversine), "
        "the Parquet list DuckDB-Wasm loads, and the known data gaps.",
        "Reference", "stable",
    ),
    "team_scholars_datasets_methods.md": (
        "Team, scholars & data",
        "Build provenance for the Org chart, People and Data tabs: the WBS "
        "DuckLake snapshot run, the scholar cohorts, 72 datasets with 242 access "
        "endpoints, and the ordered script chain that produces each.",
        "Guide", "stable",
    ),
    "person_registry.md": (
        "Person registry",
        "How canonical_id works (orcid:/openalex: forms, merging on identifier "
        "equality and never on name), the core vs archive tier split that decides "
        "what ships to the browser, and six caveats on reading registry figures.",
        "Reference", "stable",
    ),
    "VALIDATION_REPORT.md": (
        "Validation report",
        "Run val-20260729T035944Z: 40,380 identity verdicts over 10,095 core "
        "registry rows, 2 ORCID conflicts, 114 inactive RORs, a co-author harvest "
        "at 30% coverage, and the OWL/SHACL conformance accounting.",
        "Report", "stable",
    ),
    "REFERENCES.md": (
        "References",
        "A 2026-04-26 snapshot of the 98-item COD Zotero group library (group "
        "5711743) as 58 articles, 8 books, 10 reports and 21 webpages, with the "
        "curl commands to regenerate it. BibTeX form: docs/references.bib.",
        "Reference", "stable",
    ),
    "reference_documents_report.md": (
        "Reference documents report",
        "Inventory of COD background reading (the 2018 landscape survey, WATERS "
        "plans, NSF solicitations) plus proposed schema extensions with DDL and "
        "dated built/superseded notes on each.",
        "Report", "stable",
    ),
    "map_visualization_plan.md": (
        "Map visualization plan",
        "Design of the Network tab's MVG cartogram: the grouping options with "
        "polygon counts, the three-step supergraph/subgraph/Voronoi layout, the "
        "PCL toggle, and the click and filter interaction rules.",
        "Guide", "stable",
    ),
    "NETWORK_FIX_METRICS.md": (
        "Network fix metrics",
        "M7 crossing-pair decomposition before and after 59 researchers move into "
        "interstitial space (694 -> 821 pairs, 86% within x between), correcting "
        "an earlier claim. Describes models, not the shipped layout.",
        "Report", "stable",
    ),
    "funding_pipeline_plan.md": (
        "Funding pipeline plan",
        "Funding table layout, the high/medium/low confidence rules, current NSF "
        "Awards API coverage, and the USAspending, Form-990 and budget-book "
        "passes still to run.",
        "Plan", "draft",
    ),
    "suitability_roadmap.md": (
        "Suitability roadmap",
        "Roadmap for a site-suitability layer that is NOT built yet: MEOW/Koppen/"
        "EEZ, GHSL and GBIF/OBIS source tables, a weighted H3 hex-cell scoring "
        "formula, and an effort estimate.",
        "Plan", "draft",
    ),
    "personnel_gap_research_plan.md": (
        "Personnel gap research",
        "How facility leaders are sourced per network group, every "
        "facility_personnel column recorded, the coverage figure achieved, and "
        "the rule that unresolvable leadership stays NULL rather than guessed.",
        "Guide", "stable",
    ),
    "orcid_enrichment_plan.md": (
        "ORCID enrichment",
        "The strict ORCID matcher's accept rules (family name, given name, and a "
        "distinctive employer-token gate), the pub.orcid.org endpoints used, the "
        "staff coverage achieved, and the resolution log.",
        "Guide", "stable",
    ),
    "google_scholar_enrichment_plan.md": (
        "Google Scholar enrichment",
        "Measured result of a four-tier Scholar-id lookup: OpenAlex and ORCID "
        "yield almost nothing, only a handful of people carry an id, and the "
        "reasoning for not scraping Scholar at all.",
        "Guide", "stable",
    ),
}

# ── Table groupings and notes for llms.txt ─────────────────────────────────
# Grouped so an agent can find the right table without reading all 46 column
# lists. Any Parquet file not listed here lands in "Other tables" and triggers
# a warning, so a new table is never silently mis-filed.
TABLE_GROUPS: list[tuple[str, str, list[str]]] = [
    ("Facilities and places",
     "The spine of the dataset. `facilities` is one row per catalogued site; "
     "`regions` is one row per overlay polygon; `facility_regions` is the "
     "spatial-containment edge between them.",
     ["facilities", "locations", "facility_types", "provenance",
      "regions", "facility_regions", "region_area_links"]),
    ("Research areas and networks",
     "Controlled-vocabulary topic and consortium membership for facilities.",
     ["research_areas", "research_areas_active", "area_links",
      "networks", "network_membership", "area_coverage_matrix"]),
    ("People",
     "Three human layers unified by `person_registry`: facility staff "
     "(`people`), the COD project team (`cod_team_members`), and the "
     "coastal-science scholar roster (`community_scholars`).",
     ["person_registry", "person_identity_source", "people",
      "facility_personnel", "registry_facilities",
      "cod_wbs", "cod_team_members",
      "community_scholars", "scholar_area_assignments",
      "person_areas", "person_area_metrics"]),
    ("Publications and collaboration",
     "Bibliometric layer harvested from OpenAlex, plus the co-authorship "
     "graphs and the identifier-validation audit trail.",
     ["publications", "authorship", "publication_topics",
      "collaborations", "registry_collaborations",
      "coauthor_edges", "coauthor_candidates", "person_validation"]),
    ("Funding",
     "Award-level funding events and their rollups. Amounts are NOMINAL USD: "
     "no inflation adjustment is applied anywhere in the published tables.",
     ["funders", "funding_events", "funding_links",
      "facility_area_funding", "funder_area_funding", "cpi_index_us"]),
    ("Coastal datasets",
     "Curated catalogue of external coastal datasets and their machine-readable "
     "access endpoints.",
     ["coastal_datasets", "dataset_endpoints", "dataset_facilities"]),
    ("Knowledge-map layout",
     "Precomputed groupings and coordinates that drive the Network tab's MVG "
     "cartogram. Derived artifacts, not source data.",
     ["facility_primary_groups", "person_primary_groups",
      "mvg_node_layout", "mvg_area_polygons", "mvg_layout_metrics"]),
]

# Short clarifications for tables whose name does not carry its meaning, or
# where a naive query would be read wrongly.
TABLE_NOTES: dict[str, str] = {
    "facilities": "mixes two populations — 210 research organisations and "
                  "3,309 protected-area units (facility_type prefixed "
                  "protected-area-). Filter on facility_type; do not read the "
                  "row count as a count of research facilities",
    "locations": "one row per site location; a facility may have several",
    "provenance": "per-record source URL, retrieval date and confidence",
    "regions": "overlay polygons as first-class rows; geometry itself is in "
               "public/overlays/*.geojson, not here",
    "funding_links": "a 7-column projection of funding_events kept for "
                     "backwards compatibility, materialised as its own file; "
                     "same 3,634 rows",
    "cpi_index_us": "intentionally EMPTY — the deflator series is not loaded, "
                    "so no real-dollar view can be computed yet",
    "person_registry": "core tier only: the full population stays in the local "
                       "DuckDB, so counts here are floors, not totals",
    "person_identity_source": "which rule (ORCID equality, OpenAlex id) bound "
                              "each identifier, and its conflicts",
    "registry_collaborations": "core-to-core co-publication edges only",
    "registry_facilities": "researcher-to-site links joined on ROR equality only",
    "coauthor_edges": "every row names the OpenAlex Work that proves it and the "
                      "identifier-equality rule that matched it",
    "coauthor_candidates": "review queue, NOT personnel records: co-authors seen "
                           "on registry members' works who are not registry rows",
    "person_validation": "append-only: one row per (registry row, check, run), "
                         "so filter to the latest run_id before counting",
    "publication_topics": "OpenAlex lists a work under every topic it carries, so "
                          "summing over a topic set double-counts multi-topic works",
    "community_scholars": "gated on a coastal-topic share, not name matching",
    "cod_wbs": "work-breakdown hierarchy for the COD project org chart",
    "area_coverage_matrix": "per-research-area coverage metrics used by the Stats tab",
    "mvg_layout_metrics": "two rows: before/after metrics for the cartogram layout",
}

# Tables that are fetchable over HTTP but are NOT registered by src/db.js, so
# the site's own SQL tab cannot see them. Externally they behave normally.
# Derived at runtime from src/db.js rather than hard-coded.
DB_JS = "src/db.js"


def warn(msg: str, warnings: list[str]) -> None:
    warnings.append(msg)
    print(f"warning: {msg}", file=sys.stderr)


def first_heading(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"#\s+(.+)", line)
            if m:
                return m.group(1).strip()
    return os.path.basename(path)


def doc_pages_from_js(path: str = "src/views/docs.js") -> list[str]:
    """Filenames listed in DOC_PAGES in src/views/docs.js.

    Used only to warn about drift: a new file in docs/ appears in llms.txt
    automatically (this script globs the directory) but stays invisible in the
    Docs tab until it is added to that hand-maintained array.
    """
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return []
    return re.findall(r"path:\s*'docs/([^']+)'", src)


def registered_tables(path: str = DB_JS) -> set[str]:
    """Table names src/db.js registers as DuckDB-Wasm views (eager + lazy).

    Parses the two array literals rather than every identifier in the file, so
    a table mentioned only in a comment does not count as registered.
    """
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return set()
    names: set[str] = set()
    for decl in (r"const tables = \[(.*?)\n  \];", r"const lazyTables = \[(.*?)\n  \];"):
        m = re.search(decl, src, re.S)
        if not m:
            continue
        # Strip // comments first: the arrays carry long explanatory comments
        # that quote table names and other words (e.g. "the 'core' tier"),
        # which would otherwise be counted as registered entries.
        body = re.sub(r"//[^\n]*", "", m.group(1))
        names |= set(re.findall(r"'([a-z0-9_]+)'", body))
    return names


def browser_views(path: str = DB_JS) -> list[str]:
    """Helper-view names in the helperViews array of src/db.js, in order.

    These are the ONLY views that exist in the running app. The generator must
    not claim that every view in schema/schema.sql is recreated in the browser —
    most are not.
    """
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return []
    m = re.search(r"const helperViews = \[(.*?)\n  \];", src, re.S)
    body = m.group(1) if m else ""
    return re.findall(r"CREATE OR REPLACE VIEW\s+(\w+)", body)


def schema_views(path: str = "schema/schema.sql") -> list[str]:
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return []
    return re.findall(r"CREATE (?:OR REPLACE )?VIEW\s+(\w+)", src)


def parquet_schemas(parquet_dir: str) -> tuple[dict, str | None]:
    """{table: {size_bytes, n_rows?, columns?}} for every published Parquet file.

    The file list and sizes come from the filesystem, so they are always
    available. Row counts and column types need a Parquet reader; when duckdb
    is not importable they are simply absent and the second return value says
    why, so llms.txt still lists every table (minus its columns) rather than
    losing the whole section.
    """
    if not os.path.isdir(parquet_dir):
        return {}, f"{parquet_dir} not found"

    out: dict[str, dict] = {}
    for fn in sorted(f for f in os.listdir(parquet_dir) if f.endswith(".parquet")):
        table = fn[: -len(".parquet")]
        out[table] = {"size_bytes": os.path.getsize(os.path.join(parquet_dir, fn))}

    try:
        import duckdb  # noqa: PLC0415  (optional dependency, by design)
    except ImportError:
        return out, "duckdb is not installed"

    con = duckdb.connect()
    try:
        for table in out:
            lit = os.path.join(parquet_dir, f"{table}.parquet").replace("'", "''")
            cols = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{lit}')").fetchall()
            n_rows = con.execute(
                f"SELECT count(*) FROM read_parquet('{lit}')").fetchone()[0]
            out[table]["n_rows"] = int(n_rows)
            out[table]["columns"] = [{"name": c[0], "type": c[1]} for c in cols]
    finally:
        con.close()
    return out, None


def human_bytes(n: int) -> str:
    return f"{n / 1_048_576:.2f} MB" if n >= 1_048_576 else f"{max(1, n // 1024)} KB"


def git_date(path: str, fallback: str) -> str:
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%cs", "--", path],
                           capture_output=True, text=True, timeout=15)
        out = r.stdout.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", out):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return fallback


# ── robots.txt ─────────────────────────────────────────────────────────────
def write_robots(out_dir: str) -> None:
    ai_block = "".join(f"User-agent: {a}\nAllow: /\n\n" for a in AI_AGENTS)
    text = f"""# {NAME} — {SITE_URL}/
# This site is published for people AND for AI agents.
#
# NOTE ON SCOPE: crawlers only honour robots.txt at a host root, and this site
# lives under a path prefix. So this file is a POINTER for agents that look for
# one here. The policy crawlers actually honour is the origin root's, at
# {ROBOTS_ROOT}, which is already fully permissive.
User-agent: *
Allow: /

{ai_block}Sitemap: {SITE_URL}/sitemap.xml

# AI agents and harnesses:
#   Machine-readable outline:   {SITE_URL}/llms.txt
#   Full corpus (one file):     {SITE_URL}/llms-full.txt
#   Agent guide:                {SITE_URL}/docs/for_ai_agents.md
#   Data endpoint reference:    {SITE_URL}/docs/data_endpoints.md
#
# Documentation is served as raw Markdown at {SITE_URL}/docs/<file>.md
# (text/markdown), and mirrored at {RAW_URL}/docs/<file>.md for sandboxes that
# allow github.com but not github.io.
#
# Data is served as Parquet at {SITE_URL}/public/parquet/<table>.parquet over
# HTTP range requests, so DuckDB can query a table in place without
# downloading it. Column names, types and row counts for every table:
#   {SITE_URL}/public/parquet/schema.json
#
# Source repository: {REPO_URL}
#   Rules for coding agents: {REPO_URL}/blob/{BRANCH}/AGENTS.md
"""
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(text)


# ── sitemap.xml ────────────────────────────────────────────────────────────
def write_sitemap(out_dir: str, docs: list[str], today: str) -> int:
    # Only real, fetchable URLs. The app's hash routes (#/people, #/sql) are
    # fragments of one document, not separate URLs — listing them would make
    # the sitemap wrong.
    urls: list[tuple[str, str, str]] = [
        (f"{SITE_URL}/", git_date("index.html", today), "1.0"),
        (f"{SITE_URL}/llms.txt", today, "0.9"),
        (f"{SITE_URL}/llms-full.txt", today, "0.9"),
    ]
    for fn in docs:
        urls.append((f"{SITE_URL}/docs/{fn}",
                     git_date(os.path.join("docs", fn), today), "0.7"))

    body = "\n".join(
        f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{mod}</lastmod>\n"
        f"    <priority>{pri}</priority>\n  </url>"
        for loc, mod, pri in urls)
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"{body}\n</urlset>\n")
    return len(urls)


# ── llms.txt ───────────────────────────────────────────────────────────────
def build_llms(docs: list[str], schemas: dict, schema_note: str | None,
               warnings: list[str]) -> list[str]:
    L: list[str] = [f"# {NAME}", "", f"> {DESC}", ""]

    L += [
        "## Start here",
        "",
        "The rendered page at the site root is a JavaScript application "
        "(MapLibre GL + DuckDB-Wasm): fetching it without executing JavaScript "
        "returns an empty shell. Do not scrape it. Everything it displays is a "
        "plain static file, addressable as follows.",
        "",
        "```",
        f"Documentation (Markdown)  {SITE_URL}/docs/<file>.md",
        f"  mirrored on GitHub      {RAW_URL}/docs/<file>.md      (branch {BRANCH}; a moving target)",
        f"Whole doc corpus          {SITE_URL}/llms-full.txt",
        f"Data table (Parquet)      {SITE_URL}/public/parquet/<table>.parquet",
        f"Table schemas (JSON)      {SITE_URL}/public/parquet/schema.json",
        f"Map points (GeoJSON)      {SITE_URL}/public/facilities.geojson",
        f"Overlay polygons          {SITE_URL}/public/overlays/<layer>.geojson",
        f"  layer index             {SITE_URL}/public/overlays/manifest.json",
        f"Controlled vocabularies   {SITE_URL}/public/vocab/<name>.csv",
        f"Crawl surface             {SITE_URL}/robots.txt · {SITE_URL}/sitemap.xml",
        f"Source repository         {REPO_URL}",
        "```",
        "",
        f"Agent guide, including what to do if you cannot reach this host: "
        f"{SITE_URL}/docs/for_ai_agents.md",
        "",
    ]

    # ── Documentation ─────────────────────────────────────────────────────
    L += ["## Documentation", "",
          "Human-authored Markdown, served as-is with content type "
          "`text/markdown`. Each entry gives the site address and the "
          "`raw.githubusercontent.com` mirror; both return the same bytes.",
          ""]
    for fn in docs:
        meta = DOC_META.get(fn)
        if meta is None:
            title = first_heading(os.path.join("docs", fn))
            desc, dtype, status = "", "", "stable"
            warn(f"docs/{fn} is not in DOC_META in scripts/"
                 "generate_agent_surface.py; add a title and description",
                 warnings)
        else:
            title, desc, dtype, status = meta
        suffix = ""
        if status == "draft":
            suffix = " (draft: describes work not yet done)"
        elif status == "deprecated":
            suffix = " (deprecated; kept for history)"
        L.append(f"- [{title}]({SITE_URL}/docs/{fn}): {desc}{suffix}"
                 f" Type: {dtype or 'unclassified'}."
                 f" Raw source: {RAW_URL}/docs/{fn}")
    L += ["",
          f"- [References in BibTeX]({SITE_URL}/docs/references.bib): the same "
          f"library as References above, as a flat BibTeX export. "
          f"Raw source: {RAW_URL}/docs/references.bib",
          ""]

    # ── Queryable data ────────────────────────────────────────────────────
    tables = sorted(schemas) if schemas else []
    total_bytes = sum(v.get("size_bytes", 0) for v in schemas.values())
    reg = registered_tables()

    L += ["## Queryable data (Parquet over HTTP)", ""]
    head = (f"{len(tables)} tables, {human_bytes(total_bytes)} in total. Every "
            f"table is one Parquet file at "
            f"`{SITE_URL}/public/parquet/<table>.parquet`. They are served with "
            f"HTTP range-request support, so DuckDB (or any Parquet reader) can "
            f"query one in place without downloading it:")
    if schema_note:
        head += f" (Column lists are omitted from this build: {schema_note}.)"
    L.append(head)
    L += [
        "",
        "```sql",
        "INSTALL httpfs; LOAD httpfs;",
        "SELECT canonical_name, acronym, facility_type, country",
        f"FROM '{SITE_URL}/public/parquet/facilities.parquet'",
        "-- slugs come from facility_types.parquet / vocab/facility_types.csv",
        "WHERE facility_type = 'university-marine-lab'",
        "ORDER BY canonical_name",
        "LIMIT 10;",
        "```",
        "",
        "Row counts below are of the PUBLISHED file. Several tables ship only a "
        "subset of what the pipeline builds (see the per-table notes), so treat "
        "counts as floors rather than totals.",
        "",
    ]

    grouped: set[str] = set()
    for title, blurb, members in TABLE_GROUPS:
        present = [t for t in members if t in schemas or not schemas]
        if not present:
            continue
        L += [f"### {title}", "", blurb, ""]
        for t in present:
            grouped.add(t)
            L.append(table_line(t, schemas, reg))
        L.append("")

    leftover = [t for t in tables if t not in grouped]
    if leftover:
        L += ["### Other tables", "",
              "Not yet filed into a group above.", ""]
        for t in leftover:
            warn(f"{t}.parquet is not in TABLE_GROUPS in scripts/"
                 "generate_agent_surface.py; add it to a group", warnings)
            L.append(table_line(t, schemas, reg))
        L.append("")

    # ── The honest three-way split ────────────────────────────────────────
    bviews = browser_views()
    sviews = [v for v in schema_views() if v not in bviews and v != "funding_links"]
    unregistered = sorted(t for t in tables if t not in reg)

    count = f"{len(tables)} " if tables else ""
    L += [
        "## What is queryable where",
        "",
        "Three different things are easy to confuse. Only the first is "
        "reachable over HTTP.",
        "",
        f"**1. Parquet tables — queryable by anyone.** All {count}files under "
        "`/public/parquet/` are fetchable and range-readable by any external "
        "DuckDB, pandas or Arrow client. Nothing else on this site is a "
        "queryable relation.",
        "",
    ]
    if unregistered:
        L += [
            "   Note: " + ", ".join(f"`{t}`" for t in unregistered) +
            " are published and externally queryable, but are NOT registered by "
            "the site's own `src/db.js`, so the in-app SQL tab cannot see them. "
            "That asymmetry affects the app only, not you.",
            "",
        ]
    L += [
        "**2. Helper views recreated in the browser — not fetchable.** "
        f"`src/db.js` recreates {len(bviews)} views inside DuckDB-Wasm on first "
        "use of the SQL tab: " + ", ".join(f"`{v}`" for v in bviews) + ". "
        "A view is not a file, so these have no URL. To use one externally, copy "
        f"its `CREATE OR REPLACE VIEW` body from "
        f"{RAW_URL}/src/db.js (the `helperViews` array) or from "
        f"{RAW_URL}/schema/schema.sql, and run it against the Parquet tables "
        "above — all of their base tables are published.",
        "",
        "**3. Views that exist only in the repo schema — not available either "
        "place.** " + ", ".join(f"`{v}`" for v in sviews) + " are defined in "
        f"`schema/schema.sql` but are NOT recreated in the browser. Reproduce "
        "them against a local DuckDB built from the committed Parquet "
        f"(`python scripts/rebuild_db_from_parquet.py`), or inline their SQL. "
        "`v_facility_funding_by_year_real` additionally needs `cpi_index_us`, "
        "which ships with zero rows, so it cannot return real-dollar figures yet.",
        "",
        "To run SQL that uses bare table names, bind them first:",
        "",
        "```sql",
        "INSTALL httpfs; LOAD httpfs;",
        "CREATE OR REPLACE VIEW facilities AS SELECT * FROM",
        f"  read_parquet('{SITE_URL}/public/parquet/facilities.parquet');",
        "CREATE OR REPLACE VIEW funding_events AS SELECT * FROM",
        f"  read_parquet('{SITE_URL}/public/parquet/funding_events.parquet');",
        "-- ...one per table you need, then run the query verbatim.",
        "```",
        "",
    ]

    # ── Other data endpoints ──────────────────────────────────────────────
    L += [
        "## Other data endpoints",
        "",
        f"- [facilities.geojson]({SITE_URL}/public/facilities.geojson): all "
        "catalogued facilities as GeoJSON points, the map's first-paint fallback. "
        "Properties: `id`, `name`, `acronym`, `type`, `country`, `parent_org`, "
        "`url`. Use this if you want coordinates without a Parquet reader.",
        f"- [overlays/manifest.json]({SITE_URL}/public/overlays/manifest.json): "
        "index of the polygon overlay layers — label, colour, category, and for "
        "the newer layers the authoritative source and feature count. Each key "
        f"`<id>` resolves to `{SITE_URL}/public/overlays/<id>.geojson`.",
        f"- [vocab/facility_types.csv]({SITE_URL}/public/vocab/facility_types.csv) "
        "(`slug,label,description`), "
        f"[vocab/research_areas.csv]({SITE_URL}/public/vocab/research_areas.csv) "
        "(`slug,label,gcmd_uri,parent_slug`), "
        f"[vocab/networks.csv]({SITE_URL}/public/vocab/networks.csv) "
        "(`slug,label,aliases,level,url`): the controlled vocabularies behind "
        "`facilities.facility_type`, `research_areas.slug` and `networks.slug`. "
        "Join on the slug.",
        f"- [parquet/schema.json]({SITE_URL}/public/parquet/schema.json): every "
        "table's columns, types and row count, as JSON. Fetch this instead of "
        "reading 46 Parquet footers.",
        "",
    ]

    # ── Provenance and caveats ────────────────────────────────────────────
    L += [
        "## Provenance and how much to trust this",
        "",
        "- Every facility record carries `source_url`, `retrieved_at` and a "
        "`confidence` grade; the `provenance` table holds them per record. "
        "Prefer high-confidence rows and cite the `source_url`, not this site.",
        "- People are linked to publications on ORCID or OpenAlex-id equality "
        "**only, never by name**. A missing identifier is deliberate; it is not "
        "a gap to be filled by guessing.",
        "- Funding amounts are nominal USD with no inflation adjustment.",
        "- Tables marked core-tier publish a subset of the pipeline's full "
        "output, so aggregate counts are floors.",
        "- Co-authorship degree 0 means *unmeasured*, not *publishes alone*: the "
        "graph harvest covers a fraction of registry identities.",
        "- `*_plan.md` pages marked draft above describe intended work. Do not "
        "read them as descriptions of shipped features.",
        "- Full method and caveat detail: "
        f"{SITE_URL}/docs/METHODS.md and {SITE_URL}/docs/person_registry.md.",
        "",
    ]

    # ── Worked examples ───────────────────────────────────────────────────
    L += [
        "## Worked examples",
        "",
        "Against the published Parquet, with no local data. All four run in a "
        "plain DuckDB shell.",
        "",
        "```sql",
        "-- 1. Facility counts by type, resolved through the vocabulary.",
        "INSTALL httpfs; LOAD httpfs;",
        "SELECT ft.label AS facility_type, count(*) AS n",
        f"FROM '{SITE_URL}/public/parquet/facilities.parquet' f",
        f"JOIN '{SITE_URL}/public/parquet/facility_types.parquet' ft",
        "  ON ft.slug = f.facility_type",
        "GROUP BY 1 ORDER BY n DESC;",
        "```",
        "",
        "```sql",
        "-- 2. Who funds the most distinct facilities.",
        "SELECT fu.name AS funder, fu.type,",
        "       count(DISTINCT fl.facility_id) AS facilities",
        f"FROM '{SITE_URL}/public/parquet/funding_links.parquet' fl",
        f"JOIN '{SITE_URL}/public/parquet/funders.parquet' fu",
        "  USING (funder_id)",
        "GROUP BY 1, 2 ORDER BY facilities DESC LIMIT 20;",
        "```",
        "",
        "```sql",
        "-- 3. Current key personnel with a resolved ORCID.",
        "SELECT f.acronym, f.canonical_name, p.name, fp.role, p.orcid",
        f"FROM '{SITE_URL}/public/parquet/facility_personnel.parquet' fp",
        f"JOIN '{SITE_URL}/public/parquet/people.parquet' p USING (person_id)",
        f"JOIN '{SITE_URL}/public/parquet/facilities.parquet' f USING (facility_id)",
        "WHERE fp.is_key_personnel AND p.orcid IS NOT NULL",
        "ORDER BY f.canonical_name, p.name;",
        "```",
        "",
        "```python",
        "# 4. Same idea from Python, no DuckDB: read one column of one table.",
        "import pandas as pd",
        f'base = "{SITE_URL}/public/parquet/"',
        'df = pd.read_parquet(base + "facilities.parquet",',
        '                     columns=["canonical_name", "country", "facility_type"])',
        'print(df.value_counts("country").head())',
        "```",
        "",
        "More recipes, and one example per table group: "
        f"{SITE_URL}/docs/data_endpoints.md",
        "",
    ]

    # ── Application routes ────────────────────────────────────────────────
    L += [
        "## Application routes (for people, not fetchable)",
        "",
        "The app is a hash router, so these are fragments of the single page at "
        f"{SITE_URL}/ — they are not separate URLs and are deliberately absent "
        "from sitemap.xml. Listed so you can point a human at the right tab.",
        "",
        "`#/` map · `#/browse` facility list · `#/network` MVG cartogram · "
        "`#/people` roster · `#/org` COD org chart · `#/data` dataset catalogue · "
        "`#/sql` SQL console · `#/stats` per-area dashboards · `#/docs/<slug>` "
        "documentation. Doc slugs are the filename stem, lower-cased, with "
        "underscores turned into dashes: `docs/for_ai_agents.md` is "
        "`#/docs/for-ai-agents`.",
        "",
    ]

    # ── Meta ──────────────────────────────────────────────────────────────
    L += [
        "## Meta",
        "",
        f"- [Full documentation corpus in one file]({SITE_URL}/llms-full.txt): "
        "every page above, concatenated, links made absolute. Prefer it over "
        "fetching pages one at a time.",
        f"- [For AI agents]({SITE_URL}/docs/for_ai_agents.md): endpoints, trust "
        "signals, and what to do if your sandbox cannot reach this host.",
        f"- [Data endpoints]({SITE_URL}/docs/data_endpoints.md): the per-table "
        "reference with worked recipes.",
        f"- [robots.txt]({SITE_URL}/robots.txt) and "
        f"[sitemap.xml]({SITE_URL}/sitemap.xml). Note that the authoritative "
        f"crawl policy is the origin root's: https://tyson-swetnam.github.io/robots.txt",
        f"- [Source repository]({REPO_URL}): the pipeline that builds all of this. "
        f"Rules for coding agents: {REPO_URL}/blob/{BRANCH}/AGENTS.md. "
        f"Canonical schema: {REPO_URL}/blob/{BRANCH}/schema/schema.sql.",
        "- Licence: MIT. Third-party data under "
        "`data/raw/synthesis-networks/` keeps its upstream MIT licence.",
        "",
    ]
    return L


def table_line(t: str, schemas: dict, reg: set[str]) -> str:
    """One llms.txt bullet for a Parquet table: size, caveat, column list."""
    info = schemas.get(t) or {}
    line = f"- `{t}`"
    if "n_rows" in info:
        line += f" — {info['n_rows']:,} rows, {human_bytes(info['size_bytes'])}"
    elif "size_bytes" in info:
        line += f" — {human_bytes(info['size_bytes'])}"
    note = TABLE_NOTES.get(t)
    if note:
        line += f". {note[0].upper()}{note[1:]}"
    if schemas and t not in reg:
        line += ". Not registered by the site's own app; externally queryable"
    if info.get("columns"):
        cols = ", ".join(f"{c['name']} {c['type']}" for c in info["columns"])
        line += f". Columns: {cols}"
    return line + "."


# ── llms-full.txt ──────────────────────────────────────────────────────────
def build_full(docs: list[str]) -> list[str]:
    F = [f"# {NAME} — full documentation corpus", "",
         f"> {DESC}", "",
         "Every documentation page below appears in full, prefixed by its "
         "canonical URL. This is the whole human-authored corpus; the DATA lives "
         f"in Parquet tables described at {SITE_URL}/llms.txt and "
         f"{SITE_URL}/docs/data_endpoints.md, and is not reproduced here.", ""]
    for fn in docs:
        meta = DOC_META.get(fn)
        title = meta[0] if meta else first_heading(os.path.join("docs", fn))
        F += ["-" * 72, "",
              f"## {title}",
              f"URL: {SITE_URL}/docs/{fn}",
              f"Raw source: {RAW_URL}/docs/{fn}", "",
              open(os.path.join("docs", fn), encoding="utf-8").read().rstrip(),
              ""]
    return F


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out_dir", nargs="?", default="_site",
                    help="staged site directory (default: _site)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any drift warning was emitted")
    args = ap.parse_args()
    out_dir = args.out_dir
    warnings: list[str] = []

    if not os.path.isdir(out_dir):
        print(f"error: {out_dir} not found — stage the site first",
              file=sys.stderr)
        return 2
    if not os.path.isdir("docs"):
        print("error: run this from the repository root", file=sys.stderr)
        return 2

    today = date.today().isoformat()

    # Documentation order: DOC_META order first, then anything else, so a new
    # file still ships (and warns) rather than disappearing.
    on_disk = sorted(f for f in os.listdir("docs") if f.endswith(".md"))
    docs = [f for f in DOC_META if f in on_disk]
    docs += [f for f in on_disk if f not in DOC_META]
    for f in DOC_META:
        if f not in on_disk:
            warn(f"DOC_META lists docs/{f}, which does not exist", warnings)

    # Docs-tab drift: a page can ship and be indexed yet be unreachable in the UI.
    in_tab = set(doc_pages_from_js())
    for f in docs:
        if f not in in_tab:
            warn(f"docs/{f} is not in DOC_PAGES in src/views/docs.js, so the "
                 "Docs tab has no route for it", warnings)

    # Parquet schemas, from the STAGED copy so they describe what is served.
    staged_parquet = os.path.join(out_dir, "public", "parquet")
    parquet_dir = staged_parquet if os.path.isdir(staged_parquet) \
        else os.path.join("public", "parquet")
    schemas, schema_note = parquet_schemas(parquet_dir)
    if schema_note:
        warn(f"Parquet column lists and schema.json omitted: {schema_note}",
             warnings)
    else:
        payload = {
            "site": SITE_URL,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url_template": f"{SITE_URL}/public/parquet/{{table}}.parquet",
            "note": ("Every table is one Parquet file served with HTTP "
                     "range-request support, so a DuckDB/Arrow client can query "
                     "it in place. Row counts are of the published file, which "
                     "for some tables is a core-tier subset of the pipeline's "
                     "full output."),
            "n_tables": len(schemas),
            "tables": schemas,
        }
        dest_dir = os.path.join(out_dir, "public", "parquet")
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "schema.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, indent=1, sort_keys=False)
            f.write("\n")

    write_robots(out_dir)
    n_urls = write_sitemap(out_dir, docs, today)

    lines = build_llms(docs, schemas, schema_note, warnings)
    with open(os.path.join(out_dir, "llms.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    full = build_full(docs)
    with open(os.path.join(out_dir, "llms-full.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(full).rstrip() + "\n")

    n_cols = sum(len(v.get("columns", ())) for v in schemas.values())
    print(f"agent surface -> {out_dir}/: "
          f"llms.txt ({len(docs)} docs, {len(schemas)} tables, {n_cols} columns), "
          f"llms-full.txt, robots.txt, sitemap.xml ({n_urls} urls)"
          + ("" if schema_note else ", public/parquet/schema.json"))
    if warnings:
        print(f"{len(warnings)} drift warning(s) above", file=sys.stderr)
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
