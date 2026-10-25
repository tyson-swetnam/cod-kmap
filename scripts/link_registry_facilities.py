#!/usr/bin/env python3
"""Give facilities a ROR, then link registry members to the sites they work at.

Two passes.

**Pass 1 — facilities.ror.** `facilities` has no persistent organisation
identifier, so a researcher's OpenAlex affiliation (which always carries a
ROR) has nothing to join to. This resolves each facility against the
OpenAlex /institutions endpoint and stores the ROR.

Only research organisations are attempted. Of 3,532 catalogued facilities,
3,309 are protected areas — state parks, wildlife refuges, private
preserves. A national estuarine reserve or a state park is not a research
organisation and will never hold a ROR; asking OpenAlex for one invites
exactly the false match this repo has cleaned up three times. Those rows
are skipped and their ror stays null by design, not by failure.

A candidate is accepted only when the OpenAlex institution name shares a
distinctive token with the facility name — the same generic-token-stripped
test used by the ORCID matcher, for the same reason.

**Pass 2 — registry_facilities.** With ROR on both sides the link is an
equality join: a registry member whose affiliation_ror matches a
facility's ror works at that site. No name comparison is involved.

Usage::

    python scripts/link_registry_facilities.py --dry-run
    python scripts/link_registry_facilities.py
    python scripts/link_registry_facilities.py --link-only   # pass 2 only, offline

Both passes write the lake in one snapshot, after every OpenAlex call has
returned; scripts/export_parquet.py publishes facilities and registry_facilities
(the core-tier slice of the latter to public/parquet). --export-parquet is
accepted for old callers and does nothing.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
import openalex_auth  # noqa: E402
from enrich_people_orcid import distinctive_tokens  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.openalex.org"
SLEEP = 0.25

# Facility types that can plausibly hold a ROR. Everything else in the
# catalogue is a place, not an organisation.
#
# 'observatory' was missing from the first version of this set, which
# silently excluded Ocean Networks Canada — a research organisation that
# does hold a ROR — from resolution entirely. The omission was invisible in
# the run output because the skip count (3,310) reads as if it were just
# the protected areas (3,309). Any type added to the catalogue that names an
# organisation rather than a place belongs here.
RESEARCH_TYPES = {
    "federal", "network", "nonprofit", "international-federal",
    "university-marine-lab", "international-university", "state",
    "international-nonprofit", "foundation", "observatory", "university",
    "consortium", "international-network", "tribal", "private",
}


def has_ror_column(conn) -> bool:
    return "ror" in {r[1] for r in conn.execute(
        "PRAGMA table_info('facilities')").fetchall()}


def ensure_schema_guard(conn) -> None:
    """facilities.ror and registry_facilities are both in schema/schema.sql,
    so a lake rebuilt from it has them. A lake older than that gets them
    here, column-only from the schema.sql spec (DuckLake rejects the
    PRIMARY KEY, which cod_db.assert_integrity checks instead). The table
    used to be created by this script with its own DDL; schema.sql owns it
    now. Call inside a transaction."""
    if not has_ror_column(conn):
        conn.execute("ALTER TABLE facilities ADD COLUMN ror VARCHAR")
        print("[schema] added facilities.ror")
    if not cod_db.table_exists(conn, "registry_facilities"):
        conn.execute(cod_db.column_only_ddl("registry_facilities"))
        print("[schema] created registry_facilities from schema/schema.sql")


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


def short(v) -> str | None:
    if not v:
        return None
    return str(v).rstrip("/").rsplit("/", 1)[-1] or None


def resolve_ror(sess, name: str, acronym: str | None) -> tuple[str | None, str]:
    want = distinctive_tokens(name)
    if not want:
        return None, "name-too-generic"
    r = sess.get(f"{API}/institutions",
                 params={"search": name, "per_page": 5,
                         "select": "id,display_name,ror,works_count,"
                                   "display_name_acronyms"}, timeout=40)
    if not r.ok:
        return None, f"http-{r.status_code}"
    for inst in r.json().get("results", []):
        got = distinctive_tokens(inst.get("display_name") or "")
        if got & want:
            return short(inst.get("ror")), "name-token-match"
        if acronym and acronym.upper() in [
                a.upper() for a in (inst.get("display_name_acronyms") or [])]:
            return short(inst.get("ror")), "acronym-match"
    return None, "no-match"


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--export-parquet", action="store_true",
                    help="deprecated no-op: run scripts/export_parquet.py")
    ap.add_argument("--link-only", action="store_true",
                    help="skip pass 1 and run only the ROR equality join — no "
                         "OpenAlex calls and no API key needed")
    args = ap.parse_args()

    sess = None
    if not args.link_only:
        openalex_auth.require_api_key()
        sess = openalex_auth.openalex_session()
    # Pass 1 talks to OpenAlex for minutes; nothing is written until both
    # passes are computed, so the lake write is one short transaction (one
    # snapshot) at the end. A dry run opens the lake read-only.
    try:
        conn = cod_db.connect(read_only=args.dry_run,
                              catalog=args.catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    if args.dry_run and not (has_ror_column(conn)
                             and cod_db.table_exists(conn, "registry_facilities")):
        print("[error] lake lacks facilities.ror or registry_facilities; run "
              "once without --dry-run to add them", file=sys.stderr)
        return 2
    today = date.today().isoformat()

    types = ",".join(f"'{t}'" for t in sorted(RESEARCH_TYPES))
    targets = [] if args.link_only else conn.execute(f"""
        SELECT facility_id, canonical_name, acronym, facility_type
        FROM facilities
        WHERE facility_type IN ({types}) AND (ror IS NULL OR ror = '')
        ORDER BY canonical_name""").fetchall()
    # Report the skipped set BY TYPE, not as a single number. A bare count
    # reads as "the protected areas" and hides a research organisation that
    # fell outside RESEARCH_TYPES by omission — which is exactly how Ocean
    # Networks Canada went unresolved without anyone noticing.
    skipped_by_type = conn.execute(f"""
        SELECT facility_type, COUNT(*) FROM facilities
        WHERE facility_type NOT IN ({types})
        GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    skipped = sum(n for _, n in skipped_by_type)
    non_place = [(t, n) for t, n in skipped_by_type
                 if not t.startswith("protected-area")]
    if args.link_only:
        print("[ror] pass 1 skipped (--link-only); facilities.ror is taken as given")
    else:
        print(f"[ror] {len(targets)} research-organisation facility/ies to resolve "
              f"({skipped:,} skipped by design)")
        if non_place:
            print(f"[ror] NOTE: skipped types that are not protected areas: "
                  f"{non_place} — if any of these name an organisation rather "
                  f"than a place, add them to RESEARCH_TYPES")
    if args.limit:
        targets = targets[:args.limit]

    found, decisions = [], {}
    for i, (fid, name, acro, ftype) in enumerate(targets, 1):
        ror, why = resolve_ror(sess, name, acro)
        decisions[why] = decisions.get(why, 0) + 1
        if ror:
            found.append((ror, fid))
        time.sleep(SLEEP)
        if i % 25 == 0:
            print(f"  [ror] {i}/{len(targets)}  {decisions}")
    if targets:
        print(f"[ror] {decisions}")
        print(f"[ror] resolved {len(found)} facility ROR(s)")

    # A ROR identifies ONE organisation, so two facilities claiming the same
    # one means at least one is wrong. "Monterey Bay Aquarium" matched
    # MBARI's ROR (02nb3aq72) on the shared "Monterey Bay" tokens — they are
    # separate institutions, and accepting both would have attached 90
    # MBARI researchers to the public aquarium. Keep no claimant rather than
    # guess which is right; the collision is reported for curation.
    already = {r[0]: r[1] for r in conn.execute(
        "SELECT ror, canonical_name FROM facilities "
        "WHERE ror IS NOT NULL AND ror <> ''").fetchall()}
    claims: dict[str, list[str]] = {}
    for ror, fid in found:
        claims.setdefault(ror, []).append(fid)
    contested = {r for r, fids in claims.items()
                 if len(fids) > 1 or (r in already and fids)}
    if contested:
        for r in sorted(contested):
            names = conn.execute(
                "SELECT canonical_name FROM facilities WHERE facility_id IN "
                f"({','.join('?' * len(claims[r]))})", claims[r]).fetchall()
            other = f" (already on '{already[r]}')" if r in already else ""
            print(f"[ror] CONTESTED {r}: {[n[0] for n in names]}{other} "
                  f"— left unset, needs curation")
        found = [(r, fid) for r, fid in found if r not in contested]
        print(f"[ror] {len(found)} uncontested ROR(s) will be written")

    # ── pass 2: equality join on ROR ───────────────────────────────────
    # Both passes land in one snapshot: the facility RORs (staged, then one
    # set-based UPDATE) and the rebuilt link table. This script is the only
    # writer of registry_facilities, so the table is rebuilt whole.
    if not args.dry_run:
        with cod_db.transaction(conn, f"link_registry_facilities: {len(found)} "
                                      f"facility ROR(s), ror-equality links"):
            ensure_schema_guard(conn)
            if found:
                conn.execute("CREATE OR REPLACE TEMP TABLE _ror_stage "
                             "(ror VARCHAR, facility_id VARCHAR)")
                conn.executemany("INSERT INTO _ror_stage VALUES (?, ?)", found)
                conn.execute("UPDATE facilities SET ror = s.ror FROM _ror_stage s "
                             "WHERE facilities.facility_id = s.facility_id")
            conn.execute("DELETE FROM registry_facilities")
            conn.execute("""
                INSERT INTO registry_facilities
                       (canonical_id, facility_id, ror, method, confidence, retrieved_at)
                SELECT DISTINCT r.canonical_id, f.facility_id, f.ror,
                       'ror-equality', 'high', CAST(? AS VARCHAR)
                FROM person_registry r
                JOIN facilities f ON f.ror = r.affiliation_ror
                WHERE r.affiliation_ror IS NOT NULL AND r.affiliation_ror <> ''
                  AND f.ror IS NOT NULL AND f.ror <> ''""", [today])
            assert_written(conn, ["facilities", "registry_facilities"])
    n = cod_db.scalar(conn, "SELECT COUNT(*) FROM registry_facilities")
    people = cod_db.scalar(
        conn, "SELECT COUNT(DISTINCT canonical_id) FROM registry_facilities")
    sites = cod_db.scalar(
        conn, "SELECT COUNT(DISTINCT facility_id) FROM registry_facilities")
    print(f"[link] {n:,} registry↔facility link(s): "
          f"{people:,} researcher(s) across {sites} site(s)")

    # The browser only holds the core tier of person_registry, so a link to
    # an archive-tier researcher would render as a reference to a row that
    # isn't there (1,204 of 1,467 links were in that state before the
    # filter). export_parquet.py applies that filter (EXPORT_WHERE) when it
    # publishes; this only reports what it will ship. facilities ships whole.
    shipped = cod_db.scalar(
        conn, "SELECT COUNT(*) FROM registry_facilities rf "
              "JOIN person_registry r USING (canonical_id) WHERE r.tier = 'core'")
    print(f"[link] lake: all {n:,} link(s); export_parquet.py will ship "
          f"{shipped:,} core-tier link(s) to public/parquet")
    if args.export_parquet:
        print("[note] --export-parquet is a no-op now; run "
              "scripts/export_parquet.py to publish the lake")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
