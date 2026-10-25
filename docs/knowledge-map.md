# Knowledge map

The **Network** tab (`#/network`) draws the coastal research catalogue as a map of research areas. Each polygon is one research area. Inside it, smaller cells are the institutions (facilities) that work in that area, and dots are the researchers attached to them. This page describes how big each region is, what the first view shows, and when names appear. The code is `src/views/network.js`.

As of 2026-10-25 the shipped Parquet gives the map 21 regions: the active research areas that are their own polygon (35, after five small areas fold into their parents) and carry any weight. The other 14 carry none and are left out. The map holds 213 institutions and 243 researchers. Which region each institution and researcher belongs to comes from `facility_primary_groups` and `person_primary_groups`.

## What sizes a region

A region's area is proportional to a **data-and-people weight**:

```
weight = organisations
       + 3.0  × data providers
       + 1.5  × datasets
       + 0.35 × researchers anchored in the region
```

- **Organisations** are the region's facilities, not counting protected areas.
- **Data providers** are facilities with at least one catalogued dataset in `dataset_facilities`. As of 2026-10-25, 24 facilities are data providers, linked to 39 of the 72 datasets on the **Data** tab. A data provider's dot on the map is drawn larger than other facilities' dots.
- **Datasets** counts the datasets each facility in the region produces. A dataset linked to two facilities in the same region counts twice.
- **Researchers anchored in the region** sums three groups. The first is site staff (`facility_personnel`). The second is registry researchers linked to a site in the region (`registry_facilities`). The third is registry researchers with no site link whose main research area is this region.

The coefficients are constants (`W_DATA_PROVIDER`, `W_DATASET`, `W_PERSON`) in `network.js`, so the ranking can be audited.

The weight replaces the raw facility count because of protected areas. Of the 3,532 catalogued facilities, 3,309 are protected areas (`facility_type LIKE 'protected-area%'`). All of them fall in *Coastal terrestrial ecosystems*, and none has a dataset or a researcher. Sized by raw count, that one region took about 94% of the map. Under the weight it is 10.05, from its 9 other organisations and 3 researchers.

**Protected areas play no part in the map's layout.** They add nothing to any region's weight, get no cell or dot, and are left out of the facilities the map lays out. They are still in the catalogue and in the SQL tab. The off-by-default **Protected areas (context)** switch adds one summary chip per region, giving its protected-area count ("▤ 3309 protected"). The chip does not change the layout.

The sidebar lists regions by the same weight. Its three numbers are organisations · datasets · people. Hover a row for the full breakdown, including the data-provider count and how many protected areas were excluded.

## Layout

The layout follows KMap (Hossain, Moradi, Mondal & Kobourov, *Map Visualizations for Graphs with Group Restrictions*, Graphics Interface 2025):

1. **Supergraph.** Each research area becomes a square whose side is 34 × √weight. A force layout places the squares, pulling areas that share facilities or co-authors closer together.
2. **Subgraph.** Inside each square, a small force layout places the area's institutions and researchers.
3. **Polygons.** A Voronoi diagram is computed over all the nodes, and each area's cells are merged into one polygon. Institutions get their own sub-polygons inside it.

The layout is shaped to the stage. On a wide screen the squares are pulled toward the centre harder vertically than sideways, so the map comes out about as wide as the stage instead of round. The layout is computed once, when the tab first opens. Resizing the window refits it but does not recompute it. **Recompute layout** runs it again for the current stage shape. Nothing in the layout is random: its jitter is seeded from the area ids, and d3-force seeds its own. The same data on the same stage shape therefore gives the same map.

## Viewport

- **First view: the whole map.** The map opens framed so that every polygon fits on the stage with a 24 px margin. Nothing is cropped.
- **Zoom limits are relative to that first view.** Zooming out stops at the first view. Zooming in goes to 24×, or further if the stage needs it, up to 200×. The limit is raised to the zoom that frames the smallest region in the stage. On a large monitor 24× is usually enough, but on a phone it can leave the smallest region well short of the stage width. Because the limits are tied to the fit, "4×" means the same on a phone and on a large monitor.
- **The map cannot be lost off-screen.** You can pan at most half the map's size past any edge.
- **Resizing.** If you have not zoomed or panned, a resize refits the whole map. If you have, the point at the centre of the stage stays there and the zoom level is kept. Labels are recomputed either way.
- **Going to a region.** Clicking a region, or its row in the sidebar, zooms to fit that region with a 48 px margin. The region is then **pinned**: its outline is heavier, its fill darker, its sidebar row marked, and its name always shown.
- **Back to everything.** **Reset zoom**, or a double-click on the map, returns to the first view and unpins the region.

