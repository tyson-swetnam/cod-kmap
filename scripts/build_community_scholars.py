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
scripts/wipe_bad_openalex_attributions.py):

  * Harvest candidates are OpenAlex author ids from the start, so a person
    is never *resolved* by name. A coastal-share gate then drops authors
    whose coastal work is incidental, which is what previously let a
    cardiologist onto a marine-lab page.
  * A scholar is linked to an existing `people` row only on ORCID or
    openalex_id equality — never on a name.
  * reconcile() does compare names, but only to decide whether two rows
    already in this roster describe one researcher. That never attributes a
    publication, so it cannot cause a wrong-person attribution; and
    same_researcher() requires spelled-out given names to agree, so
    "Y. Stacy Zhang" and "Y. Joseph Zhang" stay two people.

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
import unicodedata
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
# Stage A's second pass: top authors per topic over the last
# RECENT_WINDOW years only, so early-career researchers reach the
# candidate pool at all. Without it the rising cohort is always empty
# (see the comment in stage_a).
RECENT_WINDOW = 6
TOP_AUTHORS_RECENT = 150
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import openalex_auth

    openalex_auth.require_api_key()
    print("[api] authenticated with OPENALEX_API_KEY")
    return openalex_auth.openalex_session()


def get(session, path: str, params: dict, tries: int = 4) -> dict | None:
    """GET with backoff on the transient statuses. Returns None once the
    retries are spent so the caller can skip one author rather than lose
    the whole run."""
    url = f"{OPENALEX}/{path.lstrip('/')}"
    # The api_key is attached by the session (scripts/openalex_auth.py),
    # which also strips any stray mailto — OpenAlex rejects both together.
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


def scholar_ident(v: str | None) -> str | None:
    """Reduce a Google Scholar reference to its bare profile id.

    OpenAlex reports `ids.scholar` as a full profile URL
    (`http://scholar.google.com/citations?user=XXXX&hl=en`), while the
    curated seed stores the bare id. Comparing the two raw meant the
    Scholar-id matcher in reconcile() could never fire, so 332 of 346
    curated scholars would have been appended as duplicates of researchers
    the harvest had already measured.
    """
    if not v:
        return None
    v = v.strip()
    m = re.search(r"[?&]user=([A-Za-z0-9_-]+)", v)
    return m.group(1) if m else (v or None)


def _fold(v: str) -> str:
    v = unicodedata.normalize("NFKD", v or "")
    return "".join(ch for ch in v if not unicodedata.combining(ch)).lower()


def name_key(name: str | None) -> str:
    """Surname, accent-folded — a cheap bucket for candidate comparison."""
    parts = [p for p in re.split(r"[^A-Za-z]+", _fold(name or "")) if p]
    return parts[-1] if parts else ""


