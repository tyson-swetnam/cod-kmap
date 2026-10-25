"""cod-kmap ingest pipeline (D2 deliverable).

Reads all research-agent JSON under data/raw/R*/, normalizes and deduplicates
records, geocodes missing coordinates, upserts everything into the DuckLake
store of record (db/cod_kmap_catalog.sqlite + db/lake/, see scripts/cod_db.py)
per schema/schema.sql, and records provenance.

The lake must exist (scripts/rebuild_lake_from_parquet.py); on a fresh clone
cod_db.connect raises LakeNotFound. This is an upsert, not a rebuild: the
old ingest re-executed schema.sql, which emptied every table, including the
3,300+ R11 protected areas (scripts/coastal_research/ingest_r11.py) and the
people / registry / dataset layers that live only in committed parquet.
Facilities already in the lake and absent from data/raw stay as they are.

What a re-run changes, table by table:

  * vocab (facility_types, research_areas, networks): replaced from
    schema/vocab/*.csv, which is the whole truth for them.
  * facilities / locations: upserted by key, last record wins (the old
    INSERT-OR-REPLACE). ``ror`` is normalised to the bare id and only ever
    fills in or corrects a value — a record without one never erases the
    ROR another script found. ``created_at`` keeps its lake value.
  * area_links: the raw record is the whole truth for the facilities it
    covers, so their rows are replaced.
  * network_membership, funders, funding_events: insert-only; other
    scripts add rows to these facilities that the raw records do not know.
  * provenance: this agent's row for each facility is rewritten.
  * regions / region_area_links / facility_regions: re-derived from the
    region overlays (scripts/populate_regions.py) unless --skip-regions.

One run is one lake snapshot (``ingest: ...``), checked by
cod_db.assert_integrity before it commits. Publish afterwards with
``python scripts/qa.py && python scripts/export_parquet.py``.

Usage:
    python scripts/ingest.py                # upsert data/raw/R*/ into the lake
    python scripts/ingest.py --skip-geocode # use cache only
    python scripts/ingest.py --catalog /tmp/x/cat.sqlite   # a scratch lake
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
VOCAB_DIR = ROOT / "schema" / "vocab"

# Every table this script writes; assert_integrity checks them (and every
# foreign key that points at them) before the snapshot commits.
INGEST_TABLES = [
    "facility_types", "research_areas", "networks", "facilities", "locations",
    "funders", "funding_events", "area_links", "network_membership",
    "provenance", "ingest_runs",
]
REGION_TABLES = ["regions", "region_area_links", "facility_regions"]


def facility_id(name: str, acronym: str | None) -> str:
    key = (name or "").strip().lower() + "|" + (acronym or "").strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def funder_id(name: str) -> str:
    return hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:16]


def location_id(fid: str, label: str | None) -> str:
    return hashlib.sha1((fid + "|" + (label or "")).encode("utf-8")).hexdigest()[:16]


def funding_event_id(fuid: str, fid: str, award_id: str | None = None,
                     fiscal_year: int | None = None) -> str:
    """funding_events.event_id — the formula of
    scripts/migrate_funding_events.py:event_id, so a funder edge from a raw
    record is the same event as the legacy funding_links row the migration
    already carried over, not a second copy of it."""
    key = "|".join([fuid or "", fid or "", (award_id or "").strip(),
                    str(fiscal_year) if fiscal_year is not None else ""])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_ROR_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?ror\.org/", re.I)


def normalise_ror(value: Any) -> str | None:
    """The bare ROR id ('0abcd1234'), from a bare id or any ror.org URL.

    link_registry_facilities.py joins facilities.ror by equality to
    person_registry.affiliation_ror, which OpenAlex gives bare, so a
    'https://ror.org/0…' value silently links to nobody (qa.py checks
    ``^0[a-z0-9]{8}$``)."""
    if value is None:
        return None
    s = _ROR_PREFIX.sub("", str(value).strip()).strip("/").lower()
    return s or None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(x))


@dataclass
class Record:
    agent: str
    raw: dict
    fid: str = ""
    merged_from: list[str] = field(default_factory=list)


def load_raw_records() -> list[Record]:
    records: list[Record] = []
    for agent_dir in sorted(RAW_DIR.glob("R*")):
        agent = agent_dir.name
        for path in sorted(agent_dir.glob("facilities_*.json")):
            try:
                with path.open() as f:
                    payload = json.load(f)
            except json.JSONDecodeError as e:
                print(f"[warn] {path} is not valid JSON: {e}", file=sys.stderr)
                continue
            if not isinstance(payload, list):
                print(f"[warn] {path} is not a JSON array, skipping", file=sys.stderr)
                continue
            for rec in payload:
                rec["_source_file"] = str(path.relative_to(ROOT))
                records.append(Record(agent=agent, raw=rec))
    return records


def assign_ids(records: list[Record]) -> None:
    for r in records:
        r.fid = facility_id(r.raw.get("canonical_name", ""), r.raw.get("acronym"))


def dedup(records: list[Record]) -> list[Record]:
    """Merge records across agents using id / url / fuzzy-name + proximity."""
    by_key: dict[str, Record] = {}
    url_index: dict[str, Record] = {}

    for r in records:
        name = (r.raw.get("canonical_name") or "").strip()
        url = (r.raw.get("url") or "").strip().lower() or None

        match: Record | None = by_key.get(r.fid)
        if match is None and url and url in url_index:
            match = url_index[url]
        if match is None:
            for cand in by_key.values():
                if fuzz.token_set_ratio(name, cand.raw.get("canonical_name", "")) >= 92:
                    try:
                        a = (float(r.raw["hq"]["lat"]), float(r.raw["hq"]["lng"]))
                        b = (float(cand.raw["hq"]["lat"]), float(cand.raw["hq"]["lng"]))
                        if haversine_km(a, b) < 5:
                            match = cand
                            break
                    except (KeyError, TypeError, ValueError):
                        continue

        if match is None:
            by_key[r.fid] = r
            if url:
                url_index[url] = r
            continue

        # merge r into match, preferring higher-confidence source.
        merged = merge(match.raw, r.raw)
        match.raw = merged
        match.merged_from.append(r.agent)

    return list(by_key.values())


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, None: 0}


def pick(a: Any, b: Any, prov_a: dict | None, prov_b: dict | None) -> Any:
    if a in (None, "", []):
        return b
    if b in (None, "", []):
        return a
    ra = CONFIDENCE_RANK.get((prov_a or {}).get("confidence"))
    rb = CONFIDENCE_RANK.get((prov_b or {}).get("confidence"))
    return a if ra >= rb else b


def merge(dst: dict, src: dict) -> dict:
    pa, pb = dst.get("provenance"), src.get("provenance")
    out = dict(dst)
    for k in [
        "canonical_name", "acronym", "parent_org", "facility_type", "country",
        "region", "url", "contact", "established", "ror",
    ]:
        out[k] = pick(dst.get(k), src.get(k), pa, pb)

    # HQ
    hq_a, hq_b = dst.get("hq") or {}, src.get("hq") or {}
    out["hq"] = {
        "address": pick(hq_a.get("address"), hq_b.get("address"), pa, pb),
        "lat": pick(hq_a.get("lat"), hq_b.get("lat"), pa, pb),
        "lng": pick(hq_a.get("lng"), hq_b.get("lng"), pa, pb),
    }

    # Union list fields
    def union(key: str) -> list:
        seen, result = set(), []
        for item in (dst.get(key) or []) + (src.get(key) or []):
            marker = json.dumps(item, sort_keys=True) if isinstance(item, dict) else item
            if marker not in seen:
                seen.add(marker)
                result.append(item)
        return result

    out["locations"] = union("locations")
    out["research_areas"] = union("research_areas")
    out["networks"] = union("networks")
    out["funders"] = union("funders")
    out["provenance"] = dst.get("provenance") or src.get("provenance")
    return out


def geocode_missing(records: list[Record], skip: bool) -> None:
    if skip:
        return
    from geocode import Geocoder  # needs geopy; not imported with --skip-geocode
    gc = Geocoder()
    for r in records:
        hq = r.raw.setdefault("hq", {})
        if (hq.get("lat") is None or hq.get("lng") is None) and hq.get("address"):
            coords = gc.lookup(hq["address"])
            if coords:
                hq["lat"], hq["lng"] = coords


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    # schema.sql cannot run against the lake (it fails on the first
    # PRIMARY KEY), and running it on the old .duckdb emptied every table.
    # cod_db.ensure_schema adds whatever tables or columns it has gained
    # since the lake was built, refreshes the views, and never drops
    # anything.
    for change in cod_db.ensure_schema(conn):
        print(f"[schema] {change}")


def load_vocab(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("DELETE FROM main.facility_types")
    conn.execute(
        "INSERT INTO main.facility_types SELECT slug, label, description "
        "FROM read_csv_auto(?, header=True)",
        [str(VOCAB_DIR / "facility_types.csv")],
    )
    conn.execute("DELETE FROM main.research_areas")
    conn.execute(
        """
        INSERT INTO main.research_areas
        SELECT slug AS area_id, label, gcmd_uri, parent_slug AS parent_id
        FROM read_csv_auto(?, header=True)
        """,
        [str(VOCAB_DIR / "research_areas.csv")],
    )
    conn.execute("DELETE FROM main.networks")
    conn.execute(
        """
        INSERT INTO main.networks
        SELECT slug AS network_id, label, level, url
        FROM read_csv_auto(?, header=True)
        """,
        [str(VOCAB_DIR / "networks.csv")],
    )


def _stage(conn: duckdb.DuckDBPyConnection, name: str, cols: str, rows: list[tuple]) -> None:
    """A TEMP table of rows from Python, with a `_seq` column in input
    order so merge_upsert can keep the first (INSERT-OR-IGNORE) or the
    last (INSERT-OR-REPLACE) of two rows with the same key."""
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {name} ({cols}, _seq INTEGER)")
    if rows:
        n = cols.count(",") + 2
        conn.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' * n)})",
                         [(*r, i) for i, r in enumerate(rows)])


def insert_records(conn: duckdb.DuckDBPyConnection, records: list[Record]) -> dict:
    """Upsert the records and their link rows. Returns merge_upsert stats
    per table. Runs inside the caller's cod_db.transaction.

    Rows are collected per table and written with one MERGE each. The
    lake checks no foreign keys, so link rows whose vocab slug is unknown
    are filtered out by a join against the vocab table and counted in
    ``unknown_slug`` (a single typo in an R-agent's output doesn't tank
    the ingest; scripts/ingest_supplement.py rejects them up front)."""
    today = datetime.now(timezone.utc).date()

    fac, locs, areas, nets, funders, events, prov = [], [], [], [], [], [], []
    for r in records:
        d = r.raw
        hq = d.get("hq") or {}
        fac.append((
            r.fid, d.get("canonical_name"), d.get("acronym"), d.get("parent_org"),
            d.get("facility_type"), d.get("country"), d.get("region"),
            hq.get("address"), hq.get("lat"), hq.get("lng"), d.get("url"),
            d.get("contact"), d.get("established"), normalise_ror(d.get("ror")),
        ))

        locations: list[dict] = d.get("locations") or []
        if not locations and d.get("hq"):
            locations = [{
                "label": d.get("canonical_name"),
                "address": d["hq"].get("address"),
                "lat": d["hq"].get("lat"),
                "lng": d["hq"].get("lng"),
                "role": "headquarters",
            }]
        for loc in locations:
            locs.append((
                location_id(r.fid, loc.get("label")), r.fid, loc.get("label"),
                loc.get("address"), loc.get("lat"), loc.get("lng"),
                loc.get("role") or "headquarters",
            ))

        areas += [(r.fid, area) for area in d.get("research_areas") or []]
        for net in d.get("networks") or []:
            nets.append((r.fid, net.lower() if isinstance(net, str) else str(net).lower(),
                         None))

        p = d.get("provenance") or {}
        for funder in d.get("funders") or []:
            fname = funder.get("name") if isinstance(funder, dict) else str(funder)
            if not fname:
                continue
            fuid = funder_id(fname)
            funders.append((fuid, fname))
            # funding_links is a view over funding_events (schema.sql); the
            # old INSERT INTO funding_links failed on it. One undated,
            # amount-less event per (funder, facility), as the migration
            # wrote for the legacy rows.
            events.append((
                funding_event_id(fuid, r.fid), fuid, r.fid,
                (funder.get("relation") if isinstance(funder, dict) else None),
                p.get("source_url"),
            ))

        prov.append((
            "facility", r.fid, p.get("source_url"),
            p.get("retrieved_at") or today.isoformat(),
            p.get("confidence", "medium"), r.agent,
        ))

    stats: dict[str, dict] = {}
    up = cod_db.merge_upsert

    # Named columns, not positional. created_at is not staged: a match
    # keeps its lake value and a new row gets the column DEFAULT (now).
    # ror is COALESCEd: most raw records carry none, and the lake holds
    # ROR ids other scripts found, which a NULL here must not erase.
    _stage(conn, "_s_facilities",
           "facility_id VARCHAR, canonical_name VARCHAR, acronym VARCHAR, "
           "parent_org VARCHAR, facility_type VARCHAR, country VARCHAR, region VARCHAR, "
           "hq_address VARCHAR, hq_lat DOUBLE, hq_lng DOUBLE, url VARCHAR, contact VARCHAR, "
           "established INTEGER, ror VARCHAR", fac)
    stats["facilities"] = up(conn, "facilities", ["facility_id"], "_s_facilities",
                             coalesce=["ror"], order_by="_seq DESC")

    def links(table: str, cols: str, rows: list[tuple], key: list[str],
              vocab: str, vocab_col: str, ref_col: str,
              replace_for: set[str] | None = None) -> None:
        """Write one link table, insert-only. ``replace_for``: facilities
        whose raw record carries this field, so the record is the whole
        truth for it — their existing rows are deleted first and a link
        the record dropped does not linger. (Before the lake, ingest
        re-created every table from schema.sql, so links always matched
        the raw data.)"""
        _stage(conn, f"_s_{table}", cols, rows)
        if replace_for:
            # Only the rows the record no longer has: an unchanged link is
            # left alone instead of being deleted and inserted again.
            same = " AND ".join(f"s.{k} = t.{k}" for k in key)
            conn.execute(f"DELETE FROM main.{table} t WHERE t.facility_id IN "
                         f"(SELECT unnest(?::VARCHAR[])) AND NOT EXISTS "
                         f"(SELECT 1 FROM _s_{table} s WHERE {same})",
                         [sorted(replace_for)])
        stats[table] = up(
            conn, table, key,
            f"SELECT s.* FROM _s_{table} s WHERE EXISTS "
            f"(SELECT 1 FROM main.{vocab} v WHERE v.{vocab_col} = s.{ref_col})",
            insert_only=True, order_by="_seq")
        stats[table]["unknown_slug"] = len(rows) - stats[table]["source_rows"]

    def carrying(*fields: str) -> set[str]:
        return {r.fid for r in records if any(f in r.raw for f in fields)}

    links("area_links", "facility_id VARCHAR, area_id VARCHAR",
          areas, ["facility_id", "area_id"], "research_areas", "area_id", "area_id",
          replace_for=carrying("research_areas"))
    # Not replaced: other loaders add memberships to ingested facilities
    # (fix_epa_region_affiliations.py), and the raw records do not know them.
    links("network_membership", "facility_id VARCHAR, network_id VARCHAR, role VARCHAR",
          nets, ["facility_id", "network_id"], "networks", "network_id", "network_id")

    _stage(conn, "_s_locations",
           "location_id VARCHAR, facility_id VARCHAR, label VARCHAR, address VARCHAR, "
           "lat DOUBLE, lng DOUBLE, role VARCHAR", locs)
    stats["locations"] = up(conn, "locations", ["location_id"], "_s_locations",
                            order_by="_seq DESC")

    _stage(conn, "_s_funders", "funder_id VARCHAR, name VARCHAR", funders)
    stats["funders"] = up(conn, "funders", ["funder_id"], "_s_funders",
                          insert_only=True, order_by="_seq")
    _stage(conn, "_s_events",
           "event_id VARCHAR, funder_id VARCHAR, facility_id VARCHAR, relation VARCHAR, "
           "source_url VARCHAR", events)
    stats["funding_events"] = up(
        conn, "funding_events", ["event_id"],
        "SELECT *, 'research-agent JSON' AS source, 'medium' AS confidence FROM _s_events",
        insert_only=True, order_by="_seq")

    # provenance has no key; replace this agent's row for each facility so
    # a re-run does not stack duplicates.
    _stage(conn, "_s_provenance",
           "record_type VARCHAR, record_id VARCHAR, source_url VARCHAR, "
           "retrieved_at DATE, confidence VARCHAR, agent VARCHAR", prov)
    conn.execute(
        "DELETE FROM main.provenance p WHERE EXISTS (SELECT 1 FROM _s_provenance s "
        "WHERE s.record_type = p.record_type AND s.record_id = p.record_id "
        "AND s.agent IS NOT DISTINCT FROM p.agent)")
    conn.execute("INSERT INTO main.provenance BY NAME "
                 "SELECT * EXCLUDE (_seq) FROM _s_provenance")
    stats["provenance"] = {"inserted": len(prov)}

    for t in ("facilities", "area_links", "network_membership", "locations",
              "funders", "events", "provenance"):
        conn.execute(f"DROP TABLE IF EXISTS _s_{t}")
    return stats


def log_run(conn: duckdb.DuckDBPyConnection, started: datetime, count: int, status: str) -> None:
    run_id = started.strftime("%Y%m%d-%H%M%S")
    conn.execute(
        "INSERT INTO main.ingest_runs VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?)",
        [run_id, started.replace(tzinfo=None), os.environ.get("GITHUB_SHA"), count, status],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-geocode", action="store_true")
    parser.add_argument("--skip-regions",  action="store_true",
                        help="Don't rebuild the regions / facility_regions "
                             "tables from public/overlays/ (saves time if "
                             "overlays haven't changed).")
    cod_db.add_argparse_catalog(parser)
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc)

    records = load_raw_records()
    if not records:
        print("[info] no raw records found — refreshing schema and vocab only")
    assign_ids(records)
    records = dedup(records)
    geocode_missing(records, args.skip_geocode)

    # Read the overlay polygons before opening the transaction, so a
    # missing shapely or a broken GeoJSON skips the regions pass (as
    # before) instead of aborting the ingest half-way.
    region_rows = None
    if not args.skip_regions:
        try:
            # Imported lazily so the ingest step doesn't require shapely
            # unless the regions pass runs.
            import populate_regions
            region_files = populate_regions.overlay_files()
            region_rows = populate_regions.load_region_rows(region_files)
        except Exception as e:
            print(f"[warn] populate_regions unavailable ({e!r}); regions not refreshed")

    msg = f"ingest: {len(records)} facilities from data/raw/R*"
    if region_rows is not None:
        msg += f"; regions from {len(region_files)} overlays"
    conn = cod_db.connect(catalog=args.catalog, data=args.lake_data)  # LakeNotFound
    try:
        with cod_db.transaction(conn, msg):
            ensure_schema(conn)
            load_vocab(conn)
            stats = insert_records(conn, records)
            if region_rows is not None:
                stats.update(populate_regions.write_regions(conn, region_rows))
            log_run(conn, started, len(records), "success")
            cod_db.assert_integrity(
                conn, INGEST_TABLES + (REGION_TABLES if region_rows is not None else []))
        snap = cod_db.latest_snapshot(conn)
    finally:
        conn.close()

    for table, st in stats.items():
        print(f"  {table:20s} {st}")
    print(f"[ok] wrote {len(records)} facilities to {cod_db.catalog_path(args.catalog)} "
          f"(snapshot {snap.get('snapshot_id')})")
    print("[next] python scripts/qa.py && python scripts/export_parquet.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
