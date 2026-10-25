#!/usr/bin/env python3
"""Precompute MVG (map visualization of a graph with group restrictions)
layouts for the COD knowledge map.

Implements KMap and PCL from Hossain, Moradi, Mondal & Kobourov, "Map
Visualizations for Graphs with Group Restrictions", Graphics Interface 2025
(see scripts/mvg.py), and writes four derived tables into the lake (one
snapshot; scripts/export_parquet.py publishes them to db/parquet/ and
public/parquet/):

  mvg_node_layout          per-node KMap and PCL coordinates
  mvg_area_polygons        per-area polygon WKT + area/size shares
  scholar_area_assignments scholar -> research area (+confidence)
  mvg_layout_metrics       M1-M7 quality metrics for both methods

The front end recomputes a KMap-equivalent layout in src/views/network.js at
page load; these tables let it read a precomputed PCL layout instead
(network.js Phase 3, "PCL refinement", is unbuilt).

Node population (713 nodes / 5,648 edges / 21 areas):
  site personnel   people with an area in person_primary_groups
  COD leadership   cod_team_members (domain role, else operations)
  scholars         community_scholars joined via person_registry

Edges: internal co-authorship (authorship), registry collaborations
(registry_collaborations), and scholar<->COD bridge edges.

Inputs are read from the lake by default, so a layout run straight after
compute_primary_groups.py sees its output without an export in between.
The lake holds the whole person_registry (archive tier included), which is
what db/parquet -- this script's old input -- holds too. With --parquet-dir
the inputs are read from a parquet catalog instead; the outputs always go
to the lake. The layout is deterministic for a given --seed (default 7):
every input query is ordered and mvg.py seeds every random draw.

Usage:
  python scripts/build_mvg_layout.py                  # lake -> lake
  python scripts/build_mvg_layout.py --k-ext 12       # sparser graph
  python scripts/build_mvg_layout.py --parquet-dir db/parquet   # parquet -> lake

PROVENANCE WARNING. Research areas for community scholars are INFERRED, not
curated: no scholar->area table exists in the catalog, and the shared_areas
field on registry_collaborations covers only 9 scholars. Each scholar's area
is a weighted vote over their community_scholars.top_topics phrases mapped
onto research_areas, via the cached map in
db/derived/topic_area_map.json. Mean confidence is 0.58 and 125 of 442 rows
fall below 0.4. The confidence column ships with the table -- treat low
values as provisional pending curator review, and do not present them in the
UI as curated assignments.
"""
import argparse
import json
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cod_db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS_AREA = "Project leadership & operations"


def _rollup(area_id, parent):
    seen = set()
    while True:
        p = parent.get(area_id)
        if p is None or (isinstance(p, float) and p != p) or not p or p in seen:
            return area_id
        seen.add(area_id)
        area_id = p


