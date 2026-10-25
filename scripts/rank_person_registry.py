#!/usr/bin/env python3
"""Rank the registry and split it into a shipped core tier and a local archive.

The browser loads parquet directly into DuckDB-Wasm, so the site cannot
serve the whole harvested population — 190k authors match the coastal
topic filter. This script scores every registry row and promotes the top
``--core-size`` to ``tier='core'``; only that tier is exported to
public/parquet (by scripts/export_parquet.py, which filters on tier). The
full population stays in the lake and its db/parquet mirror for analysis.

Scoring
-------
Four components, each converted to a percentile within the scored
population. Percentiles rather than raw values because the components have
incomparable ranges — cited_by_count runs to six figures while
coastal_share is bounded at 1.0 — so a raw-weighted sum would be decided
almost entirely by the citation term.

  coastal_output   coastal_works_count       — how much coastal work
  impact           h_index                   — how well cited
  recency          two_yr_mean_citedness     — recent citation rate
  connectivity     registry co-author degree — embedded in the community

Note on `recency`: two_yr_mean_citedness is OpenAlex's 2-year mean
citedness, i.e. how heavily the author's recent work is being cited. It is
a recency-weighted *impact* measure, not a measure of recent output
volume. A recent-output rate would be the better signal here — the harvest
already computes one from counts_by_year — but person_registry has no
column for it yet, so this is the best proxy the stored schema supports.
Adding a recent_works column and switching this term is the obvious next
refinement.

Weights are a parameter, not a constant, because "most important" is an
editorial judgement the project owns. The defaults weight coastal output
and impact equally and treat recency and connectivity as tiebreakers.

Anyone already in the COD orbit — a Team member, a person staffing a
catalogued facility, or a hand-curated scholar — is pinned into the core
tier regardless of score. They are the roster the site exists to show,
and a metric threshold must never drop them.

Usage::

    python scripts/rank_person_registry.py --core-size 10000
    python scripts/rank_person_registry.py --dry-run

A real run writes tier/tier_rank/tier_score in one lake snapshot; then run
scripts/export_parquet.py. --export-parquet is accepted for old callers and
does nothing.

Ties are broken by canonical_id, so the ranking is reproducible: the old
``ROW_NUMBER() OVER (ORDER BY pinned DESC, score DESC)`` numbered tied
scores in whatever order the engine produced them.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

DEFAULT_WEIGHTS = dict(coastal_output=0.35, impact=0.35,
                       recency=0.15, connectivity=0.15)


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


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--core-size", type=int, default=10000)
    ap.add_argument("--w-coastal", type=float, default=DEFAULT_WEIGHTS["coastal_output"])
    ap.add_argument("--w-impact", type=float, default=DEFAULT_WEIGHTS["impact"])
    ap.add_argument("--w-recency", type=float, default=DEFAULT_WEIGHTS["recency"])
    ap.add_argument("--w-connectivity", type=float,
                    default=DEFAULT_WEIGHTS["connectivity"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--export-parquet", action="store_true",
                    help="deprecated no-op: run scripts/export_parquet.py")
    args = ap.parse_args()

    try:
        conn = cod_db.connect(read_only=args.dry_run,
                              catalog=args.catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    total = cod_db.scalar(conn, "SELECT COUNT(*) FROM person_registry")
    print(f"[rank] scoring {total:,} registry row(s)")

    # Degree within the co-authorship graph, as a scoring input.
    conn.execute("""
        CREATE OR REPLACE TEMP VIEW _degree AS
        SELECT canonical_id, COUNT(*) AS deg FROM (
            SELECT canonical_id_a AS canonical_id FROM registry_collaborations
            UNION ALL
            SELECT canonical_id_b FROM registry_collaborations)
        GROUP BY canonical_id""")

    # PERCENT_RANK puts every component on [0,1] regardless of native
    # units. COALESCE to 0 first so a null metric scores at the floor
    # rather than dropping the row from the window.
    conn.execute(f"""
        CREATE OR REPLACE TEMP VIEW _scored AS
        SELECT r.canonical_id,
               r.is_team OR r.is_site_personnel OR r.source = 'community_scholars'
                                                          AS pinned,
               PERCENT_RANK() OVER (ORDER BY COALESCE(r.coastal_works_count,0))  AS p_coastal,
               PERCENT_RANK() OVER (ORDER BY COALESCE(r.h_index,0))              AS p_impact,
               PERCENT_RANK() OVER (ORDER BY COALESCE(r.two_yr_mean_citedness,0))AS p_recency,
               PERCENT_RANK() OVER (ORDER BY COALESCE(d.deg,0))                  AS p_conn
        FROM person_registry r
        LEFT JOIN _degree d USING (canonical_id)""")

    conn.execute(f"""
        CREATE OR REPLACE TEMP VIEW _final AS
        SELECT canonical_id, pinned,
               {args.w_coastal} * p_coastal + {args.w_impact} * p_impact
             + {args.w_recency} * p_recency + {args.w_connectivity} * p_conn AS score
        FROM _scored""")

    # canonical_id breaks ties so the ranking is reproducible run to run.
    conn.execute("""
        CREATE OR REPLACE TEMP TABLE _ranked AS
        SELECT canonical_id, pinned, score,
               ROW_NUMBER() OVER (ORDER BY pinned DESC, score DESC,
                                  canonical_id) AS rk
        FROM _final""")

    n_pinned = cod_db.scalar(conn, "SELECT count(*) FROM _ranked WHERE pinned")
    core_size = max(args.core_size, n_pinned)
    if core_size > args.core_size:
        print(f"[rank] {n_pinned:,} pinned row(s) exceed --core-size "
              f"{args.core_size:,}; core widened to {core_size:,} so no "
              f"COD-affiliated person is dropped")

    n_core = cod_db.scalar(conn, "SELECT count(*) FROM _ranked WHERE rk <= ?",
                           [core_size])
    print(f"[rank] core={n_core:,} ({n_pinned:,} pinned) archive={total - n_core:,}")

    if args.dry_run:
        top = conn.execute("""
            SELECT r.display_name, r.h_index, r.coastal_works_count, f.score
            FROM _final f JOIN person_registry r USING (canonical_id)
            WHERE NOT f.pinned ORDER BY f.score DESC, f.canonical_id LIMIT 8""").fetchall()
        print("[rank] top unpinned by score:")
        for name, h, cw, sc in top:
            print(f"    {sc:.3f}  h={h:<4} coastal={cw:<5} {name}")
        conn.close()
        return 0

    # One set-based UPDATE from the ranking table: a per-row UPDATE against
    # the lake is far slower at harvest scale (150k rows).
    with cod_db.transaction(conn, f"rank_person_registry: core={n_core} "
                                  f"archive={total - n_core} (core_size={core_size})"):
        conn.execute(f"""
            UPDATE person_registry
            SET tier = CASE WHEN s.rk <= {int(core_size)} THEN 'core'
                            ELSE 'archive' END,
                tier_rank = CAST(s.rk AS INTEGER),
                tier_score = s.score
            FROM _ranked s
            WHERE person_registry.canonical_id = s.canonical_id""")
        assert_written(conn, ["person_registry"])

    # Shipping is export_parquet.py's job: it exports tier='core' only, and
    # keeps person_identity_source / registry_facilities / person_validation
    # rows only for core-tier people and registry_collaborations /
    # coauthor_edges rows only when both endpoints ship (EXPORT_WHERE), or
    # the browser would hold edges pointing at absent nodes — which qa.py's
    # orphan-edge invariant would (correctly) fail on a site rebuild.
    print("[rank] tiers written to the lake (run export_parquet.py to publish)")
    if args.export_parquet:
        print("[note] --export-parquet is a no-op now; run "
              "scripts/export_parquet.py to publish the lake")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
