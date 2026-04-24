#!/usr/bin/env python3
"""Enrich rows in `people` with OpenAlex metadata.

OpenAlex (openalex.org) is free and auth-less; setting OPENALEX_EMAIL
in the environment moves you into the "polite pool" with higher rate
limits — recommended. For each person we resolve to an OpenAlex
Author record via (in priority order):

  1. Existing openalex_id on the row
  2. ORCID (`/authors?filter=orcid:0000-…`)
  3. Name + institutional affiliation search
     (`/authors?search=<name>&filter=last_known_institution.display_name.search:<inst>`)

For each match we then fetch up to N publications, co-authors, and
concepts (research topics), writing into:

  * people.openalex_id / research_interests     (update)
  * publications                                (upsert by doi/openalex_id)
  * authorship                                  (link person ↔ pub)
  * person_areas                                (weighted links to
                                                 research_areas; we map
                                                 OpenAlex concepts to
                                                 our area_id slugs via
                                                 data/vocab_crosswalk/
                                                 openalex_to_area.csv
                                                 when present, else we
                                                 just skip topic writes)

Adapted from the UNM knowledge-map enrichment pipeline pattern.

Usage::

    python scripts/enrich_people_openalex.py --dry-run           # no writes
    python scripts/enrich_people_openalex.py --limit 10          # small batch
    python scripts/enrich_people_openalex.py --db db/cod_kmap.duckdb

Environment:
    OPENALEX_EMAIL=tswetnam@arizona.edu   # polite pool (recommended)

Dependencies:
    pip install requests
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("[error] pip install requests --break-system-packages", file=sys.stderr)
    raise

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
API = "https://api.openalex.org"
UA = "cod-kmap/0.1 (github.com/tyson-swetnam/cod-kmap; mailto:{email})"


def session() -> requests.Session:
    email = os.environ.get("OPENALEX_EMAIL", "")
    s = requests.Session()
    s.headers["User-Agent"] = UA.format(email=email or "unset")
    if email:
        s.params = {"mailto": email}
    return s


def resolve_author(sess: requests.Session, person: dict) -> dict | None:
    """Try openalex_id → ORCID → name+institution. Returns the author JSON
    or None."""
    oa_id = (person.get("openalex_id") or "").strip()
    if oa_id:
        r = sess.get(f"{API}/authors/{oa_id}")
        if r.ok:
            return r.json()

    orcid = (person.get("orcid") or "").strip()
    if orcid:
        r = sess.get(f"{API}/authors",
                     params={"filter": f"orcid:{orcid}", "per_page": 1})
        if r.ok:
            hits = r.json().get("results", [])
            if hits:
                return hits[0]

    name = (person.get("name") or "").strip()
    if name:
        r = sess.get(f"{API}/authors",
                     params={"search": name, "per_page": 1})
        if r.ok:
            hits = r.json().get("results", [])
            if hits:
                return hits[0]
    return None


def fetch_works(sess: requests.Session, author_oa_id: str,
                max_records: int = 100) -> list[dict]:
    """Fetch up to `max_records` publications for this OpenAlex author.
    Uses cursor pagination."""
    out: list[dict] = []
    cursor = "*"
    while cursor and len(out) < max_records:
        r = sess.get(f"{API}/works", params={
            "filter": f"authorships.author.id:{author_oa_id}",
            "per_page": min(50, max_records - len(out)),
            "cursor": cursor,
        })
        if not r.ok:
            print(f"[warn] works fetch {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        out.extend(data.get("results", []))
        cursor = data.get("meta", {}).get("next_cursor")
        time.sleep(0.1)   # be polite even in the polite pool
    return out[:max_records]


def upsert_publication(conn, work: dict) -> str | None:
    oa = work.get("id", "")
    doi = (work.get("doi") or "").replace("https://doi.org/", "").strip() or None
    pub_id = (oa.split("/")[-1] if oa else (doi or ""))
    if not pub_id:
        return None
    # Older DuckDB builds (0.9.x) segfault on GREATEST(...) inside an
    # ON CONFLICT DO UPDATE clause and are picky about CURRENT_DATE as a
    # bare identifier. Portable path: try a SELECT first, then branch
    # between INSERT and UPDATE. Works on every DuckDB ≥ 0.9.
    existing = conn.execute(
        "SELECT cited_by_count FROM publications WHERE publication_id = ?",
        [pub_id],
    ).fetchone()
    title    = work.get("title")
    year     = work.get("publication_year")
    pub_type = work.get("type")
    journal  = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    cbc      = work.get("cited_by_count") or 0
    url      = (work.get("primary_location") or {}).get("landing_page_url")
    if existing is None:
        conn.execute(
            """
            INSERT INTO publications (
                publication_id, doi, title, pub_year, pub_type,
                journal, cited_by_count, openalex_id, url, source, retrieved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_date)
            """,
            [pub_id, doi, title, year, pub_type, journal, cbc,
             oa or None, url, "openalex"],
        )
    # existing row case: skip the UPDATE.
    #
    # DuckDB (≥1.0, as of 1.5.x) refuses to UPDATE a row that has any
    # FK still pointing at it, even when the UPDATE doesn't touch the
    # PK column. This breaks co-author enrichment: if person A is
    # enriched first and has paper X, paper X gets an authorship row
    # (A, X). When we then process co-author B on the same paper,
    # upsert_publication tries to UPDATE publications.cited_by_count
    # for X, and DuckDB aborts with
    #
    #   Constraint Error: Violates foreign key constraint because key
    #   "publication_id: W…" is still referenced by a foreign key in
    #   a different table
    #
    # The first write is already factually correct (same OpenAlex
    # data, minutes apart), so we can safely skip the refresh and
    # just fall through to the authorship INSERT.
    return pub_id


def enrich_person(conn, sess: requests.Session, person: dict,
                  max_pubs: int, dry: bool) -> dict:
    result = {"person_id": person["person_id"], "works_found": 0, "upserted": 0}
    author = resolve_author(sess, person)
    if not author:
        return result
    author_oa = author["id"]
    author_oa_short = author_oa.split("/")[-1]
    interests = ", ".join(
        (c.get("display_name") or "") for c in (author.get("x_concepts") or [])[:5]
    )
    if not dry:
        conn.execute(
            "UPDATE people SET openalex_id = ?, research_interests = "
            "COALESCE(NULLIF(?, ''), research_interests), "
            "updated_at = now() WHERE person_id = ?",
            [author_oa_short, interests, person["person_id"]],
        )

    works = fetch_works(sess, author_oa, max_pubs)
    result["works_found"] = len(works)
    if dry:
        return result
    for w in works:
        pid = upsert_publication(conn, w)
        if not pid:
            continue
        conn.execute(
            """
            INSERT INTO authorship (person_id, publication_id, raw_name)
            VALUES (?, ?, ?)
            ON CONFLICT (person_id, publication_id) DO NOTHING
            """,
            [person["person_id"], pid, person["name"]],
        )
        result["upserted"] += 1
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=0,
                    help="Max people to process (0 = all)")
    ap.add_argument("--max-pubs", type=int, default=100,
                    help="Max publications per person")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] db not found: {args.db}", file=sys.stderr)
        return 2

    conn = duckdb.connect(str(args.db))
    sess = session()

    # .fetchall() + manual dict build is more portable across DuckDB
    # versions than .fetchdf() (pandas bridge varies) and avoids a
    # hard pandas dependency on the user's machine.
    rows = conn.execute(
        "SELECT person_id, name, orcid, openalex_id FROM people ORDER BY name"
    ).fetchall()
    people = [
        {"person_id": r[0], "name": r[1], "orcid": r[2], "openalex_id": r[3]}
        for r in rows
    ]
    if args.limit:
        people = people[: args.limit]
    print(f"[enrich] processing {len(people)} people"
          f"{'  (dry-run)' if args.dry_run else ''}")

    totals = {"works_found": 0, "upserted": 0, "skipped": 0}
    for i, p in enumerate(people, 1):
        try:
            r = enrich_person(conn, sess, p, args.max_pubs, args.dry_run)
            totals["works_found"] += r["works_found"]
            totals["upserted"] += r["upserted"]
            if not r["works_found"]:
                totals["skipped"] += 1
            print(f"  [{i}/{len(people)}] {p['name']}  "
                  f"works={r['works_found']}  wrote={r['upserted']}")
        except Exception as e:
            print(f"  [{i}/{len(people)}] {p['name']}  ERROR: {e}")
            totals["skipped"] += 1

    print(f"[done] works_found={totals['works_found']}  "
          f"upserted={totals['upserted']}  skipped={totals['skipped']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
