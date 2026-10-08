#!/usr/bin/env python3
"""Add one facilities_*.json file to the existing DB without a full rebuild.

`scripts/ingest.py` re-executes schema.sql, which empties every table —
including the 3,300+ R11 protected areas and the people / registry layers
that live only in committed parquet. For adding a handful of hand-researched
records to a live catalogue that is the wrong tool. This script takes the
same agent-schema JSON (see agents/README.md), checks it cannot collide with
a facility already in the DB, and inserts it with ingest.py's own
`insert_records`, so the two paths cannot drift.

The file must still live under data/raw/R*/facilities_*.json so that a full
ingest picks it up too.

Usage (after `scripts/rebuild_db_from_parquet.py`)::

    python scripts/ingest_supplement.py data/raw/R4/facilities_conservancies.json
    python scripts/ingest_supplement.py <file> --dry-run   # checks only

Steps:
  1. Validate facility_type / research_areas / networks against schema/vocab.
  2. Refuse a record that matches an existing facility from a different
     source file by URL, or by fuzzy name (>= 92) within 5 km — ingest.py's
     dedup rule. Re-running the same file is allowed and idempotent.
  3. Delete this file's previous locations / area_links / network_membership
     / provenance rows, then insert via ingest.insert_records.
  4. Point-in-polygon the new facilities against the regions already in the
     DB, then re-export parquet + public/facilities.geojson (export_parquet).
     populate_regions.populate() is not reused: it starts by deleting
     `regions`, which other tables reference by foreign key once loaded.

`funders` must be empty: ingest.insert_records writes them to the legacy
`funding_links`, which is now a view over `funding_events`. Funding edges
belong to the R9 / funding_events scripts.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import duckdb
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_parquet  # noqa: E402
from ingest import (  # noqa: E402
    DB_PATH, ROOT, VOCAB_DIR, Record, assign_ids, haversine_km, insert_records,
)
from populate_regions import load_region_rows, overlay_files  # noqa: E402
from shapely import STRtree  # noqa: E402
from shapely.geometry import Point  # noqa: E402


def vocab(name: str) -> set[str]:
    with (VOCAB_DIR / name).open() as f:
        return {row["slug"] for row in csv.DictReader(f)}


def validate(records: list[Record]) -> list[str]:
    types = vocab("facility_types.csv")
    areas = vocab("research_areas.csv")
    nets = vocab("networks.csv")
    errs = []
    for r in records:
        d, name = r.raw, r.raw.get("canonical_name") or "?"
        if d.get("facility_type") not in types:
            errs.append(f"{name}: unknown facility_type {d.get('facility_type')!r}")
        for a in d.get("research_areas") or []:
            if a not in areas:
                errs.append(f"{name}: unknown research_area {a!r}")
        for n in d.get("networks") or []:
            if str(n).lower() not in nets:
                errs.append(f"{name}: unknown network {n!r}")
        if d.get("funders"):
            errs.append(f"{name}: funders must be empty (see module docstring)")
        hq = d.get("hq") or {}
        if hq.get("lat") is None or hq.get("lng") is None:
            errs.append(f"{name}: hq lat/lng required")
        prov = d.get("provenance") or {}
        if not prov.get("source_url") or not prov.get("confidence"):
            errs.append(f"{name}: provenance needs source_url + confidence")
    return errs


def collisions(conn: duckdb.DuckDBPyConnection, records: list[Record]) -> list[str]:
    ours = {r.fid for r in records}
    existing = conn.execute(
        "SELECT facility_id, canonical_name, lower(trim(url)), hq_lat, hq_lng "
        "FROM main.facilities"
    ).fetchall()
    errs = []
    for r in records:
        d = r.raw
        url = (d.get("url") or "").strip().lower() or None
        here = (d["hq"]["lat"], d["hq"]["lng"])
        for fid, name, eurl, lat, lng in existing:
            if fid in ours:
                continue  # our own earlier run
            if url and eurl == url:
                errs.append(f"{d['canonical_name']}: same URL as existing {name!r} ({fid})")
            elif (lat is not None and lng is not None
                  and fuzz.token_set_ratio(d["canonical_name"], name) >= 92
                  and haversine_km(here, (lat, lng)) < 5):
                errs.append(f"{d['canonical_name']}: fuzzy-matches existing {name!r} ({fid})")
    return errs


def link_regions(conn: duckdb.DuckDBPyConnection, records: list[Record]) -> int:
    known = {rid for (rid,) in conn.execute("SELECT region_id FROM main.regions").fetchall()}
    rows = [r for r in load_region_rows(overlay_files()) if r["region_id"] in known]
    tree = STRtree([r["geometry"] for r in rows])
    n = 0
    for rec in records:
        pt = Point(rec.raw["hq"]["lng"], rec.raw["hq"]["lat"])
        for idx in tree.query(pt):
            if rows[int(idx)]["geometry"].contains(pt):
                conn.execute(
                    "INSERT OR IGNORE INTO main.facility_regions VALUES (?, ?, 'within', 0.0)",
                    [rec.fid, rows[int(idx)]["region_id"]],
                )
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = args.file.resolve()
    agent = path.parent.name
    if not (path.parent.parent == ROOT / "data" / "raw" and agent.startswith("R")
            and path.name.startswith("facilities_")):
        print(f"[error] {args.file} must be data/raw/R*/facilities_*.json", file=sys.stderr)
        return 2

    records = [Record(agent=agent, raw=rec) for rec in json.loads(path.read_text())]
    assign_ids(records)
    if len({r.fid for r in records}) != len(records):
        print("[error] duplicate canonical_name|acronym within the file", file=sys.stderr)
        return 1

    errs = validate(records)
    with duckdb.connect(str(DB_PATH), read_only=args.dry_run) as conn:
        errs += collisions(conn, records)
        if errs:
            for e in errs:
                print(f"[error] {e}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"[ok] {len(records)} records pass validation (dry run)")
            return 0

        fids = [r.fid for r in records]
        for table in ("locations", "area_links", "network_membership", "facility_regions"):
            conn.execute(f"DELETE FROM main.{table} WHERE facility_id IN ?", [fids])
        conn.execute(
            "DELETE FROM main.provenance WHERE record_type = 'facility' "
            "AND record_id IN ?", [fids],
        )
        insert_records(conn, records)
        print(f"[ok] inserted {len(records)} facilities from {path.relative_to(ROOT)}")
        print(f"[ok] facility_regions: {link_regions(conn, records)} containment edges")

    return export_parquet.main()


if __name__ == "__main__":
    sys.exit(main())
