#!/usr/bin/env python3
"""Rebuild the DuckLake store of record from the committed parquet files.

Run this first after a fresh clone or a pull, before anything that opens
the lake::

    python scripts/rebuild_lake_from_parquet.py
    python scripts/rebuild_lake_from_parquet.py --strict
    COD_LAKE_CATALOG=/tmp/x/cat.sqlite python scripts/rebuild_lake_from_parquet.py

Why from parquet: ``db/parquet/*.parquet`` is the committed, canonical copy
of the data — every table, including the archive tier of
``person_registry`` and the db-only research tables. The lake —
``db/cod_kmap_catalog.sqlite`` + ``db/lake/`` — is local working state:
gitignored, its snapshot ids mean nothing across clones, and it can always
be thrown away and rebuilt from here. This replaces
``rebuild_db_from_parquet.py`` and ``db/cod_kmap.duckdb``.

Which parquet: ``db/parquet/`` unless ``--parquet`` names another folder.
``public/parquet/`` is NOT a valid source for a full rebuild: it is the
filtered publish target (core-tier registry rows only, no db-only tables,
emails redacted), so a lake built from it would lose data on the next
export. ``--parquet public/parquet`` is allowed for experiments only.

Which lake: ``--catalog`` (or ``$COD_LAKE_CATALOG``) and ``--lake-data``.
An explicit ``--catalog`` ignores ``$COD_LAKE_DATA``; see the safety
rules below for the pairings this script refuses.

What it does, in ONE lake transaction (one snapshot, with a commit
message):

  1. Executes ``schema/schema.sql`` in a throwaway in-memory "spec"
     database (see ``cod_db.spec``). That is the logical schema, with every
     PRIMARY KEY / FOREIGN KEY / UNIQUE / CHECK.
  2. Creates each spec table in the lake column-only — name, type,
     NOT NULL, DEFAULT — because DuckLake rejects the other constraints.
  3. Loads each table from ``<table>.parquet`` with ``INSERT ... BY NAME``
     (immune to column-order drift; a column missing from an older parquet
     takes its DEFAULT/NULL; a pandas-written column typed BIGINT or NULL
     is cast to the schema.sql type), reading ``file_row_number`` and
     ordering by it so rows land in file order. Spec tables with no parquet
     (``ingest_runs``, ``url_health`` until its first sweep) stay empty.
  4. Creates every parquet file that has no spec table *or view* as a
     table as-is (the derived ``compute_*`` / ``build_mvg_layout`` outputs
     and the db-only research tables). ``funding_links`` is a view in the
     schema (over ``funding_events``) and a file in parquet, because the
     exporter materialises the view so the browser can read it; the view
     wins and the file is not loaded — it carries nothing ``funding_events``
     does not.
  5. Recreates the schema views in the lake, in creation order.
  6. Runs the generic integrity check (``cod_db.integrity_violations``)
     and reports it. Unlike the old rebuild, nothing is silently dropped:
     the old FK pre-filters discarded orphan rows before load; now they
     load and are reported, and ``qa.py`` fails on them. ``--strict``
     makes a violation abort the rebuild (rolls the transaction back).

Every loaded table's row count is then compared with its file; any
difference exits non-zero.

Safety: the existing catalog (plus its ``-wal``/``-shm``/``-journal``
sidecars) and data directory are deleted before the build. The script
refuses to delete a data directory that holds anything other than
DuckLake-written files (``ducklake-*.parquet`` under schema/table dirs),
or a path that is the repo root, ``db/`` itself, ``db/parquet``,
``public/``, ``public/parquet``, the home directory or a filesystem root.
It also refuses, with no override, to pair a non-default catalog with the
default data directory ``db/lake/`` (that would delete the main lake's
files to build a scratch one), and, unless ``--force``, any data directory
other than the catalog's sibling ``lake/``. ``--force`` overrides those
last two content/sibling checks only.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = cod_db.ROOT


def _is_lake_data_dir(d: Path) -> tuple[bool, list[str]]:
    """True when every file under d is one DuckLake wrote."""
    foreign = [str(p.relative_to(d)) for p in d.rglob("*")
               if p.is_file() and not (p.name.startswith("ducklake-")
                                       and p.suffix == ".parquet")]
    return (not foreign, foreign[:5])


def clear_previous(catalog: Path, data: Path, force: bool) -> None:
    forbidden = {Path("/").resolve(), Path.home().resolve(), ROOT, ROOT / "db",
                 ROOT / "public", cod_db.PUBLIC_PARQUET, cod_db.LOCAL_PARQUET}
    if data.resolve() in forbidden or catalog.resolve() in forbidden:
        raise SystemExit(f"[error] refusing to use {data} / {catalog}: "
                         "protected path")
    default_cat = cod_db.DEFAULT_CATALOG.resolve()
    default_data = cod_db.DEFAULT_DATA.resolve()
    if catalog.resolve() != default_cat and data.resolve() == default_data:
        # No --force for this one: it would delete the main lake's data
        # files to build a scratch lake (a stale $COD_LAKE_DATA can do it).
        raise SystemExit(
            f"[error] refusing to build catalog {catalog} on the main lake's data "
            f"directory {data}. Pass --lake-data <dir> (or unset ${cod_db.ENV_DATA}).")
    if data.resolve() != (catalog.parent / "lake").resolve() and not force:
        raise SystemExit(
            f"[error] data directory {data} is not the catalog's sibling "
            f"{catalog.parent / 'lake'}; refusing to delete it. Check the paths "
            f"(and ${cod_db.ENV_DATA}), or pass --force.")
    if catalog.is_dir():
        raise SystemExit(f"[error] catalog path is a directory: {catalog}")
    if catalog.suffix != ".sqlite":
        raise SystemExit(f"[error] catalog must be a .sqlite file, got {catalog}")
    if data.exists():
        ok, foreign = _is_lake_data_dir(data)
        if not ok and not force:
            raise SystemExit(
                f"[error] {data} holds files DuckLake did not write (e.g. {foreign}); "
                "refusing to delete it. Check the path, or pass --force.")
        shutil.rmtree(data)
        print(f"[clean] removed data dir {data}")
    for p in [catalog] + [catalog.with_name(catalog.name + s)
                          for s in ("-wal", "-shm", "-journal")]:
        if p.exists():
            p.unlink()
            print(f"[clean] removed {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--parquet", type=Path, default=None,
                    help="source dir (default: db/parquet, the canonical committed copy)")
    ap.add_argument("--strict", action="store_true",
                    help="abort (roll back) if schema.sql constraints are violated")
    ap.add_argument("--force", action="store_true",
                    help="delete an existing data dir even if it holds non-DuckLake files "
                         "or is not the catalog's sibling lake/")
    args = ap.parse_args(argv)

    src = (args.parquet or cod_db.LOCAL_PARQUET).resolve()
    if not src.is_dir() or not any(src.glob("*.parquet")):
        print(f"[error] no parquet files in {src}", file=sys.stderr)
        return 2
    if src == cod_db.PUBLIC_PARQUET.resolve():
        print("[warn] building from public/parquet: the filtered publish copy "
              "(core-tier registry only, no db-only tables). Do not export from "
              "this lake to db/parquet.", file=sys.stderr)
    catalog = cod_db.catalog_path(args.catalog)
    data = cod_db.data_path(args.catalog, args.lake_data)
    print(f"[paths] parquet  {src}")
    print(f"[paths] catalog  {catalog}")
    print(f"[paths] data     {data}")

    t0 = time.time()
    sp = cod_db.spec()
    view_names = {v for v, _ in sp["views"]}
    spec_names = set(sp["tables"]) | view_names

    clear_previous(catalog, data, args.force)
    con = cod_db.connect(catalog=catalog, data=data, create=True)
    print(f"[connect] duckdb {duckdb.__version__}, DuckLake catalog created")

    loaded: dict[str, int] = {}
    derived: dict[str, int] = {}
    empty: list[str] = []
    skipped: list[str] = []
    violations: list[dict] = []
    shown = src.relative_to(ROOT) if src.is_relative_to(ROOT) else src
    msg = (f"rebuild_lake_from_parquet: {len(sp['tables'])} schema tables + "
           f"derived tables from {shown}")
    try:
        with cod_db.transaction(con, msg):
            # 2-3. schema tables, column-only, then data in file order.
            for t in sp["tables"]:
                con.execute(cod_db.column_only_ddl(t))
                f = src / f"{t}.parquet"
                if not f.exists():
                    empty.append(t)
                    continue
                try:
                    con.execute(
                        f"INSERT INTO {cod_db.qident(t)} BY NAME "
                        f"SELECT * EXCLUDE (file_row_number) "
                        f"FROM read_parquet(?, file_row_number = true) "
                        f"ORDER BY file_row_number", [str(f)])
                except duckdb.Error as e:
                    raise SystemExit(
                        f"[error] loading {t} from {f.name} failed: {e}\n"
                        "        (a parquet column schema.sql does not have, or a type "
                        "that does not cast). Fix schema.sql or the parquet.") from e
                loaded[t] = cod_db.scalar(con, f"SELECT count(*) FROM {cod_db.qident(t)}")

            # 4. parquet files that are not in the schema at all.
            for f in sorted(src.glob("*.parquet")):
                t = f.stem
                if t in spec_names:
                    if t in view_names:
                        skipped.append(t)
                    continue
                con.execute(
                    f"CREATE TABLE {cod_db.qident(t)} AS "
                    f"SELECT * EXCLUDE (file_row_number) "
                    f"FROM read_parquet(?, file_row_number = true) "
                    f"ORDER BY file_row_number", [str(f)])
                derived[t] = cod_db.scalar(con, f"SELECT count(*) FROM {cod_db.qident(t)}")

            # 5. views, in creation order, bodies exactly as the spec parsed them.
            for v, sql in sp["views"]:
                con.execute(sql)

            # 6. integrity.
            violations = cod_db.integrity_violations(con)
            if violations and args.strict:
                raise cod_db.IntegrityError(violations)
    except cod_db.IntegrityError as e:
        print("[integrity] FAILED (--strict), rebuild rolled back:\n" + str(e),
              file=sys.stderr)
        return 1

    # Row counts must equal the files: nothing is filtered any more.
    mismatch = []
    for t, n in {**loaded, **derived}.items():
        want = cod_db.scalar(con, "SELECT count(*) FROM read_parquet(?)",
                             [str(src / f"{t}.parquet")])
        if want != n:
            mismatch.append((t, n, want))

    for t, n in loaded.items():
        print(f"[load]   {t:<30} {n:>8} rows")
    for t, n in derived.items():
        print(f"[derive] {t:<30} {n:>8} rows")
    if empty:
        print(f"[empty]  schema tables with no parquet: {', '.join(empty)}")
    if skipped:
        print(f"[view]   parquet not loaded because the schema defines a view: "
              f"{', '.join(skipped)}")
    print(f"[views]  {len(sp['views'])} schema views created")

    n_cons = cod_db.constraint_count()
    if violations:
        print(f"[integrity] {n_cons} constraints checked; {len(violations)} VIOLATED "
              "(rows kept; qa.py will fail until fixed):")
        print("  " + cod_db.format_violations(violations).replace("\n", "\n  "))
    else:
        print(f"[integrity] {n_cons} constraints checked; 0 violated")

    snap = cod_db.latest_snapshot(con)
    con.close()
    print(f"\n[done] {len(loaded)} schema tables, {len(derived)} derived tables, "
          f"{len(sp['views'])} views in {time.time() - t0:.1f}s; "
          f"snapshot {snap.get('snapshot_id')} \"{snap.get('commit_message')}\"")
    if mismatch:
        print(f"[error] row counts differ from parquet: {mismatch}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
