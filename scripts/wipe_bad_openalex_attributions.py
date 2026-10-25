#!/usr/bin/env python3
"""Wipe wrong OpenAlex IDs from people who were auto-resolved by name only.

scripts/enrich_people_openalex.py used a 3-step resolver:
  1. existing openalex_id
  2. ORCID
  3. name-only OpenAlex search (top hit)

For round-2 hand-curated personnel (Reserve Managers, NEP Directors,
DFO institute leads, Latin-American institute Directors, etc.) we
seeded names without ORCID or OpenAlex ID. Step 3 fired and matched
the most-published person of that name globally, often a cardiologist
or an internal-medicine doc. The user spotted Michael Seki, Paul Dest,
David Burke, William Reay, etc. with research_interests like
'Medicine, Cardiology, Heart failure'.

This script identifies the bad rows via two heuristics — research_interests
contain ZERO marine/coastal keywords, OR (--strict) the person has no
person_areas mapping at all — then:

  * NULLs openalex_id + research_interests on the person
  * DELETEs the matching authorship rows (the bogus pub→person links)
  * DELETEs the matching person_areas rows
  * Leaves publications themselves untouched (they may have legitimate
    co-authors among other people in the dataset).

A heuristic never overrides identifier evidence. A person is exempt —
reported as kept, never wiped — when their openalex_id is verified:

  * orcid-equality: the person has an ORCID, and the OpenAlex author
    record carries that same ORCID (the latest 'orcid-resolves' verdict in
    person_validation is 'pass' for their person_registry row, which holds
    the same ORCID and the same openalex_id). An empty research_interests
    says nothing about such a match; Clark Alexander's correct,
    ORCID-matched A5052291224 was flagged only because it had none.
  * coastal-topic share: their registry row's author record clears the
    scholar roster's own gate (build_community_scholars.MIN_COASTAL_WORKS
    coastal works and MIN_COASTAL_SHARE of all works).

A (person_id, openalex_id) pair in data/seed/bad_openalex_pairs.csv is
always a suspect ('blocklisted'), whatever else holds.

Re-runs are idempotent: a fresh run finds nothing left to wipe. The wipe
is one lake snapshot (scripts/cod_db.py), rolled back if it would orphan
a row; run scripts/export_parquet.py to publish it.

Usage:
    python scripts/wipe_bad_openalex_attributions.py
    python scripts/wipe_bad_openalex_attributions.py --dry-run
    python scripts/wipe_bad_openalex_attributions.py --strict   # also wipe people with 0 person_areas rows
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
from build_community_scholars import MIN_COASTAL_SHARE, MIN_COASTAL_WORKS  # noqa: E402
from wipe_misattributed_identifiers import bad_openalex_pairs, is_blocked  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Marine / coastal / oceanographic keywords. A person's research_interests
# string is split on ', ' and lowercased; if NO token contains any of
# these substrings, the person is flagged as 'no marine signal'.
MARINE_KW = [
    'ocean', 'marine', 'coast', 'estuar', 'wetland', 'algal', 'reef',
    'plankton', 'fish', 'aquacult', 'kelp', 'mangrove', 'seagrass',
    'phytoplank', 'zooplank', 'benth', 'intertid', 'tide', 'tidal',
    'sediment', 'geolog', 'climat', 'sea ', 'salt marsh', 'crustac',
    'seabird', 'cetac', 'mammal', 'pelagic', 'demersal', 'bathym',
    'hydrolog', 'watershed', 'biogeochem', 'corals', 'ecology',
    'biolog', 'evolution', 'species', 'ecosystem', 'environment',
    'limnolog', 'wave', 'reservoir',
]


def has_marine_signal(text: str) -> bool:
    if not text:
        return False
    s = text.lower()
    return any(kw in s for kw in MARINE_KW)


def verified_openalex(conn) -> dict[str, str]:
    """person_id -> why that person's current openalex_id is verified.

    Read from the lake only (no API call): the person's person_registry row
    must hold the same openalex_id (and, for orcid-equality, the same ORCID).
    """
    if not (cod_db.table_exists(conn, "person_registry")
            and cod_db.table_exists(conn, "person_validation")):
        return {}
    out: dict[str, str] = {}
    for pid, orcid, oaid in cod_db.rows(conn, """
            WITH latest AS (
              SELECT canonical_id, verdict, subject_id_value,
                     row_number() OVER (PARTITION BY canonical_id
                                        ORDER BY retrieved_at DESC, run_id DESC) AS rn
              FROM person_validation WHERE check_id = 'orcid-resolves')
            SELECT p.person_id, p.orcid, p.openalex_id
            FROM people p
            JOIN person_registry r ON r.person_id = p.person_id
            JOIN latest v ON v.canonical_id = r.canonical_id AND v.rn = 1
            WHERE p.orcid IS NOT NULL AND p.orcid <> ''
              AND upper(r.orcid) = upper(p.orcid)
              AND upper(r.openalex_id) = upper(p.openalex_id)
              AND v.verdict = 'pass' AND upper(v.subject_id_value) = upper(p.orcid)
            ORDER BY 1"""):
        out[pid] = (f"orcid-equality: OpenAlex {oaid} carries the stored "
                    f"ORCID {orcid}")
    for pid, oaid, cw, share in cod_db.rows(conn, """
            SELECT p.person_id, p.openalex_id, r.coastal_works_count, r.coastal_share
            FROM people p JOIN person_registry r ON r.person_id = p.person_id
            WHERE upper(r.openalex_id) = upper(p.openalex_id)
              AND r.coastal_works_count >= ? AND r.coastal_share >= ?
            ORDER BY 1""", [MIN_COASTAL_WORKS, MIN_COASTAL_SHARE]):
        out.setdefault(pid, f"coastal-topic share: {cw} coastal works, "
                            f"{share:.0%} of output (gate {MIN_COASTAL_WORKS} / "
                            f"{MIN_COASTAL_SHARE:.0%})")
    return out


def find_suspects(conn, strict: bool, verified: dict[str, str] | None = None):
    """[(person_id, name, research_interests, openalex_id, why)] to wipe.
    People in `verified` are never suspects unless their pair is
    blocklisted."""
    verified = verified or {}
    blocked = bad_openalex_pairs()
    rows = cod_db.rows(conn, """
        SELECT person_id, name, research_interests, openalex_id
        FROM   people
        WHERE  openalex_id IS NOT NULL AND length(openalex_id) > 0
        ORDER  BY name, person_id
    """)
    suspects = []
    for pid, name, ri, oaid in rows:
        if is_blocked(blocked, oaid, pid):
            suspects.append((pid, name, ri, oaid, 'blocklisted'))
        elif pid in verified:
            continue
        elif not has_marine_signal(ri):
            suspects.append((pid, name, ri, oaid, 'no-marine-keywords'))
    if strict:
        # Additional candidates: anyone with openalex_id but ZERO
        # person_areas rows. The OpenAlex topic crosswalk only covers
        # marine concepts, so if a person has 0 person_areas they
        # never published anything that maps to a cod-kmap area.
        zeros = cod_db.rows(conn, """
            SELECT p.person_id, p.name, p.research_interests, p.openalex_id
            FROM   people p
            LEFT JOIN person_areas pa ON pa.person_id = p.person_id
            WHERE  p.openalex_id IS NOT NULL AND length(p.openalex_id) > 0
            GROUP  BY p.person_id, p.name, p.research_interests, p.openalex_id
            HAVING COUNT(pa.area_id) = 0
            ORDER  BY p.name, p.person_id
        """)
        already = {s[0] for s in suspects}
        for pid, name, ri, oaid in zeros:
            if pid in already or pid in verified:
                continue
            suspects.append((pid, name, ri, oaid, 'no-person-areas'))
    return suspects


def wipe(conn, suspects):
    """Run inside cod_db.transaction(): three statements, one snapshot."""
    pids = [s[0] for s in suspects]
    if not pids:
        return {'people': 0, 'authorship_deleted': 0, 'person_areas_deleted': 0}
    placeholder = ','.join(['?'] * len(pids))
    # DELETE/UPDATE return a one-row (count) result.
    n_aut = cod_db.scalar(
        conn, f"DELETE FROM authorship   WHERE person_id IN ({placeholder})", pids)
    n_pa = cod_db.scalar(
        conn, f"DELETE FROM person_areas WHERE person_id IN ({placeholder})", pids)
    cod_db.scalar(
        conn,
        # research_interests goes only when it came from OpenAlex (no
        # source, or an OpenAlex source); a curated, sourced value stays.
        f"""UPDATE people SET openalex_id = NULL,
                              research_interests = CASE
                                WHEN coalesce(research_interests_source_url, '') = ''
                                  OR research_interests_source_url ILIKE '%openalex.org%'
                                THEN NULL ELSE research_interests END,
                              research_interests_source_url = CASE
                                WHEN research_interests_source_url ILIKE '%openalex.org%'
                                THEN NULL ELSE research_interests_source_url END,
                              updated_at = now()
            WHERE person_id IN ({placeholder})""",
        pids)
    return {
        'people': len(pids),
        'authorship_deleted': n_aut,
        'person_areas_deleted': n_pa,
    }


def catalog_arg(args) -> Path | None:
    """--db is the pre-lake flag, kept as an alias of --catalog. The retired
    db/cod_kmap.duckdb file itself is not a catalog, so it is ignored."""
    if args.db is None or args.catalog is not None:
        return args.catalog
    if args.db.suffix == ".duckdb":
        print(f"[deprecated] --db {args.db} ignored (db/cod_kmap.duckdb is retired); "
              f"use --catalog", file=sys.stderr)
        return None
    return args.db


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", type=Path, default=None,
                    help="deprecated alias of --catalog (a .duckdb path is ignored)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="Also wipe people who have an openalex_id but "
                         "zero person_areas rows (likely misattributions "
                         "even when their research_interests happen to "
                         "contain a marine keyword by coincidence).")
    args = ap.parse_args()

    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=catalog_arg(args),
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    verified = verified_openalex(conn)
    suspects = find_suspects(conn, args.strict, verified)
    # Report the verified people the heuristics alone would have flagged.
    flagged = {s[0] for s in find_suspects(conn, args.strict)}
    kept = [(pid, why) for pid, why in sorted(verified.items()) if pid in flagged
            and pid not in {s[0] for s in suspects}]
    names = dict(cod_db.rows(conn, "SELECT person_id, name FROM people"))
    if kept:
        print(f"[kept] {len(kept)} flagged by the heuristics but verified:")
        for pid, why in sorted(kept, key=lambda k: names.get(k[0], "")):
            print(f"  {names.get(pid, pid):30s} {why}")
    print(f"[suspects] {len(suspects)} candidates for wipe:")
    for pid, name, ri, oaid, why in suspects:
        print(f"  [{why}] {name:30s} {oaid:13s} {(ri or '')[:60]}")

    if args.dry_run:
        print("\n[dry-run] would wipe; skipping writes.")
        conn.close()
        return 0

    if not suspects:
        print("\n[wiped] nothing to wipe")
        conn.close()
        print("[done]")
        return 0

    with cod_db.transaction(conn, f"wipe_bad_openalex_attributions: "
                                  f"{len(suspects)} people"
                                  + (" (--strict)" if args.strict else "")):
        res = wipe(conn, suspects)
        cod_db.assert_integrity(conn, ["people", "authorship", "person_areas"])
    print(f"\n[wiped] people: {res['people']}  "
          f"authorship_deleted: {res['authorship_deleted']}  "
          f"person_areas_deleted: {res['person_areas_deleted']}")

    # parquet is export_parquet.py's to write.
    print("[next] run scripts/export_parquet.py to publish")
    conn.close()
    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