def same_researcher(a: str | None, b: str | None) -> bool:
    """Do these two names describe one researcher?

    Used ONLY to decide whether two roster rows are the same person, never to
    attribute a publication. Surnames must match, and any given name both
    spell out in full must agree: a middle initial may be absent from one form
    ("Sarah Giddings" / "Sarah N. Giddings"), but two different spelled-out
    names are different people ("Y. Stacy Zhang" / "Y. Joseph Zhang").
    """
    def split(v):
        parts = [p for p in re.split(r"[^A-Za-z]+", _fold(v or "")) if p]
        return (parts[:-1], parts[-1]) if len(parts) > 1 else ([], parts[0] if parts else "")

    def compatible(x, y):
        if x == y:
            return True
        if len(x) == 1 or len(y) == 1:
            return x[0] == y[0]
        short, long = sorted((x, y), key=len)
        return len(short) >= 3 and long.startswith(short)

    ga, sa = split(a)
    gb, sb = split(b)
    if not sa or sa != sb:
        return False
    if ga and gb and not compatible(ga[0], gb[0]):
        return False
    return all(compatible(ga[i], gb[i]) for i in range(1, min(len(ga), len(gb))))


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
        data = get(session, "topics", {"search": label, "per_page": 1})
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
    # Two passes per topic. Grouping /works by author over all time ranks
    # by lifetime output, which selects long-career authors exclusively:
    # on the first real run of this harvest the most recent first
    # publication year among all 509 measured candidates was 2012, so the
    # rising cohort (first published within RISING_WINDOW years) could not
    # be filled from that pool no matter how the thresholds were tuned —
    # stage D returned 0 rising and qa.py failed the cohort invariant. The
    # recent pass re-runs the same grouping restricted to the last
    # RECENT_WINDOW years, where an author who started in 2020 can rank.
    recent_cutoff = f"{date.today().year - RECENT_WINDOW}-01-01"
    passes = [
        ("all-time", "1990-01-01", TOP_AUTHORS_PER_TOPIC),
        ("recent", recent_cutoff, TOP_AUTHORS_RECENT),
    ]
    for tid in topic_ids(topics):
        for label, since, cap in passes:
            data = get(session, "works", {
                "filter": f"primary_topic.id:{tid},from_publication_date:{since}",
                "group_by": "authorships.author.id",
                "per_page": cap,
            })
            groups = (data or {}).get("group_by") or []
            found = 0
            for g in groups[:cap]:
                aid = short_id(g.get("key"))
                if aid and AUTHOR_ID_RE.match(aid):
                    ids.add(aid)
                    found += 1
            print(f"[A] {tid} {label}: +{found} authors "
                  f"(running total {len(ids)})")
    if not ids:
        # Every topic request failed (get() returns None once its retries are
        # spent, which yields an empty group list). Writing the checkpoint
        # here would make --resume treat "nothing found" as "already done".
        print("[error] stage A found no candidates — every topic request "
              "failed. Not writing a checkpoint; re-run when the API is "
              "reachable.", file=sys.stderr)
        raise SystemExit(1)
    CACHE.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sorted(ids)))
    print(f"[A] {len(ids)} unique candidate authors -> {out.relative_to(ROOT)}")
    return sorted(ids)