## When names appear

Names are not tied to a zoom level. A name shows when the thing it names is large enough **on screen** to hold it, a test that gives the same result on every display. Every label is drawn at a fixed screen size, so zooming moves a label but does not scale its text. No label is drawn smaller than 9.8 px, the smallest text the site's tooltips already use. A label that does not fit at its size is hidden, not shrunk.

| Label | Shown when | Size |
|---|---|---|
| Region name | The whole name lies inside the polygon: its top, middle and bottom rows are each inside the outline. The map tries one line first, then two lines, then a smaller 11 px size. The three heaviest regions also get a 10 px try. If none fits, the name is hidden; the sidebar and the tooltip still give it. | 12–18 px, larger for heavier regions |
| Region name, pinned | Always. | as above |
| Region name, "inside" | When the polygon covers ¾ or more of the stage, you are inside the region and its pole is usually off-screen. The name then moves to the topmost row of the stage where it fits inside the outline and clears the other labels. A region covering a fifth of the stage or more gets the same placement if no spot near its pole works. | as above |
| Institution | Its sub-polygon is at least 40 px across in both directions, and at least 0.8× the width of its name. | 10.5–14 px |
| Researcher | Their institution's sub-polygon is at least 120 px across. A researcher with no institution drawn in that region needs the region itself to be 480 px across. At most 14 researchers per region are eligible, in order of importance (see below). | 10–13.5 px |
| Registry researcher | Only in the *Researchers & co-authorship* layer, for the site you shift-clicked: that site's sub-polygon is at least 120 px across. | 10–12.5 px |
| Protected-area chips, "N no site" captions | The region is at least 60 px across. | 10–12 px |

### Labels never overlap

After deciding which labels are eligible, the map places them one at a time in priority order:

1. the pinned region
2. other regions, heaviest first
3. context chips and captions
4. institutions (ranked as described under *Institution label order*)
5. registry researchers
6. researchers

A label is skipped if its box, with a 3 px pad, would touch a label already placed or run off the stage. This is a single pass across all label kinds, done in screen pixels, so a region name always wins over the institution and researcher labels beneath it.

The pass runs at most once per animation frame while you zoom or pan, and once more when the gesture ends. It reads cached text widths and checks collisions against a grid, so it does not force the browser to re-measure the page.

### Label text and placement

- **Region names** start at the polygon's *pole of inaccessibility*: the interior point farthest from the edge. On an irregular shape the average of the vertices can fall near a boundary; the pole cannot. If the name does not fit at the pole, the map slides it sideways within the outline, then tries positions a little above and below. Region names carry a heavier halo than other labels, because they sit over cells and dots.
- **Region names keep off institutions.** Once a region is 150 px or more across on screen, its name avoids its institutions' dots. It always avoids the space above the dot of any institution large enough to be named. Without this rule, a region holding a single institution put its own name on that institution's dot (the two share a centre) and hid the institution's name at every zoom. If no clear position exists, the name goes in the first position that fits.
- **A large region partly off the stage is still named.** When the pole is outside the stage, or within 40 px of its edge, the map also tries the centre of the region's on-stage part, and the pole moved into that part.
- **Institution label order.** Institutions compete for space by the number of people there plus 1.5 × the datasets they produce. A data provider is therefore named ahead of a similarly staffed site that ships no data.
- **Researcher importance** ranks researchers for naming and sets the size of their dot and name. It combines three numbers: their publication count, their number of co-authors (both from `person_area_metrics`), and the recorded funding of the institutions they work at (`facility_area_funding`). The tooltip shows whichever of the three are non-zero. As of 2026-10-25, of the 243 researchers on the map, 164 have publications, 77 have co-authors and 70 work at an institution with recorded funding. Treat importance as a rough ordering.
- **Institution names.** An acronym of up to 8 characters is used as the name only when no other institution on the map has the same one. Network acronyms that many sites share are replaced by the place name. On the shipped Parquet those are NERR (29 sites), NEP (19), LTER (11) and LTREB (4). For example, "Wells NERR" is labelled **Wells**, not **NERR**. Names sit just above their dot, so they do not cover it. The tooltip always gives the full name.

