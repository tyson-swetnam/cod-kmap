#!/usr/bin/env python3
"""Load the curated coastal dataset catalogue into the lake.

The catalogue at data/datasets/coastal_datasets.json is hand-researched
(see docs/team_scholars_datasets_methods.md) and is the single source of
truth: this script DELETEs and re-inserts both tables on every run (one
transaction, so one lake snapshot), so editing the JSON and re-running is
the whole update workflow. scripts/export_parquet.py publishes the tables.

Each dataset carries one or more access endpoints. The point of the
catalogue is the *machine* endpoints — an ERDDAP base, a THREDDS
catalogue, a REST API root — so a human portal link alone is treated as
thin but legal.

Usage::

    python scripts/load_coastal_datasets.py
    python scripts/load_coastal_datasets.py --json path/to/other.json
    python scripts/load_coastal_datasets.py --dry-run
    python scripts/load_coastal_datasets.py --check-urls   # needs network

Idempotent: re-running with the same JSON produces identical tables.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def rel(path: Path) -> str:
    """Repo-relative path for display, tolerating paths outside the repo.

    Path.relative_to raises for a relative argument (ROOT is absolute) and
    for anything outside the tree, so calling it directly on a user-supplied
    --json crashed on exactly the invocation the usage text documents.
    """
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)
DEFAULT_JSON = ROOT / "data" / "datasets" / "coastal_datasets.json"
TABLES = ["coastal_datasets", "dataset_endpoints"]

# Kept in sync with the same-named sets in scripts/qa.py. Both lists are
# deliberately duplicated rather than promoted to schema/vocab/*.csv: the
# vocab CSVs are served to the browser for filter labels and would then
# need the schema/vocab + public/vocab sync dance for two constants that
# only the loader and the QA gate read.
DATASET_CATEGORIES = {
    "observing-system",     # sustained in-situ observing networks
    "monitoring-program",   # periodic survey / assessment programs
    "data-portal",          # discovery + access front ends
    "remote-sensing",       # satellite / airborne products
    "synthesis-network",    # cross-site synthesis collectives
    "model-output",         # operational forecast / hindcast output
    "archive",              # long-term stewardship archives
    "mapping-product",      # derived map / land-cover products
}
ENDPOINT_TYPES = {
    "erddap", "thredds", "opendap", "ogc-wms", "ogc-wfs", "ogc-api",
    "rest-api", "s3", "ftp", "portal", "doi", "stac",
}
# Endpoint types that represent a machine-readable service, as opposed to
# a human landing page. Used only for the coverage report.
MACHINE_TYPES = ENDPOINT_TYPES - {"portal"}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]+$")
URL_RE = re.compile(r"^https?://", re.I)

DATASET_COLS = [
    "dataset_id", "name", "acronym", "provider", "program", "parent_dataset_id",
    "category", "description", "network_id", "spatial_coverage",
    "temporal_start", "temporal_end", "license", "doi", "homepage_url",
    "variables", "update_frequency", "source", "source_url", "retrieved_at",
    "confidence", "notes",
]
ENDPOINT_COLS = [
    "dataset_id", "endpoint_type", "url", "label", "format_notes", "auth_required",
]


def validate(records: list[dict], known_networks: set[str]) -> list[str]:
    """Return a list of fatal problems; warn-and-fix issues are applied
    to `records` in place."""
    fatal: list[str] = []
    ids: set[str] = set()

    for rec in records:
        did = rec.get("dataset_id") or ""
        if not SLUG_RE.match(did):
            fatal.append(f"bad dataset_id {did!r} (need ^[a-z0-9][a-z0-9-]+$)")
            continue
        if did in ids:
            fatal.append(f"duplicate dataset_id {did!r}")
            continue
        ids.add(did)

        if not (rec.get("name") or "").strip():
            fatal.append(f"{did}: missing name")
        if not (rec.get("provider") or "").strip():
            fatal.append(f"{did}: missing provider")
        if rec.get("category") not in DATASET_CATEGORIES:
            fatal.append(f"{did}: category {rec.get('category')!r} not in vocab")

        nid = rec.get("network_id")
        if nid and nid not in known_networks:
            print(f"[warn] {did}: network_id {nid!r} not in networks table -> NULL")
            rec["network_id"] = None

        eps = rec.get("endpoints") or []
        if not eps:
            fatal.append(f"{did}: no endpoints (need at least one)")
        seen_ep: set[tuple[str, str]] = set()
        for ep in eps:
            et = ep.get("endpoint_type")
            url = (ep.get("url") or "").strip()
            if et not in ENDPOINT_TYPES:
                fatal.append(f"{did}: endpoint_type {et!r} not in vocab")
                continue
            if not URL_RE.match(url):
                fatal.append(f"{did}: endpoint url not http(s): {url!r}")
                continue
            key = (et, url)
            if key in seen_ep:
                fatal.append(f"{did}: duplicate endpoint {key} (violates the PK)")
            seen_ep.add(key)

    for rec in records:
        parent = rec.get("parent_dataset_id")
        if parent and parent not in ids:
            fatal.append(
                f"{rec.get('dataset_id')}: parent_dataset_id {parent!r} not in the catalogue")
    return fatal


def check_urls(records: list[dict]) -> None:
    """Optional liveness probe. Off by default — it needs outbound
    network access, which the CI and sandboxed environments lack."""
    try:
        import requests
    except ImportError:
        print("[error] --check-urls needs requests: pip install requests", file=sys.stderr)
        return
    session = requests.Session()
    session.headers["User-Agent"] = "cod-kmap/0.1 (+https://github.com/tyson-swetnam/cod-kmap)"
    for rec in records:
        for ep in rec.get("endpoints") or []:
            url = ep["url"]
            try:
                r = session.head(url, timeout=15, allow_redirects=True)
                if r.status_code >= 400:
                    r = session.get(url, timeout=20, stream=True)
                flag = "ok " if r.status_code < 400 else "BAD"
                print(f"[{flag}] {r.status_code} {rec['dataset_id']:28s} {url}")
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"[ERR] {rec['dataset_id']:28s} {url} -> {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report; write nothing")
    ap.add_argument("--check-urls", action="store_true",
                    help="probe every endpoint URL (needs network)")
    ap.add_argument("--skip-export", action="store_true",
                    help="deprecated no-op: this script never writes parquet; "
                         "run scripts/export_parquet.py")
    args = ap.parse_args()

    if not args.json.exists():
        print(f"[error] catalogue not found: {args.json}", file=sys.stderr)
        return 2
    records = json.loads(args.json.read_text())
    if not isinstance(records, list):
        print(f"[error] {args.json} must contain a JSON array", file=sys.stderr)
        return 2
    if not records:
        # Loading proceeds by DELETE-then-insert, so an empty catalogue would
        # empty both tables and then fail in executemany, leaving nothing
        # behind. Refuse instead: emptying the catalogue is never the intent
        # of running the loader.
        print(f"[error] {rel(args.json)} contains no datasets — refusing to "
              f"empty coastal_datasets/dataset_endpoints", file=sys.stderr)
        return 2
    print(f"[read] {len(records)} datasets from {rel(args.json)}")

    conn = cod_db.connect(read_only=args.dry_run,
                          catalog=args.catalog, data=args.lake_data)
    known_networks = {r[0] for r in cod_db.rows(conn, "SELECT network_id FROM networks")}

    fatal = validate(records, known_networks)
    if fatal:
        print(f"\n[error] {len(fatal)} validation failure(s):", file=sys.stderr)
        for f in fatal:
            print(f"  - {f}", file=sys.stderr)
        return 1

    if args.check_urls:
        check_urls(records)

    n_eps = sum(len(r.get("endpoints") or []) for r in records)
    machine = sum(1 for r in records
                  if any(e["endpoint_type"] in MACHINE_TYPES for e in r["endpoints"]))
    print(f"[ok] validated: {len(records)} datasets, {n_eps} endpoints, "
          f"{machine} with a machine-readable service")

    if args.dry_run:
        print("[dry-run] nothing written")
        return 0

    ds_rows = [[rec.get(c) for c in DATASET_COLS] for rec in records]
    ep_rows = [
        [rec["dataset_id"], ep["endpoint_type"], ep["url"],
         ep.get("label"), ep.get("format_notes"), bool(ep.get("auth_required"))]
        for rec in records for ep in rec["endpoints"]
    ]

    # Stage both tables in TEMP tables shaped like the lake tables (LIMIT 0
    # copies the column types), then replace the lake rows with one
    # statement each.
    for table, stage, cols, data in (
            ("coastal_datasets", "_ds_stage", DATASET_COLS, ds_rows),
            ("dataset_endpoints", "_ep_stage", ENDPOINT_COLS, ep_rows)):
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {stage} AS "
                     f"SELECT {', '.join(cols)} FROM {table} LIMIT 0")
        conn.executemany(
            f"INSERT INTO {stage} VALUES ({', '.join('?' * len(cols))})", data)

    # The JSON is the source of truth, so replace wholesale rather than
    # upserting — a dataset removed from the file should disappear here.
    with cod_db.transaction(conn, f"load_coastal_datasets: {len(ds_rows)} datasets, "
                                  f"{len(ep_rows)} endpoints from {rel(args.json)}"):
        conn.execute("DELETE FROM dataset_endpoints")
        conn.execute("DELETE FROM coastal_datasets")
        conn.execute(f"INSERT INTO coastal_datasets ({', '.join(DATASET_COLS)}) "
                     f"SELECT {', '.join(DATASET_COLS)} FROM _ds_stage")
        conn.execute(f"INSERT INTO dataset_endpoints ({', '.join(ENDPOINT_COLS)}) "
                     f"SELECT {', '.join(ENDPOINT_COLS)} FROM _ep_stage")
        cod_db.assert_integrity(conn, TABLES)
    print(f"[lake] replaced {len(ds_rows)} datasets, {len(ep_rows)} endpoints "
          f"(run scripts/export_parquet.py to publish)")

    for cat, n in conn.execute(
        "SELECT category, COUNT(*) FROM coastal_datasets GROUP BY category ORDER BY 2 DESC, 1"
    ).fetchall():
        print(f"  {cat:20s} {n}")
    for et, n in conn.execute(
        "SELECT endpoint_type, COUNT(*) FROM dataset_endpoints GROUP BY endpoint_type "
        "ORDER BY 2 DESC, 1"
    ).fetchall():
        print(f"  endpoint {et:14s} {n}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
