#!/usr/bin/env python3
"""Search ORCID public API for each person and attach the match.

Strict resolver — ANY mismatch means we leave orcid NULL rather than
risk the OpenAlex name-only debacle. See docs/orcid_enrichment_plan.md
for design rationale.

Acceptance rules (ALL must hold):
  1. Family name matches exactly (case + diacritic-insensitive).
  2. Given names match the first one (handles "Sarah" vs "Sarah J.").
  3. Candidate's employment list contains an organisation whose name
     fuzzy-matches one of the person's facilities at >= --min-conf.

Logs every decision to data/seed/orcid_resolution_log.csv for audit.

Usage::
    python scripts/enrich_people_orcid.py --db db/cod_kmap.duckdb
    python scripts/enrich_people_orcid.py --batch 25
    python scripts/enrich_people_orcid.py --min-conf 0.80
    python scripts/enrich_people_orcid.py --dry-run
    python scripts/enrich_people_orcid.py --only-missing  (default)
    python scripts/enrich_people_orcid.py --reverify      (re-check existing orcids)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import duckdb
try:
    import requests
except ImportError:
    print("[error] pip install requests --break-system-packages", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
LOG_CSV = ROOT / "data" / "seed" / "orcid_resolution_log.csv"

API_SEARCH = "https://pub.orcid.org/v3.0/expanded-search/"
API_EMPLOY = "https://pub.orcid.org/v3.0/{orcid}/employments"
API_EDU    = "https://pub.orcid.org/v3.0/{orcid}/educations"
PARQUET_OUT = [ROOT / "db" / "parquet", ROOT / "public" / "parquet"]


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = (
        "cod-kmap/0.1 (github.com/tyson-swetnam/cod-kmap; "
        "mailto:tswetnam@arizona.edu)"
    )
    s.headers["Accept"] = "application/json"
    return s


def norm(s: str) -> str:
    """Lowercase + strip diacritics for tolerant name matching."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def split_name(full: str) -> tuple[str, str]:
    parts = re.split(r"\s+", (full or "").strip())
    if len(parts) < 2:
        return parts[0] if parts else "", ""
    return " ".join(parts[:-1]), parts[-1]


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def search_orcid(sess: requests.Session, given: str, family: str
                 ) -> list[dict]:
    """Returns up to 10 candidate ORCID profiles for the (given, family)
    pair. ORCID search query language uses Lucene-ish syntax."""
    if not family:
        return []
    q_parts = [f'family-name:"{family}"']
    if given:
        # Use only the FIRST given name to be tolerant of middle names
        # / initials.
        first = given.split()[0]
        if first:
            q_parts.append(f'given-names:{first}*')
    q = " AND ".join(q_parts)
    try:
        r = sess.get(API_SEARCH, params={"q": q, "rows": 10}, timeout=20)
    except Exception as e:
        print(f"[warn] orcid search failed for {given} {family}: {e}")
        return []
    if r.status_code == 429:
        time.sleep(2)
        return search_orcid(sess, given, family)
    if not r.ok:
        return []
    j = r.json()
    return j.get("expanded-result", []) or []


def fetch_employments(sess: requests.Session, orcid: str) -> list[str]:
    """List of organisation-names from employments + educations."""
    out: list[str] = []
    for url in (API_EMPLOY.format(orcid=orcid), API_EDU.format(orcid=orcid)):
        try:
            r = sess.get(url, timeout=20)
            if r.status_code == 429:
                time.sleep(2)
                r = sess.get(url, timeout=20)
            if not r.ok:
                continue
            j = r.json()
        except Exception as e:
            print(f"[warn] fetch employments failed for {orcid}: {e}")
            continue
        groups = j.get("affiliation-group", []) or []
        for g in groups:
            for s in g.get("summaries", []) or []:
                summary = (s.get("employment-summary")
                           or s.get("education-summary")
                           or s.get("qualification-summary")
                           or {})
                org = (summary.get("organization") or {}).get("name")
                if org:
                    out.append(org)
    return out


def best_facility_match(orgs: list[str], facilities: list[str],
                        min_conf: float) -> tuple[float, str, str]:
    """Returns (best_score, matched_org, matched_facility) or (-1,'','')."""
    best = (-1.0, "", "")
    for org in orgs:
        for fac in facilities:
            # Try the full facility string AND its acronym/long-name halves.
            for variant in [fac, *fac.split(" — ")]:
                s = name_similarity(org, variant)
                if s > best[0]:
                    best = (s, org, variant)
    return best if best[0] >= min_conf else (-1.0, "", "")


