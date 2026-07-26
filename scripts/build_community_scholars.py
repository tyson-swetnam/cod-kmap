#!/usr/bin/env python3
"""Build the coastal-ocean-science community scholar roster.

Two ways in, one table:

  --seed        load the hand-curated roster from
                data/seed/community_scholars_seed.json. No network needed.
                Metric columns stay NULL and source='websearch-curated'.
                This is what ships so the Scholars tab is populated
                before anyone has run a harvest.

  --harvest     measure the field against OpenAlex and rewrite the cohorts
                from data. NEEDS OUTBOUND ACCESS to api.openalex.org.
                Curated rows are reconciled in, not discarded: a curated
                scholar who is matched by ORCID or Google-Scholar id keeps
                their curation note and gains measured metrics.

Harvest stages (each checkpoints, so --resume is cheap):

  A  candidates  per topic in data/datasets/coastal_topics.csv, group
                 /works by authorships.author.id to get the authors who
                 publish most in that topic. Union across topics.
  B  hydrate     batch /authors?filter=ids.openalex:A|A|… 50 at a time for
                 names, ORCID, summary_stats, last known institution.
  C  metrics     for the shortlist only, count each author's works inside
                 the topic set (all-time and last 5 years) and find their
                 first publication year.
  D  select      local, deterministic cohort assignment (see COHORTS).
  E  write       upsert into community_scholars + refresh parquet.

Identity rules, learned the hard way (see
scripts/wipe_bad_openalex_attributions.py): candidates are OpenAlex author
ids from the start, so no name-matching is ever performed. A scholar is
linked to an existing `people` row only on ORCID or openalex_id equality.
A coastal-share gate then drops authors whose coastal work is incidental,
which is what previously let a cardiologist onto a marine-lab page.

Usage::

    python scripts/build_community_scholars.py --seed
    OPENALEX_EMAIL=you@example.org python scripts/build_community_scholars.py --harvest
    python scripts/build_community_scholars.py --harvest --stage A --resume
    python scripts/build_community_scholars.py --harvest --dry-run
    python scripts/build_community_scholars.py --resolve-topics   # print T##### ids

Idempotent: re-running --seed or a completed --harvest yields the same table.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
SEED_JSON = ROOT / "data" / "seed" / "community_scholars_seed.json"
TOPICS_CSV = ROOT / "data" / "datasets" / "coastal_topics.csv"
CACHE = ROOT / "data" / "raw" / "community_scholars"   # gitignored
PARQUET_OUT = [ROOT / "db" / "parquet", ROOT / "public" / "parquet"]

OPENALEX = "https://api.openalex.org"
SENTINEL = "RESOLVE"
TOPIC_ID_RE = re.compile(r"^T\d+$")
AUTHOR_ID_RE = re.compile(r"^A\d+$")

# Cohort sizes and the rules that fill them.
COHORTS = {
    "preeminent": 100,    # rank by h_index desc, then cited_by_count
    "most_active": 100,   # rank by coastal_recent_works desc
    "rising": 50,         # first_pub_year within RISING_WINDOW, by 2yr citedness
}
RISING_WINDOW = 10          # years since first publication
RISING_MIN_WORKS = 5
# Eligibility gate. MIN_COASTAL_WORKS keeps one-off coastal papers out;
# MIN_COASTAL_SHARE is what actually stops a prolific author in an
# unrelated field from ranking on total h-index alone.
MIN_COASTAL_WORKS = 10
MIN_COASTAL_SHARE = 0.15
TOP_AUTHORS_PER_TOPIC = 150
SHORTLIST_SIZE = 400
BATCH = 50
SLEEP = 0.1

COLS = [
    "scholar_id", "person_id", "name", "orcid", "openalex_id", "google_scholar_id",
    "affiliation", "affiliation_country", "affiliation_ror", "homepage_url",
    "works_count", "cited_by_count", "h_index", "i10_index", "two_yr_mean_citedness",
    "coastal_works_count", "coastal_recent_works", "first_pub_year",
    "is_preeminent", "is_most_active", "is_rising",
    "rank_preeminent", "rank_most_active", "rank_rising",
    "top_topics", "rationale", "source", "source_url", "confidence", "retrieved_at",
]


# ── HTTP ──────────────────────────────────────────────────────────────

def make_session():
    try:
        import requests
    except ImportError:
        print("[error] harvest needs requests: pip install requests", file=sys.stderr)
        raise SystemExit(2)
    s = requests.Session()
    email = os.environ.get("OPENALEX_EMAIL", "")
    if email:
        s.headers["User-Agent"] = f"cod-kmap/0.1 (mailto:{email})"
        print(f"[api] polite pool as {email}")
    else:
        s.headers["User-Agent"] = "cod-kmap/0.1 (+https://github.com/tyson-swetnam/cod-kmap)"
        print("[warn] set OPENALEX_EMAIL to use OpenAlex's polite pool (faster, kinder)")
    return s


def get(session, path: str, params: dict, tries: int = 4) -> dict | None:
    """GET with backoff on the transient statuses. Returns None once the
    retries are spent so the caller can skip one author rather than lose
    the whole run."""
    url = f"{OPENALEX}/{path.lstrip('/')}"
    if os.environ.get("OPENALEX_EMAIL"):
        params = {**params, "mailto": os.environ["OPENALEX_EMAIL"]}
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 200:
                time.sleep(SLEEP)
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                print(f"[retry] {r.status_code} on {path} — sleeping {wait}s")
                time.sleep(wait)
                continue
            print(f"[warn] {r.status_code} on {path} {params}", file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001 — network flake, keep going
            wait = 2 ** attempt
            print(f"[retry] {type(e).__name__} on {path} — sleeping {wait}s")
            time.sleep(wait)
    return None


def short_id(url_or_id: str | None) -> str | None:
    """'https://openalex.org/A123' -> 'A123'."""
    if not url_or_id:
        return None
    return url_or_id.rstrip("/").rsplit("/", 1)[-1]


def clean_orcid(v: str | None) -> str | None:
    if not v:
        return None
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", v)
    return m.group(1) if m else None


# ── topics ────────────────────────────────────────────────────────────

def read_topics() -> list[dict]:
    lines = [ln for ln in TOPICS_CSV.read_text().splitlines()
             if not ln.lstrip().startswith("#")]
    return [r for r in csv.DictReader(lines) if (r.get("label") or "").strip()]


def resolve_topics(session, rows: list[dict]) -> list[dict]:
    """Fill in any RESOLVE sentinel by searching the topics endpoint."""
    for row in rows:
        tid = (row.get("openalex_topic_id") or "").strip()
        if TOPIC_ID_RE.match(tid):
            continue
        label = row["label"].strip()
        data = get(session, "topics", {"search": label, "per-page": 1})
        results = (data or {}).get("results") or []
        if results:
            row["openalex_topic_id"] = short_id(results[0].get("id"))
            row["_resolved_label"] = results[0].get("display_name")
            print(f"[topic] {label!r} -> {row['openalex_topic_id']} "
                  f"({row['_resolved_label']})")
        else:
            print(f"[warn] no OpenAlex topic matched {label!r}")
    return rows


def topic_ids(rows: list[dict]) -> list[str]:
    return [r["openalex_topic_id"] for r in rows
            if TOPIC_ID_RE.match((r.get("openalex_topic_id") or "").strip())]


# ── stages ────────────────────────────────────────────────────────────

def stage_a(session, topics: list[dict], resume: bool) -> list[str]:
    out = CACHE / "candidates.json"
    if resume and out.exists():
        ids = json.loads(out.read_text())
        print(f"[A] resumed {len(ids)} candidates from cache")
        return ids
    ids: set[str] = set()
    for tid in topic_ids(topics):
        data = get(session, "works", {
            "filter": f"primary_topic.id:{tid},from_publication_date:1990-01-01",
            "group_by": "authorships.author.id",
            "per-page": TOP_AUTHORS_PER_TOPIC,
        })
        groups = (data or {}).get("group_by") or []
        found = 0
        for g in groups[:TOP_AUTHORS_PER_TOPIC]:
            aid = short_id(g.get("key"))
            if aid and AUTHOR_ID_RE.match(aid):
                ids.add(aid)
                found += 1
        print(f"[A] {tid}: +{found} authors (running total {len(ids)})")
    CACHE.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sorted(ids)))
    print(f"[A] {len(ids)} unique candidate authors -> {out.relative_to(ROOT)}")
    return sorted(ids)


def stage_b(session, candidates: list[str], resume: bool) -> dict[str, dict]:
    out = CACHE / "authors.ndjson"
    have: dict[str, dict] = {}
    if resume and out.exists():
        for line in out.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                have[rec["openalex_id"]] = rec
        print(f"[B] resumed {len(have)} hydrated authors")
    todo = [a for a in candidates if a not in have]
    CACHE.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            data = get(session, "authors", {
                "filter": "ids.openalex:" + "|".join(chunk),
                "per-page": BATCH,
            })
            for a in (data or {}).get("results") or []:
                aid = short_id(a.get("id"))
                if not aid:
                    continue
                inst = (a.get("last_known_institutions") or [{}])[0] or {}
                stats = a.get("summary_stats") or {}
                rec = {
                    "openalex_id": aid,
                    "name": a.get("display_name"),
                    "orcid": clean_orcid(a.get("orcid")),
                    "google_scholar_id": ((a.get("ids") or {}).get("scholar") or None),
                    "affiliation": inst.get("display_name"),
                    "affiliation_country": inst.get("country_code"),
                    "affiliation_ror": inst.get("ror"),
                    "works_count": a.get("works_count"),
                    "cited_by_count": a.get("cited_by_count"),
                    "h_index": stats.get("h_index"),
                    "i10_index": stats.get("i10_index"),
                    "two_yr_mean_citedness": stats.get("2yr_mean_citedness"),
                    "top_topics": "; ".join(
                        t.get("display_name") for t in (a.get("topics") or [])[:5]
                        if t.get("display_name")) or None,
                }
                have[aid] = rec
                fh.write(json.dumps(rec) + "\n")
            print(f"[B] hydrated {min(i + BATCH, len(todo))}/{len(todo)}")
    print(f"[B] {len(have)} authors hydrated")
    return have


def shortlist(authors: dict[str, dict]) -> list[str]:
    """Only authors who could plausibly reach a cohort get the 3 extra
    calls of stage C — that is what keeps the harvest to ~1k requests."""
    ranked = sorted(
        authors.values(),
        key=lambda a: ((a.get("h_index") or 0), (a.get("cited_by_count") or 0)),
        reverse=True)
    by_h = [a["openalex_id"] for a in ranked[:SHORTLIST_SIZE]]
    # Rising stars can have a low h-index by definition, so keep the most
    # recently-impactful authors too.
    by_recent = [a["openalex_id"] for a in sorted(
        authors.values(),
        key=lambda a: (a.get("two_yr_mean_citedness") or 0.0),
        reverse=True)[:SHORTLIST_SIZE // 2]]
    return list(dict.fromkeys(by_h + by_recent))


def stage_c(session, ids: list[str], topics: list[str], resume: bool) -> dict[str, dict]:
    out = CACHE / "metrics.ndjson"
    have: dict[str, dict] = {}
    if resume and out.exists():
        for line in out.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                have[rec["openalex_id"]] = rec
        print(f"[C] resumed {len(have)} metric rows")
    topic_filter = "|".join(topics)
    cutoff = f"{date.today().year - 5}-01-01"
    todo = [a for a in ids if a not in have]
    CACHE.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        for n, aid in enumerate(todo, 1):
            all_time = get(session, "works", {
                "filter": f"author.id:{aid},primary_topic.id:{topic_filter}",
                "per-page": 1})
            recent = get(session, "works", {
                "filter": (f"author.id:{aid},primary_topic.id:{topic_filter},"
                           f"from_publication_date:{cutoff}"),
                "per-page": 1})
            first = get(session, "works", {
                "filter": f"author.id:{aid}",
                "sort": "publication_date:asc", "per-page": 1})
            first_year = None
            fr = (first or {}).get("results") or []
            if fr:
                first_year = fr[0].get("publication_year")
            rec = {
                "openalex_id": aid,
                "coastal_works_count": ((all_time or {}).get("meta") or {}).get("count"),
                "coastal_recent_works": ((recent or {}).get("meta") or {}).get("count"),
                "first_pub_year": first_year,
            }
            have[aid] = rec
            fh.write(json.dumps(rec) + "\n")
            if n % 25 == 0 or n == len(todo):
                print(f"[C] {n}/{len(todo)} authors measured")
    print(f"[C] {len(have)} authors with coastal metrics")
    return have


def stage_d(authors: dict[str, dict], metrics: dict[str, dict]) -> list[dict]:
    """Deterministic cohort assignment. Pure local computation — no calls,
    so the thresholds can be re-tuned without re-harvesting."""
    pool = []
    for aid, m in metrics.items():
        a = authors.get(aid)
        if not a:
            continue
        cw = m.get("coastal_works_count") or 0
        total = a.get("works_count") or 0
        if cw < MIN_COASTAL_WORKS:
            continue
        if total and (cw / total) < MIN_COASTAL_SHARE:
            continue
        pool.append({**a, **m})
    print(f"[D] {len(pool)}/{len(metrics)} authors pass the coastal-share gate "
          f"(>={MIN_COASTAL_WORKS} works and >={MIN_COASTAL_SHARE:.0%} of output)")

    for rec in pool:
        rec.update(is_preeminent=False, is_most_active=False, is_rising=False,
                   rank_preeminent=None, rank_most_active=None, rank_rising=None)

    pre = sorted(pool, key=lambda r: ((r.get("h_index") or 0),
                                      (r.get("cited_by_count") or 0)), reverse=True)
    for i, rec in enumerate(pre[:COHORTS["preeminent"]], 1):
        rec["is_preeminent"], rec["rank_preeminent"] = True, i
        rec["rationale"] = (f"pre-eminent #{i}: h-index {rec.get('h_index')}, "
                            f"{rec.get('cited_by_count') or 0:,} citations, "
                            f"{rec.get('coastal_works_count')} coastal works")

    act = sorted(pool, key=lambda r: ((r.get("coastal_recent_works") or 0),
                                      (r.get("coastal_works_count") or 0)), reverse=True)
    for i, rec in enumerate(act[:COHORTS["most_active"]], 1):
        rec["is_most_active"], rec["rank_most_active"] = True, i
        note = (f"most active #{i}: {rec.get('coastal_recent_works')} coastal works "
                f"in the last 5 years")
        rec["rationale"] = f"{rec['rationale']}; {note}" if rec.get("rationale") else note

    this_year = date.today().year
    ris = [r for r in pool
           if (r.get("first_pub_year") or 0) >= this_year - RISING_WINDOW
           and (r.get("works_count") or 0) >= RISING_MIN_WORKS]
    ris.sort(key=lambda r: ((r.get("two_yr_mean_citedness") or 0.0),
                            (r.get("coastal_recent_works") or 0)), reverse=True)
    for i, rec in enumerate(ris[:COHORTS["rising"]], 1):
        rec["is_rising"], rec["rank_rising"] = True, i
        note = (f"rising #{i}: first published {rec.get('first_pub_year')}, "
                f"2-year mean citedness {rec.get('two_yr_mean_citedness'):.2f}"
                if rec.get("two_yr_mean_citedness") is not None
                else f"rising #{i}: first published {rec.get('first_pub_year')}")
        rec["rationale"] = f"{rec['rationale']}; {note}" if rec.get("rationale") else note

    selected = [r for r in pool if r["is_preeminent"] or r["is_most_active"] or r["is_rising"]]
    print(f"[D] selected {len(selected)}: "
          f"{sum(r['is_preeminent'] for r in selected)} pre-eminent, "
          f"{sum(r['is_most_active'] for r in selected)} most-active, "
          f"{sum(r['is_rising'] for r in selected)} rising")
    return selected


# ── write ─────────────────────────────────────────────────────────────

def load_seed() -> list[dict]:
    if not SEED_JSON.exists():
        print(f"[error] seed not found: {SEED_JSON}", file=sys.stderr)
        raise SystemExit(2)
    records = json.loads(SEED_JSON.read_text())
    for rec in records:
        rec.setdefault("source", "websearch-curated")
        rec.setdefault("retrieved_at", date.today().isoformat())
        rec.pop("notes_clusters", None)
    print(f"[seed] {len(records)} curated scholars from "
          f"{SEED_JSON.relative_to(ROOT)}")
    return records


def reconcile(harvested: list[dict], curated: list[dict]) -> list[dict]:
    """Fold curated rows into harvested ones. A curated scholar matched by
    ORCID or Google-Scholar id keeps their curation rationale and gains
    measured metrics; an unmatched curated scholar is kept as-is so a
    hand-picked expert is never silently dropped by a threshold."""
    by_orcid = {r["orcid"]: r for r in harvested if r.get("orcid")}
    by_gs = {r["google_scholar_id"]: r for r in harvested
             if r.get("google_scholar_id")}
    matched = 0
    for c in curated:
        target = by_orcid.get(c.get("orcid")) or by_gs.get(c.get("google_scholar_id"))
        if target:
            matched += 1
            target["source"] = "openalex+curated"
            if c.get("rationale"):
                target["rationale"] = (
                    f"{target.get('rationale') or ''}; curated: {c['rationale']}".lstrip("; "))
            for fld in ("google_scholar_id", "homepage_url", "source_url"):
                target.setdefault(fld, None)
                if not target.get(fld) and c.get(fld):
                    target[fld] = c[fld]
            continue
        harvested.append(c)
    print(f"[E] reconciled {matched} curated scholar(s) with harvest results; "
          f"{len(curated) - matched} kept as curated-only")
    return harvested


def finalize(records: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        aid = rec.get("openalex_id")
        sid = rec.get("scholar_id") or aid
        if not sid:
            continue
        row = {c: rec.get(c) for c in COLS}
        row["scholar_id"] = sid
        row["openalex_id"] = aid
        row.setdefault("confidence", "medium")
        row["confidence"] = row.get("confidence") or "medium"
        row["source"] = row.get("source") or "openalex"
        row["retrieved_at"] = row.get("retrieved_at") or date.today().isoformat()
        for flag in ("is_preeminent", "is_most_active", "is_rising"):
            row[flag] = bool(row.get(flag))
        # A rank without its flag (or vice versa) would fail the QA gate.
        for flag, rank in (("is_preeminent", "rank_preeminent"),
                           ("is_most_active", "rank_most_active"),
                           ("is_rising", "rank_rising")):
            if not row[flag]:
                row[rank] = None
        out.append(row)
    return out


def link_people(conn, rows: list[dict]) -> int:
    """Attach person_id ONLY on ORCID or openalex_id equality. Name
    matching is banned here — it is exactly what produced the bogus
    attributions that wipe_bad_openalex_attributions.py had to undo."""
    people = conn.execute(
        "SELECT person_id, orcid, openalex_id FROM people "
        "WHERE orcid IS NOT NULL OR openalex_id IS NOT NULL").fetchall()
    by_orcid = {o: p for p, o, _ in people if o}
    by_oa = {a: p for p, _, a in people if a}
    n = 0
    for row in rows:
        pid = by_orcid.get(row.get("orcid")) or by_oa.get(row.get("openalex_id"))
        if pid:
            row["person_id"] = pid
            n += 1
    return n


def write_table(conn, rows: list[dict], export: bool) -> None:
    conn.execute("DELETE FROM community_scholars;")
    conn.executemany(
        f"INSERT INTO community_scholars ({', '.join(COLS)}) "
        f"VALUES ({', '.join('?' * len(COLS))})",
        [[r.get(c) for c in COLS] for r in rows],
    )
    print(f"[db] community_scholars: {len(rows)} rows")
    if not export:
        return
    for base in PARQUET_OUT:
        base.mkdir(parents=True, exist_ok=True)
        out = base / "community_scholars.parquet"
        conn.execute(f"COPY (SELECT * FROM community_scholars) TO '{out}' (FORMAT PARQUET)")
    print("[parquet] community_scholars -> " + ", ".join(
        str((b / 'community_scholars.parquet').relative_to(ROOT)) for b in PARQUET_OUT))
    print("[note] parquet files are gitignored; stage them with `git add -f`")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--seed", action="store_true",
                      help="load the curated roster (no network)")
    mode.add_argument("--harvest", action="store_true",
                      help="measure the field against OpenAlex (needs network)")
    mode.add_argument("--resolve-topics", action="store_true",
                      help="resolve topic labels to T##### ids and exit")
    ap.add_argument("--stage", choices=["A", "B", "C", "all"], default="all")
    ap.add_argument("--resume", action="store_true", help="reuse cached stage output")
    ap.add_argument("--dry-run", action="store_true", help="compute but don't write")
    ap.add_argument("--skip-export", action="store_true", help="don't refresh parquet")
    ap.add_argument("--emit-empty-parquet", action="store_true",
                    help="write a zero-row parquet so the browser can register the view")
    args = ap.parse_args()

    if args.resolve_topics:
        rows = resolve_topics(make_session(), read_topics())
        unresolved = [r["label"] for r in rows
                      if not TOPIC_ID_RE.match((r.get("openalex_topic_id") or "").strip())]
        print(f"\n[topics] paste these ids into {TOPICS_CSV.relative_to(ROOT)}:")
        for r in rows:
            print(f"  {r.get('openalex_topic_id'):>10}  {r['label']}")
        if unresolved:
            print(f"[warn] still unresolved: {unresolved}")
        return 0

    if not args.db.exists():
        print(f"[error] {args.db} not found — run scripts/rebuild_db_from_parquet.py first",
              file=sys.stderr)
        return 2
    conn = duckdb.connect(str(args.db))
    conn.execute("SET search_path = main;")

    if args.emit_empty_parquet:
        for base in PARQUET_OUT:
            base.mkdir(parents=True, exist_ok=True)
            out = base / "community_scholars.parquet"
            conn.execute(
                f"COPY (SELECT * FROM community_scholars LIMIT 0) TO '{out}' (FORMAT PARQUET)")
        print("[parquet] wrote zero-row community_scholars.parquet to both dirs")
        conn.close()
        return 0

    if args.harvest:
        session = make_session()
        topics = resolve_topics(session, read_topics())
        tids = topic_ids(topics)
        if len(tids) < len(topics):
            print(f"[error] {len(topics) - len(tids)} topic(s) unresolved — "
                  f"run --resolve-topics and update {TOPICS_CSV.relative_to(ROOT)} "
                  f"so the roster stays reproducible", file=sys.stderr)
            return 1

        candidates = stage_a(session, topics, args.resume)
        if args.stage == "A":
            return 0
        authors = stage_b(session, candidates, args.resume)
        if args.stage == "B":
            return 0
        short = shortlist(authors)
        print(f"[C] measuring {len(short)} shortlisted authors "
              f"(of {len(authors)} hydrated)")
        metrics = stage_c(session, short, tids, args.resume)
        if args.stage == "C":
            return 0
        selected = stage_d(authors, metrics)
        rows = finalize(reconcile(selected, load_seed()))
    else:
        # Default (and --seed): curated roster only.
        rows = finalize(load_seed())

    n_linked = link_people(conn, rows)
    print(f"[E] linked {n_linked} scholar(s) to existing people rows "
          f"(ORCID / OpenAlex id equality only)")

    if args.dry_run:
        print(f"[dry-run] would write {len(rows)} rows; nothing written")
        conn.close()
        return 0

    write_table(conn, rows, export=not args.skip_export)
    for label, col in (("pre-eminent", "is_preeminent"),
                       ("most active", "is_most_active"),
                       ("rising", "is_rising")):
        n = conn.execute(f"SELECT COUNT(*) FROM community_scholars WHERE {col}").fetchone()[0]
        print(f"  {label:12s} {n}")
    n_orcid = conn.execute(
        "SELECT COUNT(*) FROM community_scholars WHERE orcid IS NOT NULL").fetchone()[0]
    n_metrics = conn.execute(
        "SELECT COUNT(*) FROM community_scholars WHERE h_index IS NOT NULL").fetchone()[0]
    print(f"  with ORCID   {n_orcid}")
    print(f"  with metrics {n_metrics}")
    if n_metrics == 0:
        print("[note] no measured metrics yet — run with --harvest once "
              "api.openalex.org is reachable")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
