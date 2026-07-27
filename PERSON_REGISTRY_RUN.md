# Person registry, cross-cohort collaboration, and roster scale-up

**Repo:** cod-kmap at `aa08fb1` (branch `claude/bibliometric-enrichment`) · **Run:** 2026-07-27
**Gate:** `scripts/qa.py` exits 0 on the working DB and after a clean `rebuild_db_from_parquet.py`

Three asks, in dependency order: unify identity, measure who works with whom, then scale.

## 1. One identity space

`people` (facility staff), `cod_team_members` (org chart) and `community_scholars`
(field roster) shared **1 ORCID and 6 exact names** across 843 rows. Anything asking
"who works with whom" was unanswerable, because one researcher could be three rows
with three keys and no edge between them.

`person_registry` is now that space: one row per human, keyed on a persistent
identifier, with a role flag per cohort. Two rows merge **only** on ORCID or
`openalex_id` equality — names never merge. That rule is inherited from three
wrong-person incidents in this repo, the most recent of which cleared 1,460
authorship rows.

| | Coverage |
|---|---:|
| Identities | **152,008** |
| with ORCID | 86,672 |
| with OpenAlex id | 152,004 |
| with ROR-bearing affiliation | 121,372 |
| with homepage | 219 |
| countries represented | 202 |
| provenance assertions | 152,653 |

Every row carries a `source_url`, a confidence rating, and at least one
`person_identity_source` assertion naming the rule that produced it. `qa.py`
enforces all of that plus identifier uniqueness — 14 assertions across two new
check functions, each verified by injecting the defect it targets.

**Identifier resolution.** 402 rows arrived with no persistent id at all. A
five-rule resolver (exact family name, compatible given names, shared *distinctive*
institution token, no conflicting ORCIDs among survivors, no stub records) resolved
226 and **refused 32**: 16 as ambiguous (8 with conflicting ORCIDs, 8 institution-verified duplicates with no ORCID to prove they are one person), 12 as stubs, 3 whose affiliation was absent or too generic to test, and 1 other. Rule 5 exists because testing
caught "Andrew G. Dickson" resolving to a 2-work shard named `A. DICKSON`, which
would have attached two papers to a researcher with hundreds and then ranked him
near the bottom. Negative controls hold: "Y. Stacy Zhang" and "Y. Joseph Zhang" stay
distinct, and a correct name at a wrong institution is refused.

Google Scholar ids remain nearly absent (12 of 152,008). OpenAlex does not
populate `ids.scholar` for most authors; that field needs a different source or
hand curation.

## 2. Who works with whom

`collaborations` is keyed on `people(person_id)`, so it structurally cannot express
a Team↔Scholar edge. `registry_collaborations` is keyed on the registry, and holds
**5,300 co-publication edges** — 51× the 103 the live site serves.

Cross-cohort census:

| Edge type | Count |
|---|---:|
| Scholar ↔ Scholar | 4,525 |
| Site personnel ↔ Scholar | 942 |
| Site personnel ↔ Site personnel | 141 |
| **Scholar ↔ Team** | **97** |
| **Site personnel ↔ Team** | **41** |
| **Team ↔ Team** | **1** |

Edge counts were validated against OpenAlex's own two-author filter: 13 of 14
sampled edges exact. A 600-work fetch cap was found undercounting four of the top six edges by
4–23% (the two whose endpoints both sat under the cap were already exact) and
was raised to 6,000.

### Team blind spots

- **The Team barely co-publishes with itself.** 14 members, **one** internal edge.
  Two members have no co-authorship edges at all; three have no link to any Scholar.
  Median Team degree is 7.5 against 19 for the Scholars cohort.
- **Seven of fourteen active coastal topics have no Team member.** By scholar
  output: Oceanographic and Atmospheric Processes (14,136 works), Ocean
  Acidification and Carbonate Chemistry (6,796), Coastal Remote Sensing (5,058),
  Coastal Hypoxia and Eutrophication (4,400), Coastal Zone Management and Policy
  (2,376), Ocean Observing Systems (959), Coastal and Marine Sediment Transport
  (160). Ocean observing is the sharpest: 3.8% of its scholar output is reachable
  from the Team in one hop.
- **The Team is US-only.** 36 of 44 countries with scholars have no Team presence,
  and only 23 of 249 non-US members link to the Team at all.
