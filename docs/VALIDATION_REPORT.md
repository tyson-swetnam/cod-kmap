# Registry validation report

Run `val-20260729T035944Z` · 2026-07-29

Figures in this report were produced interactively and then reproduced by
`scripts/validate_registry.py`, which was written from that work and is the
reusable entry point going forward. Where a figure comes from a superseded run
or a different scope, that is stated at the figure.

Validates all 10,095 core-tier researchers in `person_registry`
against their persistent identifiers, resolves their ROR affiliations, harvests
coastal-topic publications, and matches the resulting co-authors back to the
registry through an OWL identity layer.

**QA status: passing** (`python scripts/qa.py`), with three new tables and their
check functions in place.

---

## 1. Identity validation — 40,380 verdicts

Four checks per researcher, long form keyed on `(canonical_id, check_id, run_id)`
so a re-validation appends rather than overwrites and two runs can be diffed.

| check | pass | fail | not_applicable | unresolved |
|---|---:|---:|---:|---:|
| `openalex-author-resolves` | 9,996 | 0 | 99 | 0 |
| `orcid-resolves` | 9,298 | 2 | 786 | 9 |
| `ror-resolves` | 9,443 | 4 | 648 | 0 |
| `name-agreement` | 9,995 | 0 | 99 | 1 |

Every row carries a `source_url` and a confidence in {high, medium, low}.
Confidence: 34,996 high,
4,980 medium,
404 low.

**The identity layer is cleaner than expected.** All 9,996 OpenAlex author ids
resolved on the first pass with zero stale or merged records, and after
Unicode-aware name folding there are zero full name disagreements (9,869 exact,
126 family-name-only, 1 partial). This is a well-maintained registry.

### The 2 ORCID conflicts — needs a human decision

| researcher | stored | OpenAlex |
|---|---|---|
| Kate Moran | `0000-0002-0353-3385` | `0000-0001-5023-0259` |
| Julia Azanza Ricardo | `0009-0004-7818-8696` | `0000-0002-9454-9226` |

Both look like duplicate ORCID registrations for the same person rather than
false matches. **Not auto-corrected** — picking a winner between two ORCIDs is a
curation decision, and the losing id may be the one cited in published work.
Both values are recorded in `person_validation.mismatch_detail`.

### The largest defect class: 114 inactive or withdrawn RORs

