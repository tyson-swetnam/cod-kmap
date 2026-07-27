# Bibliometric enrichment of the COD scholar roster

**Run date:** 2026-07-26 · **Repo:** cod-kmap at `8732124` (main, PR #11) · **Gate:** `scripts/qa.py` exits 0 on a clean rebuild from committed parquet.

This is the run that `HANDOFF.md` §3 described as outstanding. The build environment
that produced the three data layers returned HTTP 403 for `api.openalex.org` and
`pub.orcid.org`, so every metric column shipped empty. Both hosts were reachable here.

---

## 1. What changed

| Table | Before | After |
|---|---:|---:|
| `people` with ORCID | 41 | 45 |
| `people` with `openalex_id` | 168 | 171 |
| `publications` | 10,824 | 12,505 |
| `authorship` | 8,079 | 8,772 |
| `publication_topics` | 246,571 | 356,201 |
| `person_areas` | 1,010 | 1,065 |
| `person_area_metrics` (h-index, citations, composite z) | 1,881 all-null | 2,704 fully populated |
| `collaborations` | 103 | 116 |
| `community_scholars` | 339 (0 measured) | 523 (220 measured, 303 curated-only) |

ORCID and OpenAlex counts moved less than the raw resolution rate suggests, because
resolution ran alongside a cleanup that removed more bad identifiers than it added
good ones. That is covered in §3.

---

## 2. The measured roster

220 scholars carry harvested metrics; the cohort split is exactly the
100 / 100 / 50 the QA gate requires (30 scholars hold both the pre-eminent and
most-active designations, so the distinct count is 220).

- **Median h-index 72**, median 20,824 citations.
- **Median coastal share 49%** — half of a typical roster member's
  output falls under the 15-topic coastal set. The 15% selection gate binds only at the
  left tail, so the roster is not an artifact of a permissive threshold.
- **32 countries.** US institutions hold 29%; China (30), Australia (20),
  Germany (16) and the UK (15) follow.
- Median h-index by cohort: 95 (pre-eminent + active), 87 (pre-eminent), 62 (most active),
  **9 (rising)** — the rising cohort is a genuinely different career stage, not a
  re-ranking of the same people.

**Highest-ranked pre-eminent scholars:** Carlos M. Duarte (h=178, 150,789 citations,
806 coastal works), Stephen R. Carpenter (h=145), Marten Scheffer (h=133),
Scott C. Doney (h=129), Paul G. Falkowski (h=128).

**Top rising scholars:** Albert Pessarrodona (first published 2016, 2-year mean
citedness 13.5), Jiangfeng Xian (2018, 12.6), Alberto Meucci (2018, 6.8).

**Strongest collaboration edges** within the roster: Ben Halpern–Fiorenza Micheli
(15 co-publications, 2004–2023), Robert W. Howarth–Roxanne M. Marino (12, 1988–2021),
Thomas J. Mozdzer–James Patrick Megonigal (11, 2011–2024).

---

## 3. Defects found and fixed

Four defects blocked or corrupted the chain. Each is fixed in the code, not worked
around in the data.

**3.1 OpenAlex authentication.** All six scripts that call the API authenticated with
the `mailto=` polite-pool convention, and none could use the API key this project is
configured with. Added `scripts/openalex_auth.py`, which
attaches the key to `api.openalex.org` requests and to no other host — so
`enrich_people_gscholar.py`, which uses one session for both OpenAlex and ORCID, cannot
leak the key to `pub.orcid.org`. Verified live: OpenAlex 200 with key, stray `mailto`
stripped, ORCID 200 with no key present.

**3.2 The ORCID matcher manufactured affiliations.** `best_facility_match()` scored a
candidate's employer against the person's facility with
`max(SequenceMatcher, token_overlap)` and no requirement that the names share a
distinctive word. Character similarity between two normalised organisation names sits
in the 0.51–0.67 band for unrelated organisations as often as for the same one, and the
default threshold was 0.45. Accepted matches included:

| Logged employer | Facility | Score |
|---|---|---:|
| Electric Power Research Institute | NERR | 0.560 |
| Oklahoma Medical Research Foundation | LTER | 0.593 |
| European Medicines Agency | Institute of Ocean Sciences | 0.513 |
| Appalachian State University | Apalachicola NERR | 0.529 |

The matcher now requires at least one shared *distinctive* token — proper nouns and
domain words, with generic organisation vocabulary ("research", "university",
"national", "institute") stripped — before any score is computed, and the
character-level score is averaged in at 0.25 weight rather than allowed to carry a
match on its own. True positives survive: 25 facility-verified accepts across the
roster, including Matt Ferner (San Francisco Bay NERR) and Valerie Paul (Smithsonian
Marine Station).

**3.3 The name-only fallback picked arbitrarily among namesakes.** Its comment promised
acceptance "only when there's exactly one candidate", but the code appended every
name-matching candidate and took the first after a sort in which all scored 0.0.
"David White" was accepted out of 25 ORCID candidates and "Christine Angelini" out of
26. The promise is now enforced where the count is known: a single distinct ORCID is
accepted (`accept-name-only`), two or more are logged `ambiguous-name-only` and left
null. 12 people are now correctly left unresolved.

**3.4 The rising cohort could never be filled.** Stage A grouped `/works` by author over
all time, which ranks by lifetime output and therefore selects long-career authors
exclusively. Across all 509 candidates measured on the first run, the most recent first
publication year was **2012** — so no author could satisfy the 10-year rising window,
stage D returned 0 rising, and `qa.py` failed its cohort invariant. Fixed by adding a
second stage-A pass restricted to the last 6 years, plus an early-career slice in
`shortlist()` that reserves measurement budget for authors with 5–60 works ranked by
citedness. Candidates rose 2,174 → 3,282, measured 509 → 717, and stage D now yields
exactly 100 / 100 / 50 with rising scholars first publishing 2016–2023.

---

## 4. Misattributed identifiers removed

Spot-checking the newly attributed publications surfaced identifiers already in the
committed data that pointed at different people. `scripts/wipe_misattributed_identifiers.py`
records the evidence per person and clears them; it is idempotent.

| Person | Wrong identifier resolved to | Works removed |
|---|---|---:|
| Andrew Thomson | 'M. Thomson', particle physicist (LHC collisions) | 188 |
| Clark Alexander | 'J. A. Clark' — given name matched as surname | 171 |
| Jean Wiener | 'O. Schneider' | 182 |
| Paul Orlando | 'C. Padilla Aranda' | 166 |
| Bob Miller | 'John A. Miller', service-oriented architecture | 131 |
| Anne M. Vogel | 'D. J. Ampleford', Sandia (laser-plasma physics) | 104 |
| James McClelland | 'James L. McClelland', Stanford cognitive scientist | 104 |
| Paul Dest | 'Julien Perret' | 100 |
| Alex Parker | 'A. H. Parker', planetary astronomer (Pluto) | 99 |
| Mike De Luca | 'Raktim Sarma' | 99 |
| Ed Sherwood | 'Morgan Sherwood' | 74 |
| Mark Sanborn | 'Mark A. Sanborn', mosquito-borne disease | 42 |

Totals: 8 ORCIDs and 12 `openalex_id`s cleared, **1,460 authorship rows** and 40
`person_areas` rows deleted, 2 rows whose `orcid` column held biography prose moved to
`notes`. Publications themselves were left in place — a work wrongly linked to one
person may be legitimately linked to another.

Note that ORCID and `openalex_id` were independently wrong in different rows: Ed
Sherwood and James McClelland have *correct* ORCIDs and wrong OpenAlex authors, so only
the OpenAlex id was cleared for them. Erik Smith is the reverse.

---

## 5. Caveats

- **Publication counts are capped at 100 per person.** `enrich_people_openalex.py` uses
  `max_records=100`, so 16 people sit exactly at 100 and their true output is higher.
  Any per-person productivity comparison drawn from `authorship` is censored at that
  ceiling; `works_count` on `community_scholars` is not.
- **13 of 220 measured scholars carry an implausible `first_pub_year`** (1800, 1845,
  1890 …). These are OpenAlex records with a mis-dated earliest work. They are excluded
  from the career-stage panel of the figure but remain in the table.
- **Some OpenAlex affiliation strings are wrong.** Carlos M. Duarte is recorded as
  "Centro de Investigação em Artes e Comunicação" and Marten Scheffer as "Department of
  Water". The metrics are right; the affiliation string is whatever OpenAlex last
  attached. Worth a curation pass before the roster is published.
- **Three topic mappings are approximations** and are documented inline in
  `data/datasets/coastal_topics.csv`: T10236 (coastal hypoxia) spans freshwater lakes,
  T11192 (ocean observing) is the underwater-vehicle/acoustic-sensor cluster, and
  T10747 (coastal hazards) is generic disaster resilience. OpenAlex has no closer
  cluster for any of the three.
- **Google Scholar ids: 0 of 169.** OpenAlex does not populate `ids.scholar` for these
  authors. Not a failure of the script; the source is simply empty.
- **Two labels share a topic id each.** "Sea Level Change and Coastal Flooding" and
  "Coastal Geomorphology and Shoreline Change" both map to T10647, and "Blue Carbon"
  shares T10779 with "Mangrove and Salt Marsh Ecosystems". The harvest ORs the ids into
  one filter so this is harmless, but it means 18 label rows resolve to 15 distinct ids.
- **The three web tabs still have never been rendered in a browser.** That item from
  `HANDOFF.md` §5 is untouched by this work.

---

## 6. Topic set

All 18 `RESOLVE` sentinels in `data/datasets/coastal_topics.csv` are resolved to
verified `T#####` ids. They were matched against the complete OpenAlex topic list
(4,516 topics pulled from `/topics`) on display name, description and keywords, rather
than by accepting the first hit from `/topics?search=`. Search ranking alone was tried
first and rejected: it returned nothing for 13 of the 18 labels and mapped two
different labels onto the same id. The combined filter matches 1,026,655 works. Each
non-obvious choice and each rejected alternative is documented in a comment block at
the head of the CSV.
