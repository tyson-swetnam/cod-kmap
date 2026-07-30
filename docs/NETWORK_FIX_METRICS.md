# network_fix_metrics.csv

M1-M7 (Hossain, Moradi, Mondal & Kobourov, GI'25 S5.3) for the COD knowledge
map before and after 59 researchers move out of facility circles into
interstitial space. Real graph: 443 nodes, 289 edges (209 within-group, 80
between-group), 21 groups.

## The M7 crossing decomposition

M7 = 1 - (crossing edge PAIRS) / |E|^2. Every pair of edges is classified by
whether each edge is within-group or between-group, so the three counts sum to
the total and reproduce M7 exactly:

| component | before | after | delta | share of increase |
|---|---:|---:|---:|---:|
| within x within | 0 | 2 | +2 | 2% |
| within x between | 22 | 131 | +109 | 86% |
| between x between | 672 | 688 | +16 | 13% |
| **total** | **694** | **821** | **+127** | |

M7 0.9917 -> 0.9902.

**Correction.** Earlier versions of this table, the commit message for d4218ad,
and my report to the user all stated that the M7 loss was "within-group only"
with "between-group crossings unchanged". That was wrong twice over:

* the fraction of between-group edges involved in a crossing ROSE, 0.10500 ->
  0.10750 -- 50x the within-group change in absolute terms;
* the dominant term is within x between (86% of the increase). The two
  single-subset fractions I originally reported never summed to the M7 change
  at all, because cross-subset pairs were not measured.

Moving nodes toward polygon boundaries puts them nearer the between-group
lines that leave the polygon, so those lines now cross more intra-area edges.
That is the cost of the fill improvement (M6 over groups with >=3 nodes:
0.0916 -> 0.1644); it is not free, and it lands partly on exactly
the between-group structure the map is read for.

## Fidelity caveat

This is a MODEL of the change, not the shipped layout. Polygons come from
`mvg_kmap` on the real graph, NOT from network.js's own layoutSupergraph +
computePolygons (which need d3's force simulation and a browser). The 59 moved
nodes are displaced outward toward their polygon boundary to approximate
`interstitialSlots()`; per-area move COUNTS are exact, individual coordinates
are not. Direction and rough magnitude are evidence; absolute values are not
what the site will produce.