- **But the gap is one introduction wide.** 357 of 440 scholars have no direct Team
  path — and **277 of them are two hops away**. The unreached list is topped by Carl
  Folke, Stephen Carpenter, Daniel Pauly and Marten Scheffer, each reachable through
  an existing collaborator.

The five strongest existing bridges are Angelini↔Silliman (41 co-pubs),
Bond-Lamberty↔Megonigal (26), Anderson↔Kudela (22), Myers-Pigg↔Megonigal (16),
Landry↔Bin (14). 11 people are in more than one cohort at once — including
Ben Halpern, Robert Howarth and Fiorenza Micheli, who are simultaneously site
personnel and roster scholars, a fact the previous schema could not represent.

## 3. Scale-up

The `/authors` endpoint filters on `topics.id` directly and returns ORCID,
ROR-bearing institution, `summary_stats` and per-topic work counts in one page of
200 — everything the old three-stage candidate/hydrate/measure pipeline produced,
at a fraction of the requests. `topics[].count` supplies coastal output with no
extra calls.

- **190,397** authors match the 15 coastal topics with ≥10 works
- **151,946** cleared a ≥3-coastal-works floor and were ingested
- Registry: 618 → **152,008** across 202 countries, zero duplicate identifiers

### Tiering

The browser loads parquet directly, so the full population cannot ship.

| Tier | Rows | Avg h-index | Avg coastal works | Countries |
|---|---:|---:|---:|---:|
| `core` (shipped) | 10,000 | 39.5 | 94.9 | 118 |
| `archive` (local only) | 142,008 | 10.6 | 13.7 | 201 |

Scoring uses **percentile ranks** of coastal output, h-index, recent citedness and
graph degree. Raw values were tried first and put generalists with vast non-coastal
output above coastal specialists. All 618 COD-affiliated people are pinned into
`core` regardless of score — a metric threshold must never drop the roster the site
exists to show. Site `person_registry.parquet` is 1.0 MB; total site payload 7.9 MB
across 37 files.

## 4. Scholars → sites

`facilities` had no persistent organisation id, so a researcher's OpenAlex
affiliation had nothing to join to. Added `facilities.ror` and
`registry_facilities`.

- **69** facilities resolved to a ROR, out of ~209 research organisations
- **1,467** researcher↔site links across **39** sites, joined on ROR equality alone
- 3,310 protected areas skipped by design — a state park is a place, not an
  organisation, and will never hold a ROR

A contested-ROR guard was added after "Monterey Bay Aquarium" matched MBARI's ROR
on shared tokens, which would have attached 90 MBARI researchers to the public
aquarium. Any ROR claimed by two facilities is now refused and reported.

## Caveats

1. **ROR coverage is a floor, not a ceiling.** 138 facilities were never attempted:
   the OpenAlex API budget was exhausted by the 152k harvest (resets midnight UTC).
   Re-running `link_registry_facilities.py` extends coverage with no other changes.
2. **`coastal_works_count` is an upper bound on distinct coastal papers.** OpenAlex
   lists a work under every topic it carries, so summing `topics[].count` over the
   coastal set counts a multi-topic paper more than once. It exceeded `works_count`
   for 10,571 of 152,004 rows, reaching a share of 3.0 before being clamped. The sum
   is still the best available signal of coastal volume, but do not read it as a
   distinct-paper count.
3. **The co-authorship graph covers the original 618, not all 152,008.** Building it
   over the full population is ~152k works queries; the graph shipped here is the
   COD-relevant subgraph. Degree therefore contributes to tier scoring only for
   nodes that were in the registry before the harvest.
4. **The `recency` term in tier scoring is a proxy.** It scores
   `two_yr_mean_citedness` — recent citation impact — not recent output volume.
   The harvest computes a recent-output count but `person_registry` has no
   column for it; adding one is the obvious refinement.
5. **Two ORCID conflicts are logged, not applied** — cases where OpenAlex reports a
   different ORCID than the registry holds. They sit in `person_identity_source`
   with `field='orcid-conflict'` for curation.
6. **Nothing has been rendered in a browser.** Verification was DuckDB executing the
   views' SQL and checking site parquet is self-consistent, not a live page.

## What ships where

`db/parquet` carries the full 152,008-row population; `public/parquet` carries only
the core tier, and only edges and facility links whose endpoints ship — 1,204 of
1,467 facility links pointed at archive-tier researchers and are correctly withheld.
The other 33 parquet files are byte-identical between the two directories.
