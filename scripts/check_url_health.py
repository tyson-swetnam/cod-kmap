#!/usr/bin/env python3
"""HTTP HEAD verification of every link URL in the cod-kmap lake.

Runs in CI (.github/workflows/url-health.yml), where the network is
unrestricted. Ported from lto's scripts/check_url_health.py with the
sources cut down to the link columns cod-kmap actually has:

  facilities.url
  people.homepage_url
  coastal_datasets.homepage_url
  coastal_datasets.source_url
  dataset_endpoints.url

(lto's data_archives / data_products / api_endpoints / cloud_buckets /
funding_events sources have no counterpart here.)

Each DISTINCT URL is requested once — an HTTP HEAD that follows redirects,
retried as a Range-limited GET (first 1 KiB) when the host rejects HEAD
with 403/405 — with a bounded thread pool (``--concurrency``, default 16),
a per-request timeout (``--timeout``, default 8 s) and an identifying
User-Agent. The verdict is recorded two ways:

  * ``data/seed/url_health_check.csv`` — the full audit, one row per
    (table, id, field, url): status, http_code, raw status_code (negative
    sentinels for failures), final_url after redirects, error, checked_at.
  * the lake's ``url_health`` table (url, status, http_code, checked_at),
    one row per distinct URL. export_parquet.py publishes it as
    db/parquet + public/parquet url_health.parquet; this script never
    writes parquet itself.

url_health is a schema.sql table, so it is refilled with DELETE + INSERT
inside one ``cod_db.transaction`` (commit message ``check_url_health: …``),
not ``replace_table``: a CREATE OR REPLACE would drop the schema.sql column
definitions. ``assert_integrity`` (url is the primary key) runs before the
commit.

Endpoint templates with ``{PLACEHOLDER}`` segments are never requested — a
literal GET of a template is guaranteed 4xx — and record as
``template-skipped`` with no http_code. (None exist in the catalogue today;
the rule is kept so one added later cannot paint a healthy row red.)

URL collection reads the lake; when the lake cannot be opened it falls back
to the committed db/parquet/ (force that with ``--from-parquet``). A real
sweep still needs the lake to write into, and checks for it before sending
any request.

A scratch lake (``--catalog`` / ``$COD_LAKE_CATALOG`` not the default)
writes its CSV under ``<catalog dir>/export/data/seed/`` (cod_db.export_root),
never into the repo.

Usage::

    python scripts/check_url_health.py --concurrency 16 --timeout 8
    python scripts/check_url_health.py --limit 30          # smoke test
    python scripts/check_url_health.py --dry-run [--from-parquet]

``--limit N`` checks only the first N distinct URLs (sorted) and replaces
url_health with just those: it is for smoke tests, not for a real sweep.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

try:
    import requests
except ImportError:  # dry-run still works without it
    requests = None

ROOT = Path(__file__).resolve().parent.parent
CSV_REL = Path("data") / "seed" / "url_health_check.csv"

# (table, id column, name column, url column). dataset_endpoints has no
# single-column key; dataset_id identifies the row well enough for the
# audit CSV (the url itself is the rest of its key).
URL_SOURCES = [
    ("facilities", "facility_id", "canonical_name", "url"),
    ("people", "person_id", "name", "homepage_url"),
    ("coastal_datasets", "dataset_id", "name", "homepage_url"),
    ("coastal_datasets", "dataset_id", "name", "source_url"),
    ("dataset_endpoints", "dataset_id", "label", "url"),
]

CSV_FIELDS = [
    "table", "id", "name", "field", "url",
    "status", "http_code", "status_code", "final_url", "error", "checked_at",
]

USER_AGENT = ("cod-kmap-url-health-check/1.0 "
              "(+https://github.com/tyson-swetnam/cod-kmap)")

# {SITE_ID}, {YYYY-MM-DD}, {USER}:{TOKEN}… — endpoint templates, not
# fetchable URLs. Anything matching this is classified template-skipped.
TEMPLATE_RE = re.compile(r"\{[^}]*\}")

# Sentinel status codes (< 100) recorded by check_one.
DRY_RUN, TIMEOUT, CONN_FAIL, OTHER_FAIL = -1, -2, -3, -9


def status_label(status_code: int | None) -> str:
    """Coarse verdict the frontend keys its health dots on.

    Sentinels: -1 dry-run / requests missing, -2 timeout, -3 connection
    failure, -9 anything else (SSL, malformed URL, redirect loop).
    Keep in step with URL_HEALTH_STATUSES in scripts/qa.py.
    """
    if status_code is None:
        return "template-skipped"
    if 200 <= status_code < 300:
        return "ok"
    if 300 <= status_code < 400:
        return "redirect"
    if 400 <= status_code < 500:
        return "client-error"
    if status_code >= 500:
        return "server-error"
    if status_code in (TIMEOUT, CONN_FAIL):
        return "timeout"
    if status_code == DRY_RUN:
        return "unchecked"
    return "server-error"


def http_code(status_code: int | None) -> int | None:
    """Real HTTP code, or None for sentinels / skipped templates."""
    return status_code if status_code is not None and status_code >= 100 else None


def collect_urls(con, rel_for=lambda table: table) -> list[dict]:
    """Every checkable (table, id, field, url) row.

    ``rel_for`` maps a table name to a FROM-clause relation: the bare name
    against the lake, a read_parquet() call against db/parquet/.
    """
    out = []
    for table, id_col, name_col, url_col in URL_SOURCES:
        try:
            res = con.execute(
                f"SELECT {id_col}, {name_col}, trim({url_col}) FROM {rel_for(table)} "
                f"WHERE {url_col} IS NOT NULL AND length(trim({url_col})) > 8 "
                f"ORDER BY 1, 3").fetchall()
        except duckdb.Error as e:
            print(f"[check_url_health] skipped {table}.{url_col}: "
                  f"{str(e).splitlines()[0][:120]}", file=sys.stderr)
            continue
        for rid, name, url in res:
            out.append({"table": table, "id": rid, "name": name or "",
                        "field": url_col, "url": url})
    return out


def collect_from_parquet() -> list[dict]:
    """URL collection without the lake — read the committed db/parquet/."""
    with duckdb.connect() as con:  # in-memory
        return collect_urls(
            con, rel_for=lambda t: "read_parquet("
                                   f"'{(cod_db.LOCAL_PARQUET / t).as_posix()}.parquet')")


def check_one(url: str, timeout: int) -> dict:
    """One URL → {status_code, final_url, error}."""
    if requests is None:
        return {"status_code": DRY_RUN, "final_url": "", "error": "requests-not-installed"}
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        if r.status_code in (405, 403):
            # Some hosts (NSF, NASA Earthdata, many CMSes) block HEAD; retry
            # with a Range-limited GET so the body is not downloaded.
            r = requests.get(url, allow_redirects=True, timeout=timeout, stream=True,
                             headers={**headers, "Range": "bytes=0-1023"})
            r.close()
        return {"status_code": r.status_code, "final_url": r.url, "error": ""}
    except requests.exceptions.Timeout:
        return {"status_code": TIMEOUT, "final_url": "", "error": "timeout"}
    except requests.exceptions.ConnectionError as e:
        return {"status_code": CONN_FAIL, "final_url": "",
                "error": f"conn:{type(e).__name__}"}
    except Exception as e:  # noqa: BLE001 — every failure is a verdict
        return {"status_code": OTHER_FAIL, "final_url": "",
                "error": f"{type(e).__name__}:{e}"[:120]}


def verdict(result: dict, checked_at: str) -> dict:
    code = result["status_code"]
    return {**result, "status": status_label(code), "http_code": http_code(code),
            "checked_at": checked_at}


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["table"], r["field"], str(r["id"]), r["url"]))
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"[check_url_health] wrote {len(rows)} rows -> {path}")


def write_lake(verdicts: dict[str, dict], catalog=None, data=None) -> int:
    """Per-URL verdicts -> url_health, in one lake snapshot."""
    with cod_db.connect(catalog=catalog, data=data) as con:
        with cod_db.transaction(con, f"check_url_health: url_health, "
                                     f"{len(verdicts)} URLs checked"):
            con.execute("CREATE OR REPLACE TEMP TABLE _url_health "
                        "(url VARCHAR, status VARCHAR, http_code INTEGER, "
                        "checked_at VARCHAR)")
            if verdicts:
                con.executemany(
                    "INSERT INTO _url_health VALUES (?, ?, ?, ?)",
                    [(u, v["status"], v["http_code"], v["checked_at"])
                     for u, v in sorted(verdicts.items())])
            # DELETE + INSERT, not replace_table: url_health is a schema.sql
            # table and must keep its column definitions.
            con.execute("DELETE FROM url_health")
            con.execute("INSERT INTO url_health (url, status, http_code, checked_at) "
                        "SELECT url, status, http_code, checked_at FROM _url_health")
            con.execute("DROP TABLE _url_health")
            cod_db.assert_integrity(con, ["url_health"])
        return cod_db.scalar(con, "SELECT count(*) FROM url_health")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--concurrency", type=int, default=16,
                    help="parallel requests (default 16)")
    ap.add_argument("--timeout", type=int, default=8,
                    help="per-request timeout in seconds (default 8)")
    ap.add_argument("--limit", type=int, default=None,
                    help="check only the first N distinct URLs (smoke tests)")
    ap.add_argument("--csv", type=Path, default=None,
                    help=f"audit CSV (default: <export root>/{CSV_REL})")
    ap.add_argument("--dry-run", action="store_true",
                    help="enumerate URLs and write the CSV; no requests, no lake write")
    ap.add_argument("--from-parquet", action="store_true",
                    help="collect URLs from db/parquet/ instead of the lake "
                         "(automatic when the lake cannot be opened)")
    args = ap.parse_args(argv)

    csv_path = args.csv or cod_db.export_root(args.catalog) / CSV_REL
    dry = args.dry_run or requests is None
    if requests is None and not args.dry_run:
        print("[check_url_health] requests not installed — dry-run only", file=sys.stderr)

    if not dry:
        # The sweep takes minutes; fail before it if there is nowhere to
        # write the result.
        cat = cod_db.catalog_path(args.catalog)
        if not cat.exists():
            raise cod_db.LakeNotFound(
                f"DuckLake catalog not found: {cat}\n"
                f"  Build it first:  python scripts/rebuild_lake_from_parquet.py")

    if args.from_parquet:
        rows = collect_from_parquet()
    else:
        try:
            with cod_db.connect(read_only=True, catalog=args.catalog,
                                data=args.lake_data) as con:
                rows = collect_urls(con)
        except (cod_db.LakeNotFound, duckdb.Error) as e:
            print(f"[check_url_health] cannot open the lake ({type(e).__name__}: "
                  f"{str(e).splitlines()[0]}) — falling back to db/parquet/",
                  file=sys.stderr)
            rows = collect_from_parquet()

    urls = sorted({r["url"] for r in rows})
    print(f"[check_url_health] {len(rows)} URL cells, {len(urls)} distinct URLs, "
          f"across {len(URL_SOURCES)} fields")
    if args.limit is not None:
        urls = urls[:args.limit]
        keep = set(urls)
        rows = [r for r in rows if r["url"] in keep]
        print(f"[check_url_health] --limit {args.limit}: checking {len(urls)} URLs "
              f"({len(rows)} cells); url_health will hold only these")

    today = date.today().isoformat()
    templated = [u for u in urls if TEMPLATE_RE.search(u)]
    checkable = [u for u in urls if not TEMPLATE_RE.search(u)]
    verdicts: dict[str, dict] = {
        u: verdict({"status_code": None, "final_url": "", "error": "template-skipped"},
                   today)
        for u in templated}
    if templated:
        print(f"[check_url_health] {len(templated)} template URLs ({{PLACEHOLDER}} "
              f"segments) marked template-skipped, not requested")

    if dry:
        for u in checkable:
            verdicts[u] = verdict({"status_code": DRY_RUN, "final_url": "",
                                   "error": "dry-run"}, today)
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
            futures = {ex.submit(check_one, u, args.timeout): u for u in checkable}
            for i, fut in enumerate(as_completed(futures), 1):
                verdicts[futures[fut]] = verdict(fut.result(), today)
                if i % 50 == 0:
                    print(f"  ...{i}/{len(checkable)} checked", flush=True)

    counts = Counter(v["status"] for v in verdicts.values())
    print("[check_url_health] status breakdown (distinct URLs):")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")

    write_csv([{**r, **verdicts[r["url"]]} for r in rows], csv_path)
    if dry:
        # CSV only: a dry run must not overwrite url_health (and so the
        # shipped url_health.parquet) with all-'unchecked' rows.
        return 0
    n = write_lake(verdicts, args.catalog, args.lake_data)
    print(f"[check_url_health] url_health now holds {n} URLs "
          "(run export_parquet.py to publish)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
