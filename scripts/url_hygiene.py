#!/usr/bin/env python3
"""URL hygiene: clear person homepages that are really facility landing pages.

A person's ``homepage_url`` is meant to be *their* page — a staff profile,
a lab site. Many of the researched personnel rows instead carry the
facility's own home page (``https://secoora.org/``) or a generic listing
(``…/our-team/``, ``…/people/``), so the "Homepage" link on a person's card
leads to the institution, not to the person. This pass sets those to NULL,
so the card falls through to the person's ORCID or, where the UI has the
cascade, the facility's own URL (which is where the link pointed anyway).

Ported from lto's scripts/url_hygiene.py, narrowed to that one job:

  * Only ``people.homepage_url`` is changed — never ``facilities.url``
    (a root URL IS a facility's intended landing page) and nothing is
    rewritten to a new value (lto's nrs.fs.fed.us host migration has no
    cod-kmap counterpart).
  * A homepage is nulled for one of two reasons, both about the URL alone:
      1. ``facility-url``: it equals some facility's ``url`` after
         normalising scheme, ``www.``, case and trailing slash;
      2. ``pattern``: it matches HOMEPAGE_PATTERNS, lto's list of landing /
         listing page shapes (bare domain, /about, /staff, /people, /our-team,
         …) — first match wins — unless the URL is in KEEP_URLS (a
         personal site that happens to live at a bare domain).
    Nothing is ever decided from a person's name: a name says nothing about
    whether a URL is a profile, and name-keyed rules are exactly what
    attached cardiologists to marine labs (see the wipe_* scripts).
  * The same decision is carried to ``person_registry.homepage_url`` for
    the registry row that IS that person (same ``person_id``) and still
    holds the identical URL, because the People tab reads the registry
    and build_person_registry.py would copy the NULL across on its next
    run anyway. A registry homepage that came from elsewhere is untouched.

Every change lands in ONE lake snapshot (commit message ``url_hygiene: …``)
with ``assert_integrity`` before the commit, and is appended to the decision
log ``data/seed/url_hygiene_log.csv`` (table, id, name, field, old, new,
decision, checked_at). The pass is idempotent: a second run finds nothing.

``--dry-run`` opens the lake read-only, prints every candidate and writes
nothing — not even the log, which records applied decisions only.

A scratch lake (``--catalog`` / ``$COD_LAKE_CATALOG`` not the default)
writes its log under ``<catalog dir>/export/data/seed/`` (cod_db.export_root),
never into the repo.

No network. The companion scripts/check_url_health.py does the HTTP checks.

Usage::

    python scripts/url_hygiene.py --dry-run
    python scripts/url_hygiene.py
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

LOG_REL = Path("data") / "seed" / "url_hygiene_log.csv"
LOG_FIELDS = ["table", "id", "name", "field", "old", "new", "decision", "checked_at"]

# Landing / listing page shapes — a URL here is an institution's page, not a
# person's. Copied from lto HOMEPAGE_PATTERNS minus its one rewrite rule
# (nrs.fs.fed.us -> research.fs.usda.gov). Order matters: first match wins.
HOMEPAGE_PATTERNS = [
    # Generic /about/staff or /staff/profile/ (without a per-person path).
    re.compile(r"^https?://[^/]+/about/staff\.(?:html|php|aspx)/?$"),
    re.compile(r"^https?://[^/]+/about/staff/?$"),
    re.compile(r"^https?://[^/]+/staff/profile/?$"),
    re.compile(r"^https?://[^/]+/staff/?$"),
    re.compile(r"^https?://[^/]+/staff-profiles/?$"),
    re.compile(r"^https?://[^/]+/about/?$"),
    re.compile(r"^https?://[^/]+/about-us/staff/?$"),
    re.compile(r"^https?://[^/]+/contact/?$"),
    re.compile(r"^https?://[^/]+/contact-us/?$"),
    re.compile(r"^https?://[^/]+/contactus\.htm$"),
    re.compile(r"^https?://[^/]+/leadership/?$"),
    re.compile(r"^https?://[^/]+/About/Leadership/?$"),
    # NPS unit /im/<network>/contact[us].htm and /<unit>/learn/nature/index.htm
    # are unit pages, not individual profiles.
    re.compile(r"^https?://www\.nps\.gov/im/[a-z]+/contact(?:us)?\.htm$"),
    re.compile(r"^https?://www\.nps\.gov/im/contactus\.htm$"),
    re.compile(r"^https?://www\.nps\.gov/[a-z]+/learn/nature/index\.htm$"),
    # NGO / foundation team pages.
    re.compile(r"^https?://[^/]+/our-team/?$"),
    re.compile(r"^https?://[^/]+/our-people/?$"),
    re.compile(r"^https?://[^/]+/our-staff/?$"),
    re.compile(r"^https?://[^/]+/our-scientists/?$"),
    # Listing pages that end at /people/ or /faculty/ with no person slug.
    re.compile(r"^https?://[^/]+/people/?$"),
    re.compile(r"^https?://[^/]+/faculty/?$"),
    re.compile(r"^https?://[^/]+/researchers/?$"),
    re.compile(r"^https?://[^/]+/scientists/?$"),
    # ARS region/location/lab people listing without a final slug.
    re.compile(r"^https?://www\.ars\.usda\.gov/[a-z-]+/[a-z-]+/[a-z-]+/people/?$"),
    # USFS bare-region pages (www.fs.usda.gov/srs/ …).
    re.compile(r"^https?://www\.fs\.usda\.gov/[a-z]+/?$"),
    # Bare root with no path at all.
    re.compile(r"^https?://[^/]+/?$"),
]

# URLs a pattern matches that are nonetheless a person's own page (a
# personal or lab site served at a bare domain). Keyed on the URL — never
# on a name — and checked before the patterns; a facility-url match still
# wins, since a facility's own address cannot be one person's page.
KEEP_URLS = {
    "https://jtmorris.uofsccreate.org/",  # James T. Morris's lab site, not a facility
}

# Same normalisation on both sides of the facility-url comparison.
_NORM_SQL = "rtrim(regexp_replace(lower(trim({c})), '^https?://(www\\.)?', ''), '/')"


def decide(url: str, facility_hit: str | None) -> str | None:
    """Decision label for one homepage, or None to keep it."""
    if facility_hit:
        return f"null:facility-url:{facility_hit}"
    s = url.strip()
    if s in KEEP_URLS:
        return None
    for rx in HOMEPAGE_PATTERNS:
        if rx.match(s):
            return "null:pattern:" + rx.pattern
    return None


def candidates(con) -> list[tuple[str, str, str, str]]:
    """(person_id, name, homepage_url, decision) for every homepage to clear."""
    rows = con.execute(f"""
        WITH fac AS (
            SELECT {_NORM_SQL.format(c='url')} AS k,
                   min(facility_id) AS facility_id
            FROM facilities WHERE url IS NOT NULL AND trim(url) <> ''
            GROUP BY 1)
        SELECT p.person_id, p.name, p.homepage_url, fac.facility_id
        FROM people p
        LEFT JOIN fac ON fac.k = {_NORM_SQL.format(c='p.homepage_url')}
        WHERE p.homepage_url IS NOT NULL
        ORDER BY p.person_id""").fetchall()
    out = []
    for pid, name, url, fid in rows:
        d = decide(url, fid)
        if d:
            out.append((pid, name, url, d))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the candidates; change nothing, write no log")
    ap.add_argument("--log", type=Path, default=None,
                    help=f"decision log (default: <export root>/{LOG_REL})")
    args = ap.parse_args(argv)

    log_path = args.log or cod_db.export_root(args.catalog) / LOG_REL
    today = date.today().isoformat()

    with cod_db.connect(read_only=args.dry_run, catalog=args.catalog,
                        data=args.lake_data) as con:
        audited = cod_db.scalar(con, "SELECT count(homepage_url) FROM people")
        found = candidates(con)
        by_rule = {"facility-url": 0, "pattern": 0}
        for _pid, _name, _url, d in found:
            by_rule[d.split(":")[1]] += 1
        print(f"[url_hygiene] people.homepage_url: {len(found)} of {audited} are facility "
              f"landing pages ({by_rule['facility-url']} equal a facilities.url, "
              f"{by_rule['pattern']} match a landing-page pattern)")
        for pid, name, url, d in found:
            print(f"  {pid}  {name:<34} {url}   [{d.split(':', 2)[1]}]")

        log_rows: list[dict] = []
        if not args.dry_run and found:
            msg = f"url_hygiene: null {len(found)} people.homepage_url landing pages"
            with cod_db.transaction(con, msg):
                con.execute("CREATE OR REPLACE TEMP TABLE _url_fix "
                            "(person_id VARCHAR, old VARCHAR, decision VARCHAR)")
                con.executemany("INSERT INTO _url_fix VALUES (?, ?, ?)",
                                [(pid, url, d) for pid, _n, url, d in found])
                # The registry rows that ARE these people and still carry the
                # same URL — read before people is changed.
                reg = con.execute(
                    "SELECT r.canonical_id, r.display_name, r.homepage_url, f.decision "
                    "FROM person_registry r JOIN _url_fix f "
                    "  ON f.person_id = r.person_id AND r.homepage_url = f.old "
                    "ORDER BY r.canonical_id").fetchall()
                con.execute(
                    "UPDATE people SET homepage_url = NULL, updated_at = now() "
                    "FROM _url_fix WHERE people.person_id = _url_fix.person_id "
                    "AND people.homepage_url = _url_fix.old")
                con.execute(
                    "UPDATE person_registry SET homepage_url = NULL "
                    "FROM _url_fix WHERE person_registry.person_id = _url_fix.person_id "
                    "AND person_registry.homepage_url = _url_fix.old")
                con.execute("DROP TABLE _url_fix")
                cod_db.assert_integrity(con, ["people", "person_registry"])
            log_rows += [dict(table="people", id=pid, name=name, field="homepage_url",
                              old=url, new="", decision=d, checked_at=today)
                         for pid, name, url, d in found]
            log_rows += [dict(table="person_registry", id=cid, name=name,
                              field="homepage_url", old=url, new="", decision=d,
                              checked_at=today)
                         for cid, name, url, d in reg]
            print(f"[url_hygiene] nulled {len(found)} people.homepage_url and "
                  f"{len(reg)} matching person_registry.homepage_url in one snapshot")

    if args.dry_run:
        print("[url_hygiene] dry run: nothing changed, no log written")
        return 0
    if log_rows:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not log_path.exists()
        with log_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if write_header:
                w.writeheader()
            w.writerows(log_rows)
    print(f"[url_hygiene] log -> {log_path} (+{len(log_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
