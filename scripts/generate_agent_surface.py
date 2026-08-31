#!/usr/bin/env python3
"""Generate llms.txt and llms-full.txt for the deployed site (stdlib only).

Run after the "Stage static site" step in deploy.yml:
    python3 scripts/generate_agent_surface.py _site

llms.txt indexes the human-authored docs (served raw at /docs/*.md) and —
because this site is a DuckDB-Wasm app — the queryable Parquet endpoints
under /public/parquet/, with a worked example of querying them remotely.
llms-full.txt concatenates the full docs content.
"""
import os
import re
import sys

SITE_URL = "https://tyson-swetnam.github.io/cod-kmap"
out_dir = sys.argv[1] if len(sys.argv) > 1 else "_site"

name = "COD Knowledge Map (cod-kmap)"
desc = ("Knowledge map for the Coastal Observatory Design (COD): facilities, "
        "people, funding, publications, and coastal datasets, served as a "
        "MapLibre + DuckDB-Wasm site whose Parquet tables are directly "
        "queryable over HTTP.")


def first_heading(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"#\s+(.+)", line)
            if m:
                return m.group(1).strip()
    return os.path.basename(path)


docs = sorted(f for f in os.listdir("docs") if f.endswith(".md"))
tables = sorted(os.path.splitext(f)[0] for f in os.listdir("public/parquet")
                if f.endswith(".parquet"))
views = re.findall(r"CREATE (?:OR REPLACE )?VIEW\s+(\w+)",
                   open("schema/schema.sql", encoding="utf-8").read())

lines = [f"# {name}", "", f"> {desc}", ""]

lines += [
    "## Documentation",
    "",
    "Raw markdown, served as-is:",
    "",
]
for f in docs:
    lines.append(f"- [{first_heading(os.path.join('docs', f))}]({SITE_URL}/docs/{f})")

lines += [
    "",
    "## Queryable data (Parquet over HTTP)",
    "",
    f"Every table below is a Parquet file at {SITE_URL}/public/parquet/<table>.parquet.",
    "They support HTTP range requests, so DuckDB (or any Parquet reader) can",
    "query them remotely without downloading the full dataset:",
    "",
    "```sql",
    "INSTALL httpfs; LOAD httpfs;",
    "SELECT f.canonical_name, f.country, count(*) AS n_people",
    f"FROM '{SITE_URL}/public/parquet/facility_personnel.parquet' fp",
    f"JOIN '{SITE_URL}/public/parquet/facilities.parquet' f USING (facility_id)",
    "GROUP BY 1, 2 ORDER BY n_people DESC LIMIT 5;",
    "```",
    "",
    "Tables: " + ", ".join(tables),
    "",
    "Helper views (definitions in the repo at schema/schema.sql; recreated",
    "in-browser by src/db.js): " + ", ".join(views),
    "",
    "GeoJSON fallback: " + f"{SITE_URL}/public/facilities.geojson",
    "",
    "Source repository: https://github.com/tyson-swetnam/cod-kmap",
    "",
    f"Full docs content in one file: {SITE_URL}/llms-full.txt",
]

with open(os.path.join(out_dir, "llms.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

full = [f"# {name}", "", f"> {desc}", "",
        "Full markdown content of every documentation page.", ""]
for fn in docs:
    path = os.path.join("docs", fn)
    full += ["-" * 72, "", f"## {first_heading(path)}",
             f"URL: {SITE_URL}/docs/{fn}", "",
             open(path, encoding="utf-8").read().rstrip(), ""]
with open(os.path.join(out_dir, "llms-full.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(full) + "\n")

print(f"agent surface: {len(docs)} docs, {len(tables)} parquet tables, "
      f"{len(views)} views -> {out_dir}/llms.txt, llms-full.txt")