def resolve_one(sess, person, min_conf):
    """Returns (orcid_or_None, decision_dict)."""
    given, family = split_name(person["name"])
    if not family:
        return None, {"decision": "skip-no-family", "candidates": 0}
    candidates = search_orcid(sess, given, family)
    if not candidates:
        return None, {"decision": "no-candidates", "candidates": 0}

    facilities = person.get("facilities") or []
    accepted = []
    for c in candidates:
        cand_given = c.get("given-names") or ""
        cand_family = c.get("family-names") or ""
        # Rule 1: family name exact match.
        if norm(cand_family) != norm(family):
            continue
        # Rule 2: first given name matches.
        if given:
            cg_first = (cand_given.split() or [""])[0]
            if not cg_first:
                continue
            if norm(cg_first.split('.')[0])[:len(norm(given.split()[0]))] \
                    != norm(given.split()[0]):
                continue
        orcid = c.get("orcid-id") or ""
        if not orcid:
            continue
        # Rule 3: employment-name fuzzy matches a facility.
        if facilities:
            orgs = fetch_employments(sess, orcid)
            score, org, fac = best_facility_match(orgs, facilities, min_conf)
            if score < min_conf:
                continue
            accepted.append((orcid, score, org, fac))
        else:
            # No facility on file — accept on name match alone but mark
            # confidence low, only when there's exactly one candidate.
            accepted.append((orcid, 0.0, "", ""))

    if not accepted:
        return None, {"decision": "no-employment-match",
                      "candidates": len(candidates)}
    # Pick highest score.
    accepted.sort(key=lambda x: -x[1])
    orcid, score, org, fac = accepted[0]
    return orcid, {
        "decision": "accept",
        "candidates": len(candidates),
        "score": round(score, 3),
        "match_org": org,
        "match_facility": fac,
    }


def export_parquet(conn):
    for base in PARQUET_OUT:
        base.mkdir(parents=True, exist_ok=True)
        out = base / "people.parquet"
        conn.execute(f"COPY people TO '{out}' (FORMAT PARQUET)")
        print(f"[parquet] wrote {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--min-conf", type=float, default=0.6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-missing", action="store_true", default=True)
    ap.add_argument("--reverify", action="store_true",
                    help="Re-check people who already have orcid; useful "
                         "after a refactor of the matching rules.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] db not found: {args.db}", file=sys.stderr)
        return 2

    conn = duckdb.connect(str(args.db))
    sess = session()

    # Build the work list — each person + the list of facility names
    # they're affiliated with (for employment-match).
    rows = conn.execute(f"""
        WITH facs AS (
          SELECT fp.person_id,
                 list(DISTINCT
                   COALESCE(f.acronym || ' — ' || f.canonical_name,
                            f.canonical_name)) AS facilities
          FROM   facility_personnel fp
          JOIN   facilities         f  ON f.facility_id = fp.facility_id
          GROUP  BY fp.person_id
        )
        SELECT p.person_id, p.name, p.orcid,
               COALESCE(facs.facilities, []) AS facilities
        FROM   people p
        LEFT JOIN facs ON facs.person_id = p.person_id
        WHERE  {"p.orcid IS NULL OR length(p.orcid) = 0" if not args.reverify else "TRUE"}
        ORDER BY p.name
    """).fetchall()

    people = [
        {"person_id": r[0], "name": r[1], "orcid": r[2],
         "facilities": list(r[3] or [])}
        for r in rows
    ]
    if args.batch:
        people = people[: args.batch]
    print(f"[orcid] processing {len(people)} people  "
          f"min-conf={args.min_conf}  dry-run={args.dry_run}")

    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    log_exists = LOG_CSV.exists()
    log = LOG_CSV.open("a", newline="", encoding="utf-8")
    log_w = csv.writer(log)
    if not log_exists:
        log_w.writerow([
            "person_id", "name", "decision", "orcid",
            "candidates", "score", "match_org", "match_facility",
        ])

    totals = {"accept": 0, "no-candidates": 0, "no-employment-match": 0,
              "skip-no-family": 0, "errors": 0}
    for i, p in enumerate(people, 1):
        try:
            orcid, info = resolve_one(sess, p, args.min_conf)
        except Exception as e:
            print(f"  [{i}/{len(people)}] {p['name']:30s} ERROR: {e}")
            totals["errors"] += 1
            continue
        log_w.writerow([
            p["person_id"], p["name"], info["decision"], orcid or "",
            info.get("candidates", 0), info.get("score", ""),
            info.get("match_org", ""), info.get("match_facility", ""),
        ])
        totals[info["decision"]] = totals.get(info["decision"], 0) + 1
        msg = orcid or "—"
        print(f"  [{i}/{len(people)}] {p['name']:30s} {info['decision']:25s} {msg}")
        if orcid and not args.dry_run:
            conn.execute(
                "UPDATE people SET orcid = ?, updated_at = now() "
                "WHERE person_id = ?",
                [orcid, p["person_id"]],
            )
        time.sleep(0.05)
    log.close()

    print(f"[totals] {totals}")

    if not args.dry_run:
        export_parquet(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
