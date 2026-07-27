#!/usr/bin/env python3
"""Give facilities a ROR, then link registry members to the sites they work at.

Two passes.

**Pass 1 — facilities.ror.** `facilities` has no persistent organisation
identifier, so a researcher's OpenAlex affiliation (which always carries a
ROR) has nothing to join to. This resolves each facility against the
OpenAlex /institutions endpoint and stores the ROR.

Only research organisations are attempted. Of 3,519 catalogued facilities,
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
    python scripts/link_registry_facilities.py --export-parquet
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import openalex_auth  # noqa: E402
from enrich_people_orcid import distinctive_tokens  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
PARQUET_OUT = [ROOT / "db" / "parquet", ROOT / "public" / "parquet"]
API = "https://api.openalex.org"
SLEEP = 0.25

# Facility types that can plausibly hold a ROR. Everything else in the
# catalogue is a place, not an organisation.
RESEARCH_TYPES = {
    "federal", "network", "nonprofit", "international-federal",
    "university-marine-lab", "international-university", "state",
    "international-nonprofit", "foundation", "university", "consortium",
    "international-network", "tribal", "private",
}


def ensure_ror_column(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info('facilities')").fetchall()}
    if "ror" not in cols:
        conn.execute("ALTER TABLE facilities ADD COLUMN ror VARCHAR")
        print("[schema] added facilities.ror")


def ensure_link_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registry_facilities (
            canonical_id  VARCHAR NOT NULL,
            facility_id   VARCHAR NOT NULL,
            ror           VARCHAR,
            method        VARCHAR NOT NULL,
            confidence    VARCHAR NOT NULL,
            retrieved_at  VARCHAR,
            PRIMARY KEY (canonical_id, facility_id))""")


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
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--export-parquet", action="store_true")
    args = ap.parse_args()

    openalex_auth.require_api_key()
    sess = openalex_auth.openalex_session()
    conn = duckdb.connect(str(args.db))
    ensure_ror_column(conn)
    ensure_link_table(conn)
    today = date.today().isoformat()

    types = ",".join(f"'{t}'" for t in sorted(RESEARCH_TYPES))
    targets = conn.execute(f"""
        SELECT facility_id, canonical_name, acronym, facility_type
        FROM facilities
        WHERE facility_type IN ({types}) AND (ror IS NULL OR ror = '')
        ORDER BY canonical_name""").fetchall()
    skipped = conn.execute(f"""
        SELECT COUNT(*) FROM facilities WHERE facility_type NOT IN ({types})
    """).fetchone()[0]
    print(f"[ror] {len(targets)} research-organisation facility/ies to resolve "
          f"({skipped:,} place-type facilities skipped by design)")
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

    if not args.dry_run and found:
        conn.executemany("UPDATE facilities SET ror = ? WHERE facility_id = ?", found)

    # ── pass 2: equality join on ROR ───────────────────────────────────
    if not args.dry_run:
        conn.execute("DELETE FROM registry_facilities")
        conn.execute("""
            INSERT INTO registry_facilities
            SELECT DISTINCT r.canonical_id, f.facility_id, f.ror,
                   'ror-equality', 'high', ?
            FROM person_registry r
            JOIN facilities f ON f.ror = r.affiliation_ror
            WHERE r.affiliation_ror IS NOT NULL AND r.affiliation_ror <> ''
              AND f.ror IS NOT NULL AND f.ror <> ''""", [today])
    n = conn.execute("SELECT COUNT(*) FROM registry_facilities").fetchone()[0]
    people = conn.execute(
        "SELECT COUNT(DISTINCT canonical_id) FROM registry_facilities").fetchone()[0]
    sites = conn.execute(
        "SELECT COUNT(DISTINCT facility_id) FROM registry_facilities").fetchone()[0]
    print(f"[link] {n:,} registry↔facility link(s): "
          f"{people:,} researcher(s) across {sites} site(s)")

    if args.export_parquet and not args.dry_run:
        db_base, site_base = PARQUET_OUT
        for base in PARQUET_OUT:
            base.mkdir(parents=True, exist_ok=True)
        # facilities ships whole — every site is a map feature regardless of
        # whether any of its researchers made the core tier.
        for base in PARQUET_OUT:
            conn.execute(f"COPY facilities TO "
                         f"'{base / 'facilities.parquet'}' (FORMAT PARQUET)")
        conn.execute(f"COPY registry_facilities TO "
                     f"'{db_base / 'registry_facilities.parquet'}' (FORMAT PARQUET)")
        # The browser only holds the core tier of person_registry, so a link
        # to an archive-tier researcher would render as a reference to a row
        # that isn't there. 1,204 of 1,467 links were in that state before
        # this filter. Same rule the edge export uses in
        # scripts/rank_person_registry.py.
        conn.execute(f"""COPY (SELECT rf.* FROM registry_facilities rf
                         JOIN person_registry r USING (canonical_id)
                         WHERE r.tier = 'core')
                         TO '{site_base / 'registry_facilities.parquet'}'
                         (FORMAT PARQUET)""")
        shipped = conn.execute(
            "SELECT COUNT(*) FROM registry_facilities rf "
            "JOIN person_registry r USING (canonical_id) "
            "WHERE r.tier = 'core'").fetchone()[0]
        print(f"[parquet] db/parquet: all {n:,} link(s); "
              f"public/parquet: {shipped:,} core-tier link(s)")
        print("[note] parquet is gitignored; stage with `git add -f`")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
