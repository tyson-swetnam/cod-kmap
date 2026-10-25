#!/usr/bin/env python3
"""Harvest the addressable coastal-science author population from OpenAlex.

Replaces the three-stage candidate/hydrate/measure pipeline in
build_community_scholars.py for the bulk case. That pipeline grouped
/works by author to find candidates, then re-fetched each author, then
spent ~3 more requests per author computing coastal output. The /authors
endpoint filters on `topics.id` directly and returns everything those
three stages produced — ORCID, ROR-bearing institution, summary_stats,
and per-topic work counts — in one page of 200.

Measured on 2026-07-27 against the 15 resolved coastal topics:

    topics.id:<15 topics>                     1,126,552 authors
    … + works_count:>9                          190,397
    … + works_count:>19,cited_by_count:>99       95,776

The ≥10-works floor is the default: below it OpenAlex author records are
dominated by disambiguation shards (see MIN_STUB_WORKS in
enrich_registry_identifiers.py) rather than researchers.

`coastal_works_count` is summed from `topics[].count` on the author
record, so it costs no extra requests.

Everything harvested lands in `person_registry` with tier='archive'.
scripts/rank_person_registry.py then promotes a ranked core tier to
tier='core', and only that tier is exported to public/parquet for the
browser — a 190k-row parquet would make the site unusable.

Usage::

    python scripts/harvest_coastal_authors.py --min-works 20 --max-authors 20000
    python scripts/harvest_coastal_authors.py --resume
    python scripts/harvest_coastal_authors.py --load-only   # cache -> lake, offline

The load is one lake snapshot (scripts/cod_db.py); scripts/export_parquet.py
publishes it.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
import openalex_auth  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOPICS_CSV = ROOT / "data" / "datasets" / "coastal_topics.csv"
CACHE = ROOT / "data" / "raw" / "coastal_authors"
API = "https://api.openalex.org"
PER_PAGE = 200
SLEEP = 0.05

SELECT = ("id,display_name,orcid,ids,works_count,cited_by_count,summary_stats,"
          "last_known_institutions,topics,counts_by_year")


def coastal_topic_ids() -> list[str]:
    with TOPICS_CSV.open() as fh:
        rows = list(csv.DictReader(l for l in fh if not l.startswith("#")))
    ids = sorted({r["openalex_topic_id"].strip() for r in rows
                  if r["openalex_topic_id"].strip()})
    sentinels = [r for r in rows if r["openalex_topic_id"].strip() == "RESOLVE"]
    if sentinels:
        print(f"[error] {len(sentinels)} unresolved RESOLVE sentinel(s) in "
              f"{TOPICS_CSV.name}; harvesting against them would silently "
              f"drop those topics.", file=sys.stderr)
        raise SystemExit(2)
    return ids


def short(v) -> str | None:
    if not v:
        return None
    return str(v).rstrip("/").rsplit("/", 1)[-1] or None


def flatten(a: dict, coastal: set[str]) -> dict | None:
    stats = a.get("summary_stats") or {}
    insts = a.get("last_known_institutions") or []
    inst = insts[0] if insts else {}
    topics = a.get("topics") or []
    # OpenAlex lists a work under EVERY topic it carries, so summing
    # topics[].count over the coastal set counts a paper once per coastal
    # topic it touches — not once. Raw sums exceeded works_count for
    # 10,571 of 152,004 harvested authors, reaching a nonsensical
    # share of 3.0. The sum is still the best available signal of coastal
    # output volume, but it is an upper bound on distinct coastal papers,
    # so the share it feeds is clamped to 1.0 and named accordingly.
    cw_topic_hits = sum(t.get("count", 0) for t in topics
                        if short(t.get("id")) in coastal)
    works = a.get("works_count") or 0
    cw = min(cw_topic_hits, works) if works else cw_topic_hits
    oa_id = short(a.get("id"))
    orcid = short(a.get("orcid"))
    if not oa_id:
        return None
    years = [c.get("year") for c in (a.get("counts_by_year") or [])
             if c.get("works_count")]
    ids = a.get("ids") or {}
    recent = sum(c.get("works_count", 0) for c in (a.get("counts_by_year") or [])
                 if (c.get("year") or 0) >= date.today().year - 5)
    return dict(
        canonical_id=f"orcid:{orcid}" if orcid else f"openalex:{oa_id}",
        display_name=a.get("display_name") or "",
        orcid=orcid, openalex_id=oa_id,
        google_scholar_id=short(ids.get("scholar")) if ids.get("scholar") else None,
        homepage_url=ids.get("homepage") or None,
        affiliation=inst.get("display_name"),
        affiliation_ror=short(inst.get("ror")) if inst.get("ror") else None,
        affiliation_country=inst.get("country_code"),
        works_count=works or None, cited_by_count=a.get("cited_by_count"),
        h_index=stats.get("h_index"), i10_index=stats.get("i10_index"),
        two_yr_mean_citedness=stats.get("2yr_mean_citedness"),
        coastal_works_count=cw or None,
        coastal_share=round(cw / works, 4) if works else None,
        first_pub_year=min(years) if years else None,
        recent_works=recent,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--min-works", type=int, default=10)
    ap.add_argument("--min-coastal-works", type=int, default=3,
                    help="drop authors whose coastal output is below this; "
                         "the topic filter matches anyone with a single "
                         "coastal paper")
    ap.add_argument("--max-authors", type=int, default=0, help="0 = no cap")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--load-only", action="store_true",
                    help="skip the harvest (no OpenAlex requests, no API key) "
                         "and load the existing author cache into the lake")
    ap.add_argument("--cache", type=Path, default=CACHE,
                    help=f"cache directory (default: {CACHE.relative_to(ROOT)})")
    args = ap.parse_args()

    topics = coastal_topic_ids()
    coastal = set(topics)
    out_file = args.cache / "authors.ndjson"
    if args.load_only:
        if not out_file.exists():
            print(f"[error] --load-only: no cache at {out_file}", file=sys.stderr)
            return 2
        if args.dry_run:
            return 0
        return load_cache(args, out_file, topics)

    openalex_auth.require_api_key()
    sess = openalex_auth.openalex_session()
    flt = f"topics.id:{'|'.join(topics)},works_count:>{args.min_works - 1}"

    r = sess.get(f"{API}/authors", params={"filter": flt, "per_page": 1}, timeout=60)
    total = (r.json().get("meta") or {}).get("count", 0)
    print(f"[harvest] {total:,} authors match {len(topics)} coastal topics "
          f"with >= {args.min_works} works")

    args.cache.mkdir(parents=True, exist_ok=True)
    cursor_file = args.cache / "cursor.txt"
    seen: set[str] = set()
    if args.resume and out_file.exists():
        for line in out_file.read_text().splitlines():
            if line.strip():
                seen.add(json.loads(line)["openalex_id"])
        print(f"[harvest] resumed {len(seen):,} author(s) from cache")
    cursor = (cursor_file.read_text().strip()
              if args.resume and cursor_file.exists() else "*")

    kept = len(seen)
    pages = 0
    with out_file.open("a") as fh:
        while cursor:
            r = sess.get(f"{API}/authors", params={
                "filter": flt, "sort": "cited_by_count:desc",
                "select": SELECT, "per_page": PER_PAGE, "cursor": cursor},
                timeout=90)
            if not r.ok:
                print(f"[warn] HTTP {r.status_code}; stopping", file=sys.stderr)
                break
            j = r.json()
            for a in j.get("results", []):
                row = flatten(a, coastal)
                if row is None or row["openalex_id"] in seen:
                    continue
                if (row["coastal_works_count"] or 0) < args.min_coastal_works:
                    continue
                seen.add(row["openalex_id"])
                fh.write(json.dumps(row) + "\n")
                kept += 1
            pages += 1
            cursor = (j.get("meta") or {}).get("next_cursor")
            cursor_file.write_text(cursor or "")
            if pages % 10 == 0:
                fh.flush()
                print(f"  [harvest] page {pages}, {kept:,} kept")
            if args.max_authors and kept >= args.max_authors:
                print(f"  [harvest] reached --max-authors {args.max_authors:,}")
                break
            time.sleep(SLEEP)

    print(f"[harvest] {kept:,} author(s) with >= {args.min_coastal_works} "
          f"coastal works cached to {out_file}")
    if args.dry_run:
        return 0

    return load_cache(args, out_file, topics)


REG_COLS = ["canonical_id", "display_name", "orcid", "openalex_id",
            "google_scholar_id", "homepage_url", "affiliation",
            "affiliation_ror", "affiliation_country", "works_count",
            "cited_by_count", "h_index", "i10_index", "two_yr_mean_citedness",
            "coastal_works_count", "coastal_share", "first_pub_year",
            "is_scholar", "tier", "source", "source_url", "confidence",
            "retrieved_at"]
SRC_COLS = ["canonical_id", "field", "value", "method", "evidence",
            "source_url", "confidence", "retrieved_at"]
# person_identity_source's row key (cod_db.DERIVED_CONSTRAINTS).
SRC_KEY = ["canonical_id", "field", "value", "method", "source_url"]


def stage_rows(conn, stage: str, like: str, cols: list[str], rows: list[list]) -> None:
    """Bulk-load ``rows`` into TEMP table ``stage`` typed like ``like``'s columns.

    Goes through an Arrow table rather than executemany: at harvest scale
    (150k rows x 23 columns) a per-row executemany, even into a TEMP table,
    is the slow part of the run. The INSERT casts to the lake's types.

    The stage is created with explicit column DDL, NOT ``CREATE TEMP TABLE
    s AS SELECT ... LIMIT 0``: in DuckDB 2.0.0 a TEMP table created by an
    empty CTAS and then filled from a registered Arrow/pandas object with
    >= 122,880 rows (one row group) inside a transaction reports the right
    count(*), but every later scan of it (merge_upsert's copy, DISTINCT
    ON) sees zero rows — this load silently inserted nothing.
    """
    import pyarrow as pa
    types = dict(cod_db.rows(
        conn, "SELECT column_name, data_type FROM duckdb_columns() "
              "WHERE database_name = ? AND schema_name = 'main' AND table_name = ?",
        [cod_db.LAKE, like]))
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {stage} ("
                 + ", ".join(f"{cod_db.qident(c)} {types[c]}" for c in cols) + ")")
    if not rows:
        return
    tbl = pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)})
    conn.register("_arrow_in", tbl)
    try:
        conn.execute(f"INSERT INTO {stage} ({', '.join(cols)}) "
                     f"SELECT {', '.join(cols)} FROM _arrow_in")
    finally:
        conn.unregister("_arrow_in")


def assert_written(conn, written: list[str]) -> None:
    """cod_db.assert_integrity, raising only for the tables this run wrote.

    A violation in a written table rolls the run back. A violation in a
    table that merely points at one of them (an edge or verdict whose
    registry row an earlier rebuild dropped) is printed, not raised: this
    run did not cause it and cannot repair it, the script that owns that
    table recomputes it, and qa.py fails on it until then, so nothing stale
    is exported unnoticed.
    """
    v = cod_db.integrity_violations(conn, written)
    own = [x for x in v if x["table"] in written]
    if own:
        raise cod_db.IntegrityError(own)
    dep = [x for x in v if x["table"] not in written]
    if dep:
        print("[warn] rows in other tables point at rows that no longer exist "
              "(qa.py fails until their scripts are re-run):\n"
              + "\n".join("       " + line for line in
                          cod_db.format_violations(dep).splitlines()),
              file=sys.stderr)


def load_cache(args, out_file: Path, topics: list[str]) -> int:
    """Load the author cache into person_registry as tier='archive'.

    One transaction (one lake snapshot): both tables are staged in TEMP
    tables and merged insert-only, so a key already in the lake is never
    overwritten and a re-run of the same cache adds nothing.
    """
    try:
        conn = cod_db.connect(catalog=args.catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    existing = {r[0] for r in conn.execute(
        "SELECT openalex_id FROM person_registry "
        "WHERE openalex_id IS NOT NULL").fetchall()}
    existing_orcid = {r[0] for r in conn.execute(
        "SELECT orcid FROM person_registry WHERE orcid IS NOT NULL").fetchall()}

    today = date.today().isoformat()
    new_rows, prov, skipped = [], [], 0
    for line in out_file.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        # Never duplicate a human already in the registry. Both identifier
        # spaces are checked: the existing row may have been keyed on ORCID
        # while the harvest keyed on OpenAlex id, or the reverse. Merges
        # happen ONLY on ORCID or openalex_id equality — never on a name.
        if d["openalex_id"] in existing or (d["orcid"] and d["orcid"] in existing_orcid):
            skipped += 1
            continue
        existing.add(d["openalex_id"])
        if d["orcid"]:
            existing_orcid.add(d["orcid"])
        new_rows.append([
            d["canonical_id"], d["display_name"], d["orcid"], d["openalex_id"],
            d["google_scholar_id"], d["homepage_url"], d["affiliation"],
            d["affiliation_ror"], d["affiliation_country"], d["works_count"],
            d["cited_by_count"], d["h_index"], d["i10_index"],
            d["two_yr_mean_citedness"], d["coastal_works_count"],
            d["coastal_share"], d["first_pub_year"],
            True, "archive", "openalex-harvest",
            f"{API}/authors/{d['openalex_id']}", "high", today])
        prov.append([d["canonical_id"], "openalex_id", d["openalex_id"],
                     "openalex-topic-harvest",
                     f"matched {len(topics)} coastal topics with "
                     f"{d['coastal_works_count']} coastal works",
                     f"{API}/authors/{d['openalex_id']}", "high", today])

    st = {"inserted": 0}
    if new_rows:
        with cod_db.transaction(
                conn, f"harvest_coastal_authors: +{len(new_rows):,} archive-tier "
                      f"registry rows from {len(topics)} coastal topics"):
            stage_rows(conn, "_harvest", "person_registry", REG_COLS, new_rows)
            stage_rows(conn, "_harvest_src", "person_identity_source", SRC_COLS, prov)
            # insert_only: the old plain INSERT relied on the PRIMARY KEY to
            # refuse a clash; the lake has none, so merge on the key instead.
            st = cod_db.merge_upsert(conn, "person_registry", ["canonical_id"],
                                     "_harvest", insert_only=True)
            cod_db.merge_upsert(conn, "person_identity_source", SRC_KEY,
                                "_harvest_src", insert_only=True)
            assert_written(conn, ["person_registry", "person_identity_source"])
    n = cod_db.scalar(conn, "SELECT COUNT(*) FROM person_registry")
    print(f"[lake] +{st['inserted']:,} new archive-tier rows "
          f"({skipped:,} already in the registry); person_registry now {n:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