def author_record(a: dict) -> dict | None:
    """Flatten one OpenAlex author payload into a cache row.

    `last_known_institutions` is the current (plural, list) field — the
    same one scripts/seed_people_from_openalex.py filters on — but older
    payloads carry a singular `last_known_institution` object, so accept
    either rather than silently dropping every affiliation.
    """
    aid = short_id(a.get("id"))
    if not aid:
        return None
    insts = a.get("last_known_institutions")
    if isinstance(insts, list) and insts:
        inst = insts[0] or {}
    else:
        inst = a.get("last_known_institution") or {}
    stats = a.get("summary_stats") or {}
    return {
        "openalex_id": aid,
        "name": a.get("display_name"),
        "orcid": clean_orcid(a.get("orcid")),
        "google_scholar_id": scholar_ident((a.get("ids") or {}).get("scholar")),
        "affiliation": inst.get("display_name"),
        "affiliation_country": inst.get("country_code"),
        "affiliation_ror": inst.get("ror"),
        "works_count": a.get("works_count"),
        "cited_by_count": a.get("cited_by_count"),
        # h_index/i10 live under summary_stats on current payloads and at the
        # top level on older ones.
        "h_index": stats.get("h_index", a.get("h_index")),
        "i10_index": stats.get("i10_index", a.get("i10_index")),
        "two_yr_mean_citedness": stats.get("2yr_mean_citedness"),
        "top_topics": "; ".join(
            t.get("display_name") for t in (a.get("topics") or [])[:5]
            if t.get("display_name")) or None,
    }


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
    batch_ok = True
    # Append only when resuming. Without --resume `have` starts empty and
    # `todo` is the full candidate list, so appending to an existing cache
    # wrote every record a second time and the resumed read then counted
    # duplicates.
    with out.open("a" if resume else "w") as fh:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            authors: list[dict] = []
            # The |-separated id filter is the cheap path (one request per 50
            # authors) and is the same pattern backfill_publication_topics.py
            # uses against /works. It is NOT independently confirmed for
            # /authors, so treat an empty result as "this filter isn't
            # supported here" and fall back to one request per author rather
            # than silently hydrating nothing.
            if batch_ok:
                data = get(session, "authors", {
                    "filter": "ids.openalex:" + "|".join(chunk),
                    "per_page": BATCH,
                })
                authors = (data or {}).get("results") or []
                if not authors:
                    batch_ok = False
                    print("[B] batch id filter returned nothing — falling back to "
                          "per-author requests (slower, same result)")
            if not authors:
                for aid in chunk:
                    one = get(session, f"authors/{aid}", {})
                    if one and one.get("id"):
                        authors.append(one)

            for a in authors:
                rec = author_record(a)
                if rec:
                    have[rec["openalex_id"]] = rec
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
    # Both rankings above still favour established careers: 2-year mean
    # citedness rewards authors whose back catalogue is being cited, not
    # authors who are new. Reserve a slice for the smallest bodies of
    # work that still clear RISING_MIN_WORKS, ranked by citedness within
    # that group — an author with 8 works and a good citation rate is the
    # rising-cohort candidate, and would otherwise never be measured.
    early = [a for a in authors.values()
             if RISING_MIN_WORKS <= (a.get("works_count") or 0) <= 60]
    early.sort(key=lambda a: (a.get("two_yr_mean_citedness") or 0.0,
                              a.get("cited_by_count") or 0), reverse=True)
    by_early = [a["openalex_id"] for a in early[:SHORTLIST_SIZE // 2]]
    return list(dict.fromkeys(by_h + by_recent + by_early))


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
    with out.open("a" if resume else "w") as fh:   # see stage_b

        for n, aid in enumerate(todo, 1):
            all_time = get(session, "works", {
                "filter": f"author.id:{aid},primary_topic.id:{topic_filter}",
                "per_page": 1})
            recent = get(session, "works", {
                "filter": (f"author.id:{aid},primary_topic.id:{topic_filter},"
                           f"from_publication_date:{cutoff}"),
                "per_page": 1})
            first = get(session, "works", {
                "filter": f"author.id:{aid}",
                "sort": "publication_date:asc", "per_page": 1})
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
    # No curated row carries an openalex_id today, but the seed invites
    # maintainers to fill one in, and it is the strongest identifier of the
    # three — so match on it first when present.
    by_oa = {r["openalex_id"]: r for r in harvested if r.get("openalex_id")}
    # Both sides are normalised through scholar_ident() so a bare curated id
    # can match an OpenAlex payload, which reports ids.scholar as a full
    # profile URL. Comparing them raw meant this matcher never fired.
    by_gs = {scholar_ident(r.get("google_scholar_id")): r for r in harvested
             if scholar_ident(r.get("google_scholar_id"))}
    # Most curated rows carry neither an ORCID nor a Scholar id — only ~20 of
    # 346 do — so identifier matching alone would append the other ~326 as
    # fresh rows even when the harvest had already measured that same person,
    # listing them twice in the Scholars tab.
    #
    # Falling back to a name comparison here is safe in a way that
    # name-resolving against OpenAlex is NOT: this only decides whether two
    # rows already in the roster describe one researcher. It never attributes
    # a publication, so it cannot produce the wrong-person attributions that
    # scripts/wipe_bad_openalex_attributions.py had to undo. The comparison
    # also requires spelled-out given names to agree, so "Y. Stacy Zhang" and
    # "Y. Joseph Zhang" stay separate people.
    by_name = {}
    for r in harvested:
        by_name.setdefault(name_key(r.get("name")), []).append(r)

    matched = matched_by_name = 0
    for c in curated:
        target = (by_oa.get(c.get("openalex_id"))
                  or by_orcid.get(c.get("orcid"))
                  or by_gs.get(scholar_ident(c.get("google_scholar_id"))))
        if target is None:
            for cand in by_name.get(name_key(c.get("name")), []):
                if same_researcher(c.get("name"), cand.get("name")):
                    target = cand
                    matched_by_name += 1
                    break
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
    print(f"[E] reconciled {matched} curated scholar(s) with harvest results "
          f"({matched_by_name} of them by name after no identifier matched); "
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

    # Renumber every cohort across the final set. Harvested rows arrive
    # ranked 1..100 and unmatched curated rows keep the ranks they were
    # seeded with (also starting at 1), so without this the table ships with
    # duplicate ranks — which the harvest's own qa.py invariant rejects, and
    # which would leave the Scholars tab unable to order a cohort.
    # Measured rows sort ahead of curated-only ones, and each cohort keeps
    # the ordering its own metric implies.
    ORDER = {
        "rank_preeminent": lambda r: (-(r.get("h_index") or 0),
                                      -(r.get("cited_by_count") or 0)),
        "rank_most_active": lambda r: (-(r.get("coastal_recent_works") or 0),
                                       -(r.get("coastal_works_count") or 0)),
        "rank_rising": lambda r: (-(r.get("two_yr_mean_citedness") or 0.0),
                                  -(r.get("coastal_recent_works") or 0)),
    }
    for flag, rank in (("is_preeminent", "rank_preeminent"),
                       ("is_most_active", "rank_most_active"),
                       ("is_rising", "rank_rising")):
        cohort = [r for r in out if r[flag]]
        cohort.sort(key=lambda r: (r.get("h_index") is None
                                   and r.get("coastal_recent_works") is None,
                                   ORDER[rank](r),
                                   str(r.get("name") or "")))
        for i, rec in enumerate(cohort, 1):
            rec[rank] = i
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
    ap.add_argument("--allow-unresolved-topics", action="store_true",
                    help="harvest even if coastal_topics.csv still holds RESOLVE "
                         "sentinels (resolves at run time; not reproducible)")
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
        if args.dry_run or args.skip_export:
            print("[dry-run] --emit-empty-parquet suppressed by "
                  "--dry-run/--skip-export")
            conn.close()
            return 0
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
        # Check the CSV as committed, BEFORE any resolution. resolve_topics()
        # fills the sentinel in on the fly, so checking afterwards could never
        # fire the guard the docs promise: a harvest would quietly define its
        # cohorts from whatever a search returned that day, and the committed
        # topic set would not explain the published roster.
        topics = read_topics()
        sentinels = [r["label"] for r in topics
                     if not TOPIC_ID_RE.match((r.get("openalex_topic_id") or "").strip())]
        if sentinels and not args.allow_unresolved_topics:
            print(f"[error] {len(sentinels)} topic(s) still hold the "
                  f"{SENTINEL} sentinel in {TOPICS_CSV.relative_to(ROOT)}:",
                  file=sys.stderr)
            for label in sentinels:
                print(f"          {label}", file=sys.stderr)
            print("[error] run --resolve-topics, paste the ids into that file, "
                  "and re-run, so the published roster stays traceable to an "
                  "explicit topic set. Use --allow-unresolved-topics to resolve "
                  "at run time anyway (the roster is then not reproducible).",
                  file=sys.stderr)
            return 1
        if sentinels:
            print(f"[warn] resolving {len(sentinels)} topic(s) at run time — "
                  f"this roster will not be reproducible from the committed CSV")
            topics = resolve_topics(session, topics)
        tids = topic_ids(topics)
        if len(tids) < len(topics):
            print(f"[error] {len(topics) - len(tids)} topic(s) could not be "
                  f"resolved against the OpenAlex topics endpoint",
                  file=sys.stderr)
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