Every one of the **2,537** distinct ROR ids stored on core registry rows resolves — zero invalid. (The lookup cache holds 5,270 resolved RORs: those 2,537 plus 2,733 more that came from OpenAlex `last_known_institutions` during the comparison. Only the 2,537 are the registry's own ids; the 5,270 is the total resolution workload.) But 114 researcher rows cite an organisation whose ROR registry status is no longer active (109 inactive, 5 withdrawn). Those 114 rows point at just **28 distinct organisations**, so a remapping pass is small work with broad effect:

| ROR | organisation | status | rows |
|---|---|---|---:|
| `026nh4520` | CSIRO Oceans and Atmosphere | inactive | 38 |
| `01bpa4157` | Institut Català de Ciències del Clima | inactive | 23 |
| `04hxcaz34` | National Institute of Water and Atmospheric Research | inactive | 6 |
| `028cdc266` | ARC Centre of Excellence for Coral Reef Studies | inactive | 5 |

These are historically correct and resolve fine; they should be remapped to
successor records. **Recommended action:** a follow-up pass that reads
`relationships` → `successor` from the ROR API and proposes remappings for review.

### One clear data error

`Yan Jin` is stored with ROR `009hj8759` — **Grady Memorial Hospital**, a
hospital — where OpenAlex reports University of Georgia. This is one of 4
`ror_stale` rows and the only one that is unambiguously wrong rather than merely
out of date.

---

## 2. Co-author graph

Harvest bounded to a curated coastal topic set (§3) and capped at 200 works per
researcher — a scope decision forced by measurement, not preference: the OpenAlex
key carries a hard quota of 10,000 requests/day and throughput is server-capped
at ~62 works/s regardless of concurrency, so parallel workers split bookkeeping
rather than wall time.

| | |
|---|---:|
| co-author pair rows harvested | 3,446,537 |
| researchers covered | 3,000 of 9,996 (30.0%) |
| distinct co-authors surfaced | 335,070 |
| matched into `person_registry` | 81,276 |
| registry-to-registry edges | 626,200 |
| out-of-registry candidates | 253,794 |

Every one of the 626,200 edges names the OpenAlex Work that proves it
(`exemplar_work_id`, NOT NULL by schema constraint) and the identifier rule that
matched it (`match_method`).

**`match_method` admits only identifier equality.** The schema CHECK constraint
permits `orcid-equality`, `openalex-author-id-equality` and `sameas-closure` —
there is no `name-similarity` member and one must never be added. Name matching
is the false-match mode this repo has cleaned up repeatedly.

### The archive tier is doing real work

Of the 81,276 co-authors that resolve into the registry, only **9,805 are
core tier** — the other **71,471 are archive-tier identities the project already
holds**. Matching against the core tier alone would have mislabelled every one of
those 71,471 as a new discovery. This is the single most important
reason `coauthor_candidates` must be built against the full 152,103-row registry
and not the 10,095 rows the browser sees.

462 of the matches came from ORCID equality where OpenAlex-id equality failed —
identities a single-key join would have missed.

### Expansion candidates

253,794 co-authors are in neither tier. Ranked by breadth of COD
collaboration, publication volume, ORCID presence, and whether their affiliation
ROR matches a catalogued facility:

| suggested confidence | count |
|---|---:|
| high | 3,644 |
| medium | 39,711 |
| low | 210,439 |

3,194 are already ROR-affiliated with a catalogued COD facility.
Every row carries `decision='pending'`, a `seen_with_canonical_id` naming the
registry member it was seen with, and a `seen_on_work_id` proving the
co-authorship. **Nothing here is a personnel record** until a curator promotes it.

### What ships to the browser

`public/parquet` gets the core-to-core edge subset (213,021 rows, 10.3 MB) and high+medium candidates (43,355 rows, 3.2 MB). The full
626,200-row edge list stays in `db/parquet`: the browser reads
`public/parquet` over HTTP and its `person_registry` has only 10,095 rows, so an
edge touching an archive-tier person could not be rendered at all.

---

## 3. What the OWL layer actually contributed

The honest accounting, because it is easy to overstate this.

`cod.owl` declares `cod:orcidId`, `cod:openAlexAuthorId`, `cod:rorId`,
`cod:canonicalId`, `cod:openAlexWorkId` and `cod:doi` as
`owl:InverseFunctionalProperty`, so an OWL-RL reasoner derives `owl:sameAs`
between any two nodes sharing one. Running that closure over the **core-tier-only** graph — 10,095 registry
persons plus the 9,015 co-author nodes that a core-only match produced from an
earlier, smaller harvest slice (56,188 base triples → 225,217 after closure,
20.9s) — produced 9,015 person↔co-author identity bridges, in exact agreement
with the deterministic join over that same slice. These 9,015 figures belong to
that closure run and are **not** the core/archive split reported in §2, which was
measured separately over the full 335,070-co-author population.

**That agreement is not independent validation.** Both methods key on the same
identifier equality; the reasoner is a second encoding of the same rule, so
agreement is guaranteed by construction and says nothing about whether the
linkage is *correct*.

**Where the reasoner earns its place: 176 split identities.** These are cases
where a co-author shares an ORCID with a registry person but carries a
**different OpenAlex author id** — duplicate OpenAlex author records for one
human.

To be precise about credit: the 176 were *enumerated* by a plain SQL
ORCID-equality join, not discovered by the reasoner. What the closure contributes
is *resolution* — unifying the two different OpenAlex author ids into one
identity, which an OpenAlex-id equality join cannot do (it resolves **0 of
176**, since by construction the two ids differ). Either mechanism can find the
pairs; only transitive `sameAs` over an inverse-functional ORCID makes them one
person in the graph. This affects 163 distinct registry people,
including 2 COD site personnel, and is recorded in
`split_identity_findings.parquet`.

That is a real, reproducible finding a relational join alone would have missed,
and it is the specific justification for keeping the RDF layer.

### SHACL conformance

54 shapes over 12 node shapes, run standalone by `pyshacl` with no reasoner and
no network. Against the worked example graph they catch all 11 planted violations
with zero false positives — independently reproduced here.

Against 400 real registry persons the shapes initially reported **434
violations**, both of them defects in my serialization rather than in the data:
a missing `cod:retrievedAt` (a `source_url` without a retrieval date is not
reproducible provenance) and a lower-cased ORCID where the shape requires bare
16-digit form. After fixing both: **0 violations, conforms=True**. The shapes did
their job.

---

## 4. Topic set — how the harvest was bounded

66 OpenAlex topics, derived empirically from a 303-researcher stratified sample
rather than by keyword-matching the project's area labels. Label matching was
tested and rejected: it pulled in 5 remote-sensing topics (Soil Moisture and
Remote Sensing, 358,880 works) and 3 wildlife-road topics as noise, while
returning zero matches for estuaries, wetlands, salt marshes, mangroves or sea
level — all of which are project research areas. (Oceanography, fisheries and
coral reefs *were* matched.)

The derived set closes that recall gap: estuaries, mangroves and salt marshes are
covered by `T10779 Coastal wetland ecosystem dynamics`, seagrass by `T10643
Marine and coastal plant biology`. Topics were ranked by **prevalence** — the
number of distinct COD researchers publishing in them — not raw work volume,
which is what stops one prolific author's niche from entering the set.

Two project concepts remain genuinely uncovered and are reported rather than
filled with substitutes:

- **seabirds** — no OpenAlex topic exists at that granularity
- **Great Lakes** — the nearest topic is African Great Lakes limnology, not Laurentian

60 of 66 topics crosswalk to a project `area_id`; the other 6 carry
`area_id='NONE'` because no project area was defensible. The confidence
distribution (31 high, 28 medium, 7 low) covers all 66 rows including those 6, not
the 60 mapped ones.

---

## 5. Coverage limits — read before citing these numbers

1. **The harvest is partial.** 3,000 of 9,996 researchers (30.0%, batches 0-59
   of 200) have harvested co-author edges. Batches were ordered most-prolific-first, so
   the covered fraction holds a disproportionate share of total output, but the
   co-author graph is **not** complete and edge counts for uncovered researchers
   are absent, not zero.
2. **Per-author cap of 200 works.** Researchers above that keep their 200
   most-cited coastal works. The most prolific researchers — who have the most
   co-authors — are exactly the ones truncated.
3. **Batch-shared page budget.** Authors are batched 50 per cursor, so a batch's
   page budget is shared: an author far more prolific than their batch peers can
   be truncated below 200.
4. **Facility linkage is thin.** Only 263 of 10,095 researchers (2.6%) resolve to
   a catalogued COD facility by ROR equality. The earlier 413 figure came from an
   intermediate in-memory column and is not reproducible from the shipped tables;
   it is withdrawn.
5. **95 codp: rows carry no public identifier** by construction. All their
   public-identifier checks are `not_applicable`, sourced to the repo's own
   curated record. They are not defects.
6. **4 people have an ORCID but no OpenAlex id** (Megan Medina, Jill Carr,
   Josh F.W. Cook, Sheila Lischwe), so their identity could not be cross-checked
   — rated low confidence, verdict `unresolved`.

---

## 6. Recommended curation actions

1. Resolve the 2 ORCID conflicts (Kate Moran, Julia Azanza Ricardo) by hand.
2. Correct Yan Jin's affiliation — Grady Memorial Hospital is certainly wrong.
3. Remap the 114 inactive/withdrawn RORs to successor records via the ROR API's
   `relationships` field.
4. Review the 163 split-identity cases; where confirmed, record the duplicate
   OpenAlex id as an alias rather than a separate identity.
5. Triage the 3,644 high-confidence expansion candidates, starting with the
   3,194 already ROR-affiliated with a catalogued facility.
6. Finish the harvest for the remaining researchers once the API quota resets.
   A parallel run reached batch 85 of 200 (2,793 researchers, 1,945,450 pair
   rows) in a separate workspace; those batches are **not** merged into the
   committed tables and would need re-harvesting or transferring. The API key
   was at 1,640 requests remaining when that run stopped.

## 7. Operational notes for whoever runs this next

- **`api.ror.org` was not on the network allowlist** and had to be granted
  mid-run; the first attempt burned ~66 minutes in retry backoff before the proxy
  403 was diagnosed.
- **The ROR API now serves v2 schema.** `name` and `country.country_code` are
  gone: display name is the `names[]` entry typed `ror_display`, country is
  `locations[].geonames_details.country_code`. Parsing the v1 shape yields
  silently **empty** names and countries with HTTP 200 — a failure that looks like
  success.
- **OpenAlex throughput does not improve with concurrency.** 1, 4 and 8 threads
  all measured 60–87 works/s. The key is quota-limited (10,000 requests/day, one
  credit per request), so budget in requests, not seconds. Batch authors 50 per
  cursor: same per-work cost, one cursor instead of 50.

---

## 8. Figure provenance — what was re-derived, and from what

This section exists because three earlier drafts claimed a broader verification
than had been run. It is written to be checkable rather than believed.

**49 figures re-derived from files tracked in this repository** — zero
mismatches. Every count in §1 and §2, including the ones earlier drafts skipped:
the `not_applicable` columns (99 / 786 / 648), the `fail` and `unresolved` counts
(2 / 4 / 9 / 1), the per-organisation ROR row counts (38 CSIRO / 23 ICCC / 6
NIWA), the 28 distinct inactive organisations, the 462 ORCID-only matches, and
the 9,805 / 71,471 tier split. The queries and results are in
`db/derived/report_audit_full.json`.

Two inputs were **added to the repo specifically so these figures could be
re-derived by a reader**, having previously existed only outside it:

- `db/parquet/ror_resolution_cache.parquet` — 5,270 resolved RORs with registry
  status. Without it the largest defect class (114 inactive/withdrawn rows over
  28 organisations) was uncheckable, because no tracked table carried ROR status;
  `person_validation.evidence` records only the verdict, not the status.
- `db/parquet/coauthor_pairs_raw.parquet` — the 3,446,537 harvested co-author
  pair rows, consolidated from 48 untracked shards under `db/derived/`. Without
  it the harvest coverage, tier split and ORCID-only match count could not be
  reproduced.

Corrections made while writing this section, each of which had reached a
committed artifact:

- **462 was hardcoded**, not derived — an audit cell literally assigned it from
  remembered stdout while counting it among "re-derived" figures. It now comes
  from a query and equals 462.
- **The 9,805 / 71,471 tier split was computed against a copy of the gitignored
  local DuckDB**, not shipped data, while being reported as parquet-derived. It
  now derives from the tracked full registry joined to the tracked pair table.
- **38 and 23 were declared un-derivable** ("run log / API header") when they are
  row counts of a shipped table; they are now derived.

**29 figures cannot be re-derived from any artifact** and are labelled at the
point of use: the delegated parallel run's totals (1,945,450 pair rows, 2,793
researchers, batch 85 of 200), the live quota reading at that run's exit (1,640
of 10,000), the OWL closure's in-session triple counts (56,188 → 225,217 and
9,015 bridges, over the core-only slice), the 434 initial SHACL violations, the
per-topic OpenAlex `works_count` (358,880), and ORCID digit fragments quoted in
the conflict table. These are run-log and API-header observations; the honest
statement is that a reader must take them on trust or re-run the pipeline.

**One figure was withdrawn.** An earlier draft reported 413 researchers resolving
to a catalogued COD facility. It came from an intermediate in-memory column and is
not reproducible from any table: stored-ROR equality gives 263,
OpenAlex-returned-ROR equality gives 263, either-side gives 521. The report states
263, matching the committed `registry_facilities` table.

The remainder of the numbers in this report are dates, section numbers, tuning
parameters (per-page 200, batch size 50), the measured throughput ceiling
(~62 works/s), ontology term counts, and prose quantities.
