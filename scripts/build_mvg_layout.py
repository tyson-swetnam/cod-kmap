#!/usr/bin/env python3
"""Precompute MVG (map visualization of a graph with group restrictions)
layouts for the COD knowledge map.

Implements KMap and PCL from Hossain, Moradi, Mondal & Kobourov, "Map
Visualizations for Graphs with Group Restrictions", Graphics Interface 2025
(see scripts/mvg.py), and writes four tables to db/parquet/ and
public/parquet/:

  mvg_node_layout.parquet          per-node KMap and PCL coordinates
  mvg_area_polygons.parquet        per-area polygon WKT + area/size shares
  scholar_area_assignments.parquet scholar -> research area (+confidence)
  mvg_layout_metrics.parquet       M1-M7 quality metrics for both methods

The front end recomputes a KMap-equivalent layout in src/views/network.js at
page load; these tables let it read a precomputed PCL layout instead
(network.js Phase 3, "PCL refinement", is unbuilt).

Node population (713 nodes / 5,648 edges / 21 areas):
  site personnel   people.parquet with an area in person_primary_groups
  COD leadership   cod_team_members.parquet (domain role, else operations)
  scholars         community_scholars.parquet joined via person_registry

Edges: internal co-authorship (authorship.parquet), registry collaborations
(registry_collaborations.parquet), and scholar<->COD bridge edges.

Usage:
  python scripts/build_mvg_layout.py                  # both output dirs
  python scripts/build_mvg_layout.py --k-ext 12       # sparser graph

PROVENANCE WARNING. Research areas for community scholars are INFERRED, not
curated: no scholar->area table exists in the catalog, and the shared_areas
field on registry_collaborations covers only 9 scholars. Each scholar's area
is a weighted vote over their community_scholars.top_topics phrases mapped
onto research_areas.parquet, via the cached map in
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

OPS_AREA = "Project leadership & operations"


def _rollup(area_id, parent):
    seen = set()
    while True:
        p = parent.get(area_id)
        if p is None or (isinstance(p, float) and p != p) or not p or p in seen:
            return area_id
        seen.add(area_id)
        area_id = p


def load_graph(parquet_dir, topic_map_path):
    """Assemble the combined node/edge/label lists from the parquet catalog."""
    import duckdb
    import pandas as pd
    con = duckdb.connect()
    P = lambda t: os.path.join(parquet_dir, t + ".parquet")
    q = lambda s: con.execute(s).fetchdf()

    areas = q(f"select area_id, label, parent_id from '{P('research_areas')}'")
    lab2id = dict(zip(areas.label, areas.area_id))
    id2lab = dict(zip(areas.area_id, areas.label))
    parent = dict(zip(areas.area_id, areas.parent_id))

    people = q(f"select person_id, name from '{P('people')}'")
    names = dict(zip(people.person_id, people.name))
    pg = q(f"""select person_id, primary_area_label from '{P('person_primary_groups')}'
               where primary_area_label is not null""")
    pg["area"] = [id2lab.get(_rollup(lab2id.get(l), parent))
                  for l in pg.primary_area_label]

    tm = q(f"select person_id, display_name, role from '{P('cod_team_members')}'")
    team_ids = set(tm.person_id.dropna())

    with open(topic_map_path) as fh:
        topic2area = json.load(fh)
    cs = q(f"""select c.scholar_id, c.name, c.top_topics, r.canonical_id, r.h_index
               from '{P('community_scholars')}' c
               join '{P('person_registry')}' r on c.scholar_id = r.scholar_id""")
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
    coauth = q(f"""with a as (select person_id, publication_id from '{P('authorship')}'
                              where person_id is not null)
                   select x.person_id pa, y.person_id pb from a x join a y
                    on x.publication_id=y.publication_id and x.person_id<y.person_id
                   group by 1,2""")
    rc = q(f"""select canonical_id_a a, canonical_id_b b from '{P('registry_collaborations')}'""")
    reg = q(f"""select canonical_id, person_id from '{P('person_registry')}'
                where person_id is not null""")
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parquet-dir", default="db/parquet")
    ap.add_argument("--out-dir", action="append", default=None)
    ap.add_argument("--topic-map", default="db/derived/topic_area_map.json")
    ap.add_argument("--k-ext", type=float, default=40.0,
                    help="PCL external gravity. TUNE PER GRAPH: the optimum "
                         "tracks the between-group edge fraction (12 at 54%% "
                         "between-group, 40 at 68%%).")
    ap.add_argument("--iterations", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    out_dirs = args.out_dir or ["db/parquet", "public/parquet"]

    import numpy as np
    import pandas as pd
    import mvg

    N, uids, labels, edges, cs = load_graph(args.parquet_dir, args.topic_map)
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

    for d in out_dirs:
        os.makedirs(d, exist_ok=True)
        layout.to_parquet(os.path.join(d, "mvg_node_layout.parquet"), index=False)
        polys.to_parquet(os.path.join(d, "mvg_area_polygons.parquet"), index=False)
        sch.to_parquet(os.path.join(d, "scholar_area_assignments.parquet"), index=False)
        metrics.to_parquet(os.path.join(d, "mvg_layout_metrics.parquet"), index=False)
        print("wrote layout tables ->", os.path.abspath(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