def load_graph(con, topic_map_path, parquet_dir=None):
    """Assemble the combined node/edge/label lists from the lake tables (or,
    with ``parquet_dir``, from the parquet catalog there, read through the
    same connection). Every query is ordered, so the node and edge lists --
    and with them the seeded layout -- do not depend on the order a lake
    happens to return rows in."""
    import pandas as pd
    if parquet_dir is None:
        P = lambda t: cod_db.qident(t)
    else:
        P = lambda t: "'" + os.path.join(parquet_dir, t + ".parquet").replace("'", "''") + "'"
    q = lambda s: con.execute(s).fetchdf()

    areas = q(f"select area_id, label, parent_id from {P('research_areas')} order by area_id")
    lab2id = dict(zip(areas.label, areas.area_id))
    id2lab = dict(zip(areas.area_id, areas.label))
    parent = dict(zip(areas.area_id, areas.parent_id))

    people = q(f"select person_id, name from {P('people')} order by person_id")
    names = dict(zip(people.person_id, people.name))
    pg = q(f"""select person_id, primary_area_label from {P('person_primary_groups')}
               where primary_area_label is not null order by person_id""")
    pg["area"] = [id2lab.get(_rollup(lab2id.get(l), parent))
                  for l in pg.primary_area_label]

    # Primary-key order (what export_parquet.py writes, and what the committed
    # file held). It matters: the rows with a NULL person_id reach pandas as
    # NaN, which is truthy, so the first of them becomes one 't:nan' node
    # and its display_name depends on which row comes first.
    tm = q(f"""select person_id, display_name, role from {P('cod_team_members')}
               order by member_id, wbs_code, role""")
    team_ids = set(tm.person_id.dropna())

    with open(topic_map_path) as fh:
        topic2area = json.load(fh)
    cs = q(f"""select c.scholar_id, c.name, c.top_topics, r.canonical_id, r.h_index
               from {P('community_scholars')} c
               join {P('person_registry')} r on c.scholar_id = r.scholar_id
               order by r.canonical_id, c.scholar_id""")
    cs = cs.drop_duplicates("canonical_id")

    def vote(tt):
        if not isinstance(tt, str):
            return None, 0.0
        picks = [topic2area.get(p.strip()) for p in tt.split(";")]
        picks = [p for p in picks if p]
        if not picks:
            return None, 0.0
        top, n = collections.Counter(picks).most_common(1)[0]
        return top, n / len([p for p in tt.split(";") if p.strip()])

    cs[["area", "conf"]] = cs.top_topics.apply(lambda t: pd.Series(vote(t)))

    role_map = {}
    rm_path = os.path.join(os.path.dirname(topic_map_path), "leadership_area_map.json")
    if os.path.exists(rm_path):
        with open(rm_path) as fh:
            role_map = json.load(fh)

    recs = []
    for pid, area in zip(pg.person_id, pg.area):
        recs.append({"uid": f"p:{pid}", "name": names.get(pid, ""), "area": area,
                     "cohort": "leadership" if pid in team_ids else "personnel",
                     "src": pid})
    have = {r["src"] for r in recs}
    for m in tm.itertuples(index=False):
        if m.person_id in have or not m.person_id:
            continue
        recs.append({"uid": f"t:{m.person_id}", "name": m.display_name,
                     "area": role_map.get(m.display_name) or OPS_AREA,
                     "cohort": "leadership", "src": m.person_id})
        have.add(m.person_id)
    for s in cs.itertuples(index=False):
        if s.area:
            recs.append({"uid": f"s:{s.canonical_id}", "name": s.name,
                         "area": s.area, "cohort": "scholar", "src": s.canonical_id})
    N = pd.DataFrame(recs).drop_duplicates("uid")
    N = N[N.area.notna()]

    uid_person = {r.src: r.uid for r in N.itertuples(index=False)
                  if r.uid.startswith(("p:", "t:"))}
    uid_canon = {r.src: r.uid for r in N.itertuples(index=False)
                 if r.uid.startswith("s:")}
    coauth = q(f"""with a as (select person_id, publication_id from {P('authorship')}
                              where person_id is not null)
                   select x.person_id pa, y.person_id pb from a x join a y
                    on x.publication_id=y.publication_id and x.person_id<y.person_id
                   group by 1,2 order by 1,2""")
    rc = q(f"""select canonical_id_a a, canonical_id_b b
               from {P('registry_collaborations')} order by 1, 2""")
    reg = q(f"""select canonical_id, person_id from {P('person_registry')}
                where person_id is not null order by canonical_id""")
    canon2person = dict(zip(reg.canonical_id, reg.person_id))

    E = set()
    for a, b in coauth.itertuples(index=False):
        if a in uid_person and b in uid_person:
            E.add(tuple(sorted((uid_person[a], uid_person[b]))))
    for a, b in rc.itertuples(index=False):
        ua, ub = uid_canon.get(a), uid_canon.get(b)
        if ua and ub and ua != ub:
            E.add(tuple(sorted((ua, ub))))
        for x, y in ((a, b), (b, a)):
            if x in uid_canon and canon2person.get(y) in uid_person:
                e = tuple(sorted((uid_canon[x], uid_person[canon2person[y]])))
                if e[0] != e[1]:
                    E.add(e)

    uids = sorted(N.uid)
    iu = {u: i for i, u in enumerate(uids)}
    labels = [dict(zip(N.uid, N.area))[u] for u in uids]
    edges = sorted({(iu[a], iu[b]) for a, b in E if a in iu and b in iu and iu[a] != iu[b]})
    return N, uids, labels, edges, cs


