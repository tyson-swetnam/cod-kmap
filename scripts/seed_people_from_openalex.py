#!/usr/bin/env python3
"""Scale up people seeding: for each facility, pull its top N authors
from OpenAlex's institution profile.

This is how we go from the 23 hand-curated key-personnel rows to
~2,000 real researchers across all 210 facilities. Non-fabricating:
every row we write is backed by an OpenAlex author record with a
citable openalex_id and source_url.

Resolution ladder for each facility (first hit wins):

  1. Manual override in data/seed/openalex_institution_overrides.csv
     (facility_id or facility_acronym -> openalex_institution_id)
  2. ROR match: if facilities.url looks like a ROR URL, use its id
  3. Homepage match: /institutions?filter=homepage_url:<url>
  4. Name search: /institutions?search=<canonical_name>
     (accept only the top hit when its display_name matches
      the facility canonical_name reasonably closely)

Once an institution is resolved we fetch the top N authors by
works_count::

  /authors?filter=last_known_institutions.id:<I...>&
            sort=works_count:desc&per_page=<N>

and upsert each one as a `people` row + a `facility_personnel` row
(role='Research Scientist', is_key_personnel=false by default).

Progress is checkpointed to data/seed/.openalex_seed_progress.json so
interrupted runs can resume. Pass --force-refresh to ignore the
checkpoint.

Environment::

    OPENALEX_API_KEY=...   # required (scripts/openalex_auth.py)

Usage::

    python scripts/seed_people_from_openalex.py                  # all facilities
    python scripts/seed_people_from_openalex.py --limit 10       # first 10
    python scripts/seed_people_from_openalex.py --top-authors 5  # fewer per facility
    python scripts/seed_people_from_openalex.py --dry-run        # resolve only
    python scripts/seed_people_from_openalex.py --force-refresh  # re-check facilities

Writes the DuckLake store of record (scripts/cod_db.py): authors are
collected while the run talks to OpenAlex and written in one snapshot at
the end. A facility is checkpointed as "done" only once that snapshot has
committed, so an interrupted or failed run re-fetches it next time.
``--export-parquet`` is a no-op now; publish with export_parquet.py.

Typical wall-clock time: ~5-8 minutes for all 210 facilities in the
polite pool.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:
    print("[error] pip install requests --break-system-packages", file=sys.stderr)
    raise

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = ROOT / "data" / "seed" / "openalex_institution_overrides.csv"
PROGRESS = ROOT / "data" / "seed" / ".openalex_seed_progress.json"
API = "https://api.openalex.org"


# ── HTTP ────────────────────────────────────────────────────────────
def session() -> requests.Session:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import openalex_auth

    openalex_auth.require_api_key()
    return openalex_auth.openalex_session()


def polite_get(sess: requests.Session, path: str, **params) -> dict:
    r = sess.get(f"{API}{path}", params=params, timeout=20)
    if r.status_code == 429:
        time.sleep(2)
        r = sess.get(f"{API}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ── Institution resolution ─────────────────────────────────────────
def load_overrides() -> dict[str, str]:
    if not OVERRIDES.exists():
        return {}
    out: dict[str, str] = {}
    with OVERRIDES.open() as fh:
        for row in csv.DictReader(fh):
            if not row or list(row.values())[0].strip().startswith("#"):
                continue
            key = (row.get("facility_acronym") or row.get("facility_id") or "").strip()
            inst = (row.get("openalex_institution_id") or "").strip()
            if key and inst:
                out[key] = inst
    return out


def normalise_url(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


def _root_domain(url: str) -> str | None:
    """Return the 'apex' domain of a URL, stripping subdomains above the
    last two labels for common TLDs. Examples::

        https://sbclter.msi.ucsb.edu/…   -> ucsb.edu
        https://pie-lter.mbl.edu/        -> mbl.edu
        https://hmsc.oregonstate.edu     -> oregonstate.edu
        https://mote.org                 -> mote.org
    """
    if not url:
        return None
    m = re.sub(r"^https?://", "", url.strip().lower())
    m = m.split("/")[0]          # drop path
    parts = m.split(".")
    if len(parts) < 2:
        return m
    return ".".join(parts[-2:])


def resolve_institution_candidates(sess: requests.Session, facility: dict,
                                   overrides: dict[str, str]) -> list[str]:
    """Return a list of OpenAlex institution ids to try, highest priority
    first. The main loop walks this list and picks the first that
    yields at least one top_author — this is how we bypass OpenAlex
    "stub" institution records (LTER sites, IOOS regional associations,
    some small sub-labs) that exist in the catalogue but have no
    indexed publications."""
    fid = facility["facility_id"]
    acr = facility.get("acronym") or ""
    name = facility["canonical_name"]
    url = facility.get("url")
    out: list[str] = []
    seen: set[str] = set()

    def add(cid: str) -> None:
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)

    # 1. manual override always wins — user took the time to set it
    for key in (acr, fid):
        if key and key in overrides:
            add(overrides[key])

    # 2. URL filter — OpenAlex indexes the facility's exact homepage_url.
    # Often a stub for sub-labs (e.g. sbclter.msi.ucsb.edu -> SBC LTER
    # stub with 0 authors), so we keep it as a candidate but also
    # record more fallbacks below.
    if url:
        try:
            j = polite_get(sess, "/institutions",
                           filter=f"homepage_url:{normalise_url(url)}",
                           per_page=1)
            hits = j.get("results", [])
            if hits:
                add(hits[0]["id"].split("/")[-1])
        except requests.HTTPError:
            pass

    # 3. Apex-domain search — sub-labs on .edu subdomains roll up to
    # the parent institution (sbclter.msi.ucsb.edu -> ucsb.edu -> UCSB).
    root = _root_domain(url or "")
    if root:
        try:
            j = polite_get(sess, "/institutions",
                           search=root, per_page=3)
            for hit in j.get("results", []):
                hu = (hit.get("homepage_url") or "").lower()
                if root in hu:
                    add(hit["id"].split("/")[-1])
        except requests.HTTPError:
            pass

    # 4. Name search with the relaxed fuzzy matcher.
    try:
        j = polite_get(sess, "/institutions", search=name, per_page=5)
        for hit in j.get("results", []):
            dn = (hit.get("display_name") or "").lower()
            if _name_matches(dn, name.lower()):
                add(hit["id"].split("/")[-1])
    except requests.HTTPError:
        pass

    return out


# Back-compat single-result wrapper.
def resolve_institution(sess: requests.Session, facility: dict,
                        overrides: dict[str, str]) -> str | None:
    cands = resolve_institution_candidates(sess, facility, overrides)
    return cands[0] if cands else None


def _name_matches(candidate: str, target: str) -> bool:
    """Relaxed token-overlap matcher. Strips a lot of noise words that
    common marine/coastal facility names carry (institute, laboratory,
    estuarine, reserve, …) and accepts 50% overlap on the remaining
    distinctive tokens. Lower than 0.5 risks cross-matching unrelated
    sites (a "Marine Laboratory" vs another "Marine Laboratory")."""
    stop = {
        "the", "of", "for", "at", "and", "on", "in", "to",
        "school", "institute", "institutes", "institution", "institutions",
        "laboratory", "laboratories", "lab", "labs",
        "center", "centers", "centre", "centres",
        "program", "programs", "programme",
        "national", "regional", "state",
        "us", "usa", "united", "states",
        "coastal", "marine", "ocean", "oceanic", "oceanography",
        "research", "science", "sciences", "scientific",
        "department", "office",
        "estuary", "estuarine", "reserve", "reserves",
        "sanctuary", "sanctuaries", "monument", "monuments",
        "partnership", "partnerships",
        "network", "networks", "consortium",
        "association", "associations", "system", "systems",
    }
    def tokenize(s: str) -> set[str]:
        return {w for w in re.findall(r"\w+", s.lower())
                if w not in stop and len(w) > 2}
    a, b = tokenize(candidate), tokenize(target)
    if not a or not b:
        return False
    inter = a & b
    if not inter:
        return False
    overlap = len(inter) / min(len(a), len(b))
    return overlap >= 0.5


# ── Top authors pull ───────────────────────────────────────────────
def top_authors(sess: requests.Session, institution_id: str,
                n: int = 10) -> list[dict]:
    """Return up to n OpenAlex Author records for an institution,
    ordered by works_count desc."""
    j = polite_get(sess, "/authors",
                   filter=f"last_known_institutions.id:{institution_id}",
                   sort="works_count:desc",
                   per_page=min(n, 50))
    return j.get("results", [])[:n]


# ── DB upsert ───────────────────────────────────────────────────────
def person_id(name: str, orcid: str = "", openalex_id: str = "") -> str:
    """ORCID or openalex_id is the strongest disambiguator; fall back to
    name alone."""
    key = f"{name.strip().lower()}|{(orcid or '').strip()}|{(openalex_id or '').strip()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    if len(parts) < 2:
        return "", full.strip()
    return " ".join(parts[:-1]), parts[-1]


def person_row(author: dict, seq: int) -> tuple:
    """Stage row for one OpenAlex author (layout of STAGE_PEOPLE)."""
    name = author.get("display_name") or ""
    oa_short = author.get("id", "").split("/")[-1]
    orcid = author.get("orcid")
    if orcid:
        orcid = orcid.replace("https://orcid.org/", "")
    pid = person_id(name, orcid or "", oa_short)
    given, family = split_name(name)
    concepts = author.get("x_concepts") or author.get("topics") or []
    interests = ", ".join(c.get("display_name", "") for c in concepts[:5]).strip(", ")
    return (seq, pid, name, family, given, orcid, oa_short, interests)


def personnel_row(person_pid: str, facility_id: str, author: dict,
                  institution_id: str, seq: int) -> tuple:
    """Stage row for one facility_personnel link (layout of STAGE_ROLES)."""
    role = "Research Scientist"
    return (seq, person_pid, facility_id, role,
            f"Top-{role.lower()} publisher at {author.get('display_name') or ''}",
            False, "openalex:top-authors",
            f"https://openalex.org/{institution_id}", "medium",
            f"works_count={author.get('works_count') or 0}; "
            f"h_index={author.get('h_index') or (author.get('summary_stats') or {}).get('h_index') or 0}")


STAGE_PEOPLE = ("_seq INTEGER, person_id VARCHAR, name VARCHAR, name_family VARCHAR, "
                "name_given VARCHAR, orcid VARCHAR, openalex_id VARCHAR, "
                "research_interests VARCHAR")
STAGE_ROLES = ("_seq INTEGER, person_id VARCHAR, facility_id VARCHAR, role VARCHAR, "
               "title VARCHAR, is_key_personnel BOOLEAN, source VARCHAR, "
               "source_url VARCHAR, confidence VARCHAR, notes VARCHAR")


def write_stage(conn, people_rows: list[tuple], role_rows: list[tuple]) -> dict:
    """Write the run's authors in one go. Call inside cod_db.transaction().

    Same outcome as the old per-author statements:
      * a person already in the lake is filled in (name parts, ORCID,
        OpenAlex id overwritten when the author record has them; research
        interests only when non-empty) — but only while they have no
        authorship rows yet, so an enriched person's record is left as
        enrich_people_openalex.py wrote it;
      * a new person is inserted with status 'active';
      * facility_personnel is upserted on (person, facility, role); a
        match refreshes only the provenance columns, never title /
        is_key_personnel.
    The last staged row per key wins, as the last statement did.
    """
    conn.execute(f"CREATE OR REPLACE TEMP TABLE _seed_people ({STAGE_PEOPLE})")
    conn.execute(f"CREATE OR REPLACE TEMP TABLE _seed_roles ({STAGE_ROLES})")
    if people_rows:
        conn.executemany("INSERT INTO _seed_people VALUES (?,?,?,?,?,?,?,?)", people_rows)
    if role_rows:
        conn.executemany("INSERT INTO _seed_roles VALUES (?,?,?,?,?,?,?,?,?,?)", role_rows)
    conn.execute("CREATE OR REPLACE TEMP TABLE _seed_people_d AS "
                 "SELECT DISTINCT ON (person_id) * FROM _seed_people "
                 "ORDER BY person_id, _seq DESC")
    # Existing people first (the INSERT below would otherwise make every
    # new person "existing"). Only rows the update actually changes.
    n_updated = cod_db.scalar(conn, """
        UPDATE people SET
            name         = COALESCE(s.name, people.name),
            name_family  = COALESCE(s.name_family, people.name_family),
            name_given   = COALESCE(s.name_given, people.name_given),
            orcid        = COALESCE(s.orcid, people.orcid),
            openalex_id  = COALESCE(s.openalex_id, people.openalex_id),
            research_interests =
                COALESCE(NULLIF(s.research_interests, ''), people.research_interests),
            updated_at = now()
        FROM _seed_people_d s
        WHERE people.person_id = s.person_id
          AND NOT EXISTS (SELECT 1 FROM authorship a WHERE a.person_id = s.person_id)
          AND (   COALESCE(s.name, people.name) IS DISTINCT FROM people.name
               OR COALESCE(s.name_family, people.name_family)
                      IS DISTINCT FROM people.name_family
               OR COALESCE(s.name_given, people.name_given)
                      IS DISTINCT FROM people.name_given
               OR COALESCE(s.orcid, people.orcid) IS DISTINCT FROM people.orcid
               OR COALESCE(s.openalex_id, people.openalex_id)
                      IS DISTINCT FROM people.openalex_id
               OR COALESCE(NULLIF(s.research_interests, ''), people.research_interests)
                      IS DISTINCT FROM people.research_interests)
    """)
    ins = cod_db.merge_upsert(
        conn, "people", ["person_id"],
        "SELECT person_id, name, name_family, name_given, orcid, openalex_id, "
        "research_interests, 'active' AS status, "
        "CAST(now() AS TIMESTAMP) AS created_at, CAST(now() AS TIMESTAMP) AS updated_at "
        "FROM _seed_people_d",
        insert_only=True)
    roles = cod_db.merge_upsert(
        conn, "facility_personnel", ["person_id", "facility_id", "role"],
        "SELECT *, current_date AS retrieved_at FROM _seed_roles",
        update_cols=["source", "source_url", "retrieved_at", "confidence", "notes"],
        order_by="_seq DESC")
    for t in ("_seed_people", "_seed_people_d", "_seed_roles"):
        conn.execute(f"DROP TABLE {t}")
    cod_db.assert_integrity(conn, ["people", "facility_personnel"])
    return {"people_inserted": ins["inserted"], "people_updated": n_updated,
            "personnel_inserted": roles["inserted"], "personnel_updated": roles["matched"]}


# ── Progress ────────────────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_progress(data: dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(data, indent=2, sort_keys=True))


# ── Main ────────────────────────────────────────────────────────────
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
    ap.add_argument("--limit", type=int, default=0,
                    help="Max facilities to process (0 = all)")
    ap.add_argument("--top-authors", type=int, default=10,
                    help="How many top-publishing authors per facility")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve institutions and report — no DB writes")
    ap.add_argument("--force-refresh", action="store_true",
                    help="Ignore the progress checkpoint and re-check every facility")
    ap.add_argument("--export-parquet", action="store_true",
                    help="deprecated no-op: run scripts/export_parquet.py to publish")
    args = ap.parse_args()

    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=catalog_arg(args),
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    sess = session()
    overrides = load_overrides()
    progress = {} if args.force_refresh else load_progress()

    facilities = cod_db.rows(
        conn, "SELECT facility_id, canonical_name, acronym, url, facility_type "
              "FROM facilities ORDER BY canonical_name, facility_id")
    facilities = [
        {"facility_id": r[0], "canonical_name": r[1], "acronym": r[2],
         "url": r[3], "facility_type": r[4]}
        for r in facilities
    ]
    if args.limit:
        facilities = facilities[: args.limit]

    print(f"[seed] {len(facilities)} facilities, top {args.top_authors} "
          f"authors each, {'dry-run' if args.dry_run else 'live'}")
    totals = {"resolved": 0, "unresolved": 0, "authors": 0, "skipped_done": 0}
    people_rows: list[tuple] = []
    role_rows: list[tuple] = []
    # "done" checkpoints wait for the commit: saving one before the authors
    # are in the lake would make an interrupted run skip that facility for
    # good. Unresolved / stub-only / error states need no write and are
    # checkpointed as they happen.
    pending_done: dict[str, dict] = {}
    seq = 0
    try:
        for i, f in enumerate(facilities, 1):
            fid = f["facility_id"]
            if fid in progress and progress[fid].get("status") == "done":
                totals["skipped_done"] += 1
                continue

            try:
                cands = resolve_institution_candidates(sess, f, overrides)
            except Exception as e:
                print(f"  [{i}/{len(facilities)}] {f['canonical_name'][:46]:<46} "
                      f"RESOLVE-ERR: {e}")
                progress[fid] = {"status": "error", "err": str(e)[:200]}
                continue

            if not cands:
                print(f"  [{i}/{len(facilities)}] {f['canonical_name'][:46]:<46} "
                      f"[no OpenAlex institution]")
                totals["unresolved"] += 1
                progress[fid] = {"status": "unresolved"}
                continue

            # Walk candidates; first one with at least one top author wins.
            # This is how LTER sites / IOOS RIs / other OpenAlex "stub"
            # institutions (catalogued but 0 authors) fall through to their
            # host university instead of silently producing no rows.
            chosen = None
            authors: list[dict] = []
            stub_skipped: list[str] = []
            for inst in cands:
                try:
                    cand_authors = top_authors(sess, inst, args.top_authors)
                except Exception as e:
                    print(f"    [{inst}] AUTHORS-ERR: {e}")
                    continue
                if cand_authors:
                    chosen = inst
                    authors = cand_authors
                    break
                stub_skipped.append(inst)

            if not chosen:
                tail = f"  (tried {', '.join(cands)}; all 0 authors)" if cands else ""
                print(f"  [{i}/{len(facilities)}] {f['canonical_name'][:46]:<46} "
                      f"[no authors at any candidate]{tail}")
                totals["unresolved"] += 1
                progress[fid] = {"status": "stub-only",
                                 "candidates": cands}
                continue

            totals["resolved"] += 1
            n = 0
            for a in authors:
                if args.dry_run:
                    continue
                try:
                    seq += 1
                    prow = person_row(a, seq)
                    rrow = personnel_row(prow[1], fid, a, chosen, seq)
                except Exception as e:
                    print(f"    author parse error for {a.get('display_name')}: {e}")
                    continue
                people_rows.append(prow)
                role_rows.append(rrow)
                n += 1
            totals["authors"] += n
            note = f"  (skipped stubs: {','.join(stub_skipped)})" if stub_skipped else ""
            print(f"  [{i}/{len(facilities)}] {f['canonical_name'][:46]:<46} "
                  f"inst={chosen}  authors={len(authors)}  wrote={n}{note}")
            pending_done[fid] = {"status": "done", "openalex_id": chosen,
                                 "stub_skipped": stub_skipped,
                                 "authors_found": len(authors),
                                 "authors_written": n}
            if i % 10 == 0 and not args.dry_run:
                save_progress(progress)
    except KeyboardInterrupt:
        print("\n[interrupted] writing the authors collected so far")

    if not args.dry_run:
        if people_rows:
            with cod_db.transaction(conn, f"seed_people_from_openalex: "
                                          f"{len(pending_done)} facilities, "
                                          f"{len(people_rows)} authors "
                                          f"(top {args.top_authors})"):
                res = write_stage(conn, people_rows, role_rows)
            print(f"[lake] {res}")
        # Committed (or nothing to write): the done states are now true.
        progress.update(pending_done)
        save_progress(progress)
    print(f"\n[done] resolved={totals['resolved']}, "
          f"unresolved={totals['unresolved']}, "
          f"skipped_done={totals['skipped_done']}, "
          f"authors_written={totals['authors']}"
          + ("  (dry-run: nothing written, checkpoint untouched)" if args.dry_run else ""))

    if args.export_parquet and not args.dry_run:
        print("[parquet] --export-parquet is a no-op now: run scripts/export_parquet.py "
              "(the only parquet writer)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