## Researchers & co-authorship

This off-by-default layer draws the core tier of the person registry (`person_registry`, 10,095 rows as of 2026-10-25). It loads only when switched on, because it roughly doubles the view's start-up download. A registry researcher is placed in one of two ways, and the map keeps the two visibly apart:

- **At a site.** 263 researchers have a ROR-matched link to one of 26 catalogued sites (`registry_facilities`). They are drawn as solid markers around that site. A marker's size follows the researcher's **coastal output volume** (`coastal_works_count`). That count is an upper bound on their coastal papers, because OpenAlex lists a work under every topic it carries. The map uses it only as a size cue and never calls it a paper count.
- **In a region, with no site.** 225 researchers have no site link but have a main research area. Each region shows them as one dashed "N no site" badge, not as individual markers. Placement in a region is a claim about their science, not about where they work.

The remaining 9,607 have neither and are not drawn. The status line states all three counts from the data. Amber ribbons are co-publication between researchers at two different catalogued sites, summed from `registry_collaborations`.

## Controls

| Control | Effect |
|---|---|
| **Facilities** / **People** | Show or hide institution and researcher marks. These switches also decide which cross-area edges a click can reveal. |
| **Researchers & co-authorship** | Turns on the researcher registry layer described above. Shift-clicking a site opens its roster. Clicking a dashed "no site" badge lists that region's researchers. Clicking a registry researcher opens their People card if they have one, otherwise their ORCID or OpenAlex profile. |
| **Protected areas (context)** | One summary chip per region for the protected areas catalogued there. The layout does not change. |
| **Recompute layout** | Runs the layout again for the current stage. |
| Click a region or a sidebar row | Zooms to the region and pins it. On a phone, tapping a sidebar row also folds the list and scrolls the map into view. |
| Click an institution or researcher | Reveals that node's cross-area edges. Click it again, click the background, or press **Esc** to hide them. |
| Shift-click | On an institution, opens its homepage, or its researcher roster when *Researchers & co-authorship* is on. On a researcher, opens their card on the **People** tab. |
| **Reset zoom** / double-click | Shows the whole map again. |

On a phone (760 px wide or less), the layout changes to fit:

- The subtitle is hidden and the toggles wrap.
- The status line shows two lines; tap it to see the rest.
- The map gets 65% of the screen height.
- The sidebar moves below the map and collapses to a **Research areas** heading you can tap to open.

## Loading

The map needs the in-browser database (DuckDB-Wasm). On a slow first load, `src/db.js` can stop waiting before the database has actually started. The map does not give up at that point. It shows **Waiting for the database to start…** and checks again every half second for up to three minutes, then draws itself. If the database still has not started after three minutes, the status line says so. **Recompute layout** tries again without reloading the page.

## Not implemented

The browser does not implement the KMap paper's PCL boundary refinement, custom force terms, or canvas rendering in a Web Worker. `docs/map_visualization_plan.md` describes them.

`scripts/build_mvg_layout.py` computes both a KMap and a PCL layout offline and writes four tables to `public/parquet/`:

- `mvg_node_layout`: per-node KMap and PCL coordinates for 713 nodes.
- `mvg_area_polygons`: 21 area polygons.
- `mvg_layout_metrics`: the M1–M7 quality metrics for both methods.
- `scholar_area_assignments`: 442 inferred scholar-to-area assignments, each with a confidence value.

That offline graph is a different population from the Network tab's: it includes the COD team and the community-scholar cohort. The Network tab does not read these tables. It computes its own layout. They are published Parquet, so any DuckDB client can query them over HTTP, but `src/db.js` does not register them, and the in-app SQL tab cannot see them.