# Column types of the four output tables, as committed in db/parquet.
NODE_LAYOUT_TYPES = {
    "node_uid": "VARCHAR", "name": "VARCHAR", "source_id": "VARCHAR",
    "cohort": "VARCHAR", "area_label": "VARCHAR", "degree": "BIGINT",
    "connected": "BOOLEAN", "kmap_x": "DOUBLE", "kmap_y": "DOUBLE",
    "pcl_x": "DOUBLE", "pcl_y": "DOUBLE",
}
AREA_POLYGON_TYPES = {
    "research_area": "VARCHAR", "n_nodes": "BIGINT", "n_leadership": "BIGINT",
    "n_personnel": "BIGINT", "n_scholar": "BIGINT", "polygon_area": "DOUBLE",
    "area_share": "DOUBLE", "size_share": "DOUBLE", "wkt": "VARCHAR",
}
SCHOLAR_TYPES = {
    "canonical_id": "VARCHAR", "name": "VARCHAR", "assigned_area": "VARCHAR",
    "topic_vote_confidence": "DOUBLE", "h_index": "INTEGER",
}
METRICS_TYPES = {
    "method": "VARCHAR", "nodes": "BIGINT", "edges": "BIGINT", "groups": "BIGINT",
    "M1": "DOUBLE", "M2": "DOUBLE", "M3": "DOUBLE", "M4": "DOUBLE",
    "M5": "DOUBLE", "M6": "DOUBLE", "M7": "DOUBLE",
    "within_crossings": "BIGINT", "between_crossings": "BIGINT",
    "total_crossings": "BIGINT",
}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet-dir", default=None,
                    help="read inputs from this parquet catalog instead of "
                         "the lake (outputs still go to the lake)")
    ap.add_argument("--out-dir", action="append", default=None,
                    help="removed: the tables are written to the lake; run "
                         "scripts/export_parquet.py to publish them")
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=os.path.abspath,
                    help="deprecated alias of --catalog")
    ap.add_argument("--topic-map",
                    default=os.path.join(ROOT, "db", "derived", "topic_area_map.json"))
    ap.add_argument("--k-ext", type=float, default=40.0,
                    help="PCL external gravity. TUNE PER GRAPH: the optimum "
                         "tracks the between-group edge fraction (12 at 54%% "
                         "between-group, 40 at 68%%).")
    ap.add_argument("--iterations", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    if args.out_dir:
        print("[error] --out-dir was removed: build_mvg_layout.py writes its "
              "tables to the lake; run scripts/export_parquet.py to publish "
              "them", file=sys.stderr)
        return 2

    import numpy as np
    import pandas as pd
    import mvg

    # One writer connection: the read happens before the long layout
    # computation and the write is one short transaction after it.
    lake = cod_db.connect(catalog=args.catalog, data=args.lake_data)
    N, uids, labels, edges, cs = load_graph(lake, args.topic_map, args.parquet_dir)
    print(f"graph: {len(uids)} nodes, {len(edges)} edges, {len(set(labels))} areas")

    km = mvg.mvg_kmap(labels, edges, seed=args.seed)
    pcl = mvg.mvg_pcl(km["points"], labels, edges, km["polygons"],
                      iterations=args.iterations, k_ext=args.k_ext, seed=args.seed)

    deg = np.zeros(len(uids))
    for u, v in edges:
        deg[u] += 1
        deg[v] += 1
    name_of = dict(zip(N.uid, N.name))
    src_of = dict(zip(N.uid, N.src))
    coh_of = dict(zip(N.uid, N.cohort))
    layout = pd.DataFrame({
        "node_uid": uids, "name": [name_of[u] for u in uids],
        "source_id": [src_of[u] for u in uids], "cohort": [coh_of[u] for u in uids],
        "area_label": labels, "degree": deg.astype(int), "connected": deg > 0,
        "kmap_x": km["points"][:, 0], "kmap_y": km["points"][:, 1],
        "pcl_x": pcl[:, 0], "pcl_y": pcl[:, 1],
    })

    sizes = pd.Series(labels).value_counts()
    total = sum(p.area for p in km["polygons"].values())
    polys = pd.DataFrame([{
        "research_area": g, "n_nodes": int(sizes[g]),
        "n_leadership": int(sum(1 for i, l in enumerate(labels)
                                if l == g and coh_of[uids[i]] == "leadership")),
        "n_personnel": int(sum(1 for i, l in enumerate(labels)
                               if l == g and coh_of[uids[i]] == "personnel")),
        "n_scholar": int(sum(1 for i, l in enumerate(labels)
                             if l == g and coh_of[uids[i]] == "scholar")),
        "polygon_area": round(km["polygons"][g].area, 6),
        "area_share": round(km["polygons"][g].area / total, 4),
        "size_share": round(sizes[g] / len(labels), 4),
        "wkt": km["polygons"][g].wkt,
    } for g in sorted(km["polygons"], key=str)])

    within = [(u, v) for u, v in edges if labels[u] == labels[v]]
    betw = [(u, v) for u, v in edges if labels[u] != labels[v]]
    rows = []
    for nm, pts in [("KMap", km["points"]), (f"PCL (k_ext={args.k_ext:g})", pcl)]:
        m = mvg.mvg_metrics(pts, labels, edges, km["polygons"])
        wc = mvg.mvg_crossing_fraction(pts, within) * len(within) ** 2
        bc = mvg.mvg_crossing_fraction(pts, betw) * len(betw) ** 2
        rows.append({"method": nm, "nodes": len(labels), "edges": len(edges),
                     "groups": len(sizes), **{k: round(m[k], 4) for k in mvg.MVG_METRICS},
                     "within_crossings": int(wc), "between_crossings": int(bc),
                     "total_crossings": int(wc + bc)})
        print(nm, {k: round(m[k], 3) for k in mvg.MVG_METRICS})
    metrics = pd.DataFrame(rows)

    sch = cs[["canonical_id", "name", "area", "conf", "h_index"]].copy()
    sch.columns = ["canonical_id", "name", "assigned_area",
                   "topic_vote_confidence", "h_index"]

    # Each table is cast column by column to the types the committed parquet
    # has always had (pandas dtypes vary with NULLs and pandas versions), so a
    # computed lake exports the same bytes as one rebuilt from parquet.
    tables = [("mvg_node_layout", layout, NODE_LAYOUT_TYPES),
              ("mvg_area_polygons", polys, AREA_POLYGON_TYPES),
              ("scholar_area_assignments", sch, SCHOLAR_TYPES),
              ("mvg_layout_metrics", metrics, METRICS_TYPES)]
    with cod_db.transaction(lake, f"build_mvg_layout: {len(uids)} nodes, "
                                  f"{len(edges)} edges, k_ext={args.k_ext:g}, "
                                  f"seed={args.seed}"):
        for name, df, types in tables:
            if list(df.columns) != list(types):
                raise RuntimeError(f"{name}: columns {list(df.columns)} != "
                                   f"expected {list(types)}")
            cols = ", ".join(f"CAST({cod_db.qident(c)} AS {t}) AS {cod_db.qident(c)}"
                             for c, t in types.items())
            lake.register("_mvg_df", df)
            try:
                n = cod_db.replace_table(lake, name, f"SELECT {cols} FROM _mvg_df")
            finally:
                lake.unregister("_mvg_df")
            print(f"[lake] {name}: {n} rows")
        cod_db.assert_integrity(lake, ["mvg_node_layout", "mvg_area_polygons",
                                       "scholar_area_assignments", "mvg_layout_metrics"])
    print("wrote layout tables -> lake (run scripts/export_parquet.py to publish)")
    lake.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
