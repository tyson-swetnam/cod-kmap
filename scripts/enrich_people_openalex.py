#!/usr/bin/env python3
"""Enrich rows in `people` with OpenAlex metadata.

OpenAlex (openalex.org) requires an API key on every request; set
OPENALEX_API_KEY in the environment. (It used to be auth-less with an
OPENALEX_EMAIL "polite pool"; that no longer applies, and mailto must
not be sent alongside a key.) For each person we resolve to an OpenAlex
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

Writes the DuckLake store of record (scripts/cod_db.py): rows are staged in
TEMP tables during the network loop and land in one snapshot at the end of
the run (Ctrl-C writes what was staged so far). ``--dry-run`` attaches the
lake read-only. Publish with scripts/export_parquet.py.

Usage::

    python scripts/enrich_people_openalex.py --dry-run           # no writes
    python scripts/enrich_people_openalex.py --limit 10          # small batch
    python scripts/enrich_people_openalex.py --catalog /tmp/scratch/cat.sqlite

Environment:
    OPENALEX_API_KEY=…                    # required

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
import openalex_auth  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.openalex.org"
UA = "cod-kmap/0.1 (+https://github.com/tyson-swetnam/cod-kmap)"


def session() -> requests.Session:
    """Keyed OpenAlex session.

    OpenAlex requires an API key on every request now, so the old
    OPENALEX_EMAIL polite-pool convention no longer applies; the key
    handling lives in scripts/openalex_auth.py and is shared with the
    other five scripts that call the API.
    """
    openalex_auth.require_api_key()
    return openalex_auth.openalex_session(user_agent=UA)


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

    # Name-only resolution is DELIBERATELY DISABLED. It used to fall back
    # to /authors?search=<name>&per_page=1, which returns the OpenAlex
    # author with that name who has the most publications globally —
    # almost always a prolific medical doctor when our coastal scientist
    # shares a name with one. That created the 2026-04-26 incident where
    # >50 cod-kmap people displayed "Medicine, Internal medicine" research
    # interests in the People view because their openalex_id pointed at
    # an MD instead. See scripts/wipe_medicine_attributions.py for the
    # cleanup that fixed it.
    #
    # If a person has no openalex_id and no ORCID, they stay un-enriched.
    # Add their openalex_id by hand to data/seed/openalex_institution_overrides.csv
    # (or to data/seed/orcid_resolution_log.csv with a verified ORCID) and
    # re-run this script.
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


def _short_concept_id(raw: str) -> str | None:
    """OpenAlex returns full URLs (https://openalex.org/T10102) for ids.
    We strip the prefix so the same identifier is reusable in joins
    against the OpenAlex API and against analyst-edited crosswalk
    CSVs (which write the bare id, not the URL)."""
    if not raw:
        return None
    raw = str(raw)
    # Topics:   https://openalex.org/T10102           -> T10102
    # Concepts: https://openalex.org/C2778805511      -> C2778805511
    # Keywords: https://openalex.org/keywords/citation -> keywords/citation
    if "/" in raw:
        # take the last 1 or 2 segments; topics/concepts last 1, keywords last 2
        parts = raw.rstrip("/").split("/")
        if "keywords" in parts:
            i = parts.index("keywords")
            return "/".join(parts[i:i + 2])
        return parts[-1]
    return raw


# Writes are staged in TEMP tables while the loop talks to OpenAlex and
# land in the lake in one transaction at the end of the run (one
# snapshot). TEMP tables live outside the lake, so staging them is not a
# lake write. Lookups read the lake and the stage together, so a paper
# staged for one author is reused, not re-inserted, for a co-author.
STAGE_DDL = [
    "CREATE OR REPLACE TEMP TABLE _stage_people "
    "(person_id VARCHAR, openalex_id VARCHAR, research_interests VARCHAR)",
]
# Only the columns staged below: merge_upsert inserts every column the
# stage shares with the target, so a staged created_at (NULL) would
# override the column's DEFAULT CURRENT_TIMESTAMP. Created through
# cod_db.create_temp_like (explicit DDL), never an empty CTAS.
STAGE_LIKE = [
    ("_stage_publications",
     "SELECT publication_id, doi, title, pub_year, pub_type, journal, cited_by_count, "
     "openalex_id, url, source, retrieved_at FROM publications"),
    ("_stage_authorship",
     "SELECT person_id, publication_id, raw_name, 0 AS _seq FROM authorship"),
    ("_stage_topics",
     "SELECT publication_id, concept_id, concept_name, score, level, kind, source, "
     "0 AS _seq FROM publication_topics"),
]
_seq = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def write_stage(conn) -> dict:
    """Apply the staged writes to the lake. Call inside transaction().

    The old do-nothing-on-conflict inserts (authorship, publication_topics) become an
    insert-only MERGE where the first staged row wins, as before.
    """
    # Same expressions as the old per-person UPDATE.
    conn.execute("""
        UPDATE people SET openalex_id = s.openalex_id,
               research_interests = COALESCE(NULLIF(s.research_interests, ''),
                                             people.research_interests),
               updated_at = now()
        FROM   _stage_people s WHERE people.person_id = s.person_id
    """)
    out = {"people": cod_db.scalar(conn, "SELECT count(*) FROM _stage_people")}
    out["publications"] = cod_db.merge_upsert(
        conn, "publications", ["publication_id"], "_stage_publications",
        insert_only=True)
    out["authorship"] = cod_db.merge_upsert(
        conn, "authorship", ["person_id", "publication_id"], "_stage_authorship",
        insert_only=True, order_by="_seq")
    out["publication_topics"] = cod_db.merge_upsert(
        conn, "publication_topics", ["publication_id", "concept_id"],
        "_stage_topics", insert_only=True, order_by="_seq")
    cod_db.assert_integrity(conn, ["people", "publications", "authorship",
                                   "publication_topics"])
    return out


def upsert_publication_topics(conn, pub_id: str, work: dict) -> int:
    """Stage OpenAlex concepts + topics + keywords for a publication.
    Idempotent: staged rows go in insert-only, so re-runs are cheap.
    Returns the number of rows staged."""
    rows: list[tuple] = []
    for c in (work.get("concepts") or []):
        cid = _short_concept_id(c.get("id"))
        name = c.get("display_name")
        if not (cid and name):
            continue
        rows.append((pub_id, cid, name, c.get("score"), c.get("level"),
                     "concept", "openalex"))
    for t in (work.get("topics") or []):
        tid = _short_concept_id(t.get("id"))
        name = t.get("display_name")
        if not (tid and name):
            continue
        rows.append((pub_id, tid, name, t.get("score"), None,
                     "topic", "openalex"))
    for k in (work.get("keywords") or []):
        kid = _short_concept_id(k.get("id"))
        name = k.get("display_name")
        if not (kid and name):
            continue
        rows.append((pub_id, kid, name, k.get("score"), None,
                     "keyword", "openalex"))
    if not rows:
        return 0
    written = 0
    for r in rows:
        try:
            conn.execute(
                """
                INSERT INTO _stage_topics (
                    publication_id, concept_id, concept_name,
                    score, level, kind, source, _seq
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [*r, _next_seq()],
            )
            written += 1
        except Exception as e:
            # A bad value (e.g. a score that won't cast). Log + skip so
            # one bad row doesn't kill the rest of the publication.
            print(f"[topics] skip {pub_id}/{r[1]}: {e}")
    return written


def upsert_publication(conn, work: dict) -> str | None:
    oa = work.get("id", "")
    doi = (work.get("doi") or "").replace("https://doi.org/", "").strip() or None
    pub_id = (oa.split("/")[-1] if oa else (doi or ""))
    if not pub_id:
        return None
    # Look up an existing row by *either* publication_id OR doi. OpenAlex
    # regularly assigns two distinct Work ids to the same DOI (e.g. a
    # preprint and its peer-reviewed version, or two database snapshots
    # of the same paper). We use the first row we find and reuse its
    # publication_id for the authorship link so the second insert
    # doesn't trip publications.doi UNIQUE and crash the whole author's
    # enrichment run. Rows staged earlier in this run count as existing.
    # The row whose publication_id matches is preferred over a DOI hit.
    known = ("(SELECT publication_id, doi, cited_by_count FROM publications "
             "UNION ALL SELECT publication_id, doi, cited_by_count "
             "FROM _stage_publications)")
    if doi:
        hit = cod_db.rows(
            conn,
            f"SELECT publication_id, cited_by_count FROM {known} "
            "WHERE publication_id = ? OR doi = ? "
            "ORDER BY publication_id = ? DESC LIMIT 1",
            [pub_id, doi, pub_id],
        )
    else:
        hit = cod_db.rows(
            conn,
            f"SELECT publication_id, cited_by_count FROM {known} "
            "WHERE publication_id = ? LIMIT 1",
            [pub_id],
        )
    hit = hit[0] if hit else None
    if hit and hit[0] != pub_id:
        # Existing row found via DOI; reuse its id for authorship.
        return hit[0]
    existing = (hit[1],) if hit else None
    title    = work.get("title")
    year     = work.get("publication_year")
    pub_type = work.get("type")
    journal  = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    cbc      = work.get("cited_by_count") or 0
    url      = (work.get("primary_location") or {}).get("landing_page_url")
    if existing is None:
        conn.execute(
            """
            INSERT INTO _stage_publications (
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
    # This was forced by the old native DuckDB file, which refused to
    # UPDATE a row any FK pointed at. The lake has no such block, but
    # the first write is already factually correct (same OpenAlex
    # data, minutes apart), so the refresh is still skipped and we
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
            "INSERT INTO _stage_people VALUES (?, ?, ?)",
            [person["person_id"], author_oa_short, interests],
        )

    works = fetch_works(sess, author_oa, max_pubs)
    result["works_found"] = len(works)
    result["topics_written"] = 0
    if dry:
        return result
    for w in works:
        pid = upsert_publication(conn, w)
        if not pid:
            continue
        conn.execute(
            """
            INSERT INTO _stage_authorship (person_id, publication_id, raw_name, _seq)
            VALUES (?, ?, ?, ?)
            """,
            [person["person_id"], pid, person["name"], _next_seq()],
        )
        # Write OpenAlex topics/concepts/keywords for this work. We do
        # this *every* time we see the publication (not just on first
        # insert) because OpenAlex topic scores update over time and
        # we'd rather refresh than miss the new ones; the insert-only
        # merge keeps re-runs cheap when nothing has changed.
        result["topics_written"] += upsert_publication_topics(conn, pid, w)
        result["upserted"] += 1
    return result


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
                    help="Max people to process (0 = all)")
    ap.add_argument("--max-pubs", type=int, default=100,
                    help="Max publications per person")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=catalog_arg(args),
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    sess = session()
    if not args.dry_run:
        for ddl in STAGE_DDL:
            conn.execute(ddl)
        for name, like in STAGE_LIKE:
            cod_db.create_temp_like(conn, name, like)

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

    totals = {"works_found": 0, "upserted": 0, "topics": 0, "skipped": 0}
    try:
        for i, p in enumerate(people, 1):
            try:
                r = enrich_person(conn, sess, p, args.max_pubs, args.dry_run)
                totals["works_found"] += r["works_found"]
                totals["upserted"] += r["upserted"]
                totals["topics"] += r.get("topics_written", 0)
                if not r["works_found"]:
                    totals["skipped"] += 1
                print(f"  [{i}/{len(people)}] {p['name']}  "
                      f"works={r['works_found']}  wrote={r['upserted']}  "
                      f"topics+={r.get('topics_written', 0)}")
            except Exception as e:
                print(f"  [{i}/{len(people)}] {p['name']}  ERROR: {e}")
                totals["skipped"] += 1
    except KeyboardInterrupt:
        print("\n[interrupted] writing what was staged so far")

    print(f"[done] works_found={totals['works_found']}  "
          f"upserted={totals['upserted']}  topics={totals['topics']}  "
          f"skipped={totals['skipped']}")
    if not args.dry_run:
        with cod_db.transaction(
                conn, f"enrich_people_openalex: {len(people)} people, "
                      f"max-pubs {args.max_pubs}"):
            out = write_stage(conn)
        print(f"[lake] people={out['people']}  "
              + "  ".join(f"{t}={out[t]}" for t in
                          ("publications", "authorship", "publication_topics"))
              + "  (run export_parquet.py to publish)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
