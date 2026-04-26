# ORCID enrichment plan

After wiping the misattributed OpenAlex IDs (commit landing this turn —
56 people whose enrich_people_openalex.py auto-resolved them to
cardiologists / particle physicists / family-medicine MDs), the
people table has very few ORCID IDs. This document captures how we
fill in real ORCIDs without repeating the same name-collision bug.

## Why ORCID first, then OpenAlex

ORCID is a globally-unique researcher identifier with strict
self-claim semantics — a person's ORCID record links to
publications THEY claimed, employments THEY listed, and external
identifiers (Scopus, ResearcherID, OpenAlex Author ID) THEY linked.
Once we have an ORCID, OpenAlex resolution is deterministic
(`/authors?filter=orcid:0000-…`) and we can never again collapse
two distinct researchers with the same name.

## The ORCID Public API

Free, no key required (rate-limited to ~24 req/s without a token,
~100/s with one). Endpoints we'll use:

  * `GET https://pub.orcid.org/v3.0/expanded-search/?q=<query>&start=0&rows=10`
    — Returns up to 10 candidate profiles with summary fields:
    given-names, family-name, current-employments, current-educations,
    other-name, credit-name, ORCID id.
    Accept: `application/json`

  * `GET https://pub.orcid.org/v3.0/<orcid>/employments`
    — Once a candidate is selected, fetch their full employment
    history to confirm a match against our facility list.

## Resolution rules (strict — anti-misattribution)

For each `(person.name, person.facility_name(s))` pair we want to
accept ONLY a candidate that satisfies ALL of these:

  1. Family name matches **exactly** (case-insensitive,
     diacritic-normalised).
  2. Given names match the first one (handles "Sarah" vs.
     "Sarah J." vs. "Sarah Jane").
  3. The candidate's `current-employments` OR `past-employments`
     contains an organization whose name fuzzy-matches one of our
     `facility_personnel.facility` rows for that person at >= 0.85
     similarity (Levenshtein-ratio).
  4. If multiple candidates pass 1+2+3, prefer the one with the
     most recent employment. If still tied, prefer the candidate
     whose ORCID is also linked from an OpenAlex author record we
     already have (cross-check via `/authors?filter=orcid:…`).

Any failure to meet ALL of (1, 2, 3) means we DO NOT assign that
ORCID. We'd rather have a NULL than a wrong ID.

## Workflow

```
scripts/enrich_people_orcid.py
  --db db/cod_kmap.duckdb
  --batch 50            # how many people to process per run (rate-limit polite)
  --min-conf 0.85       # employment-name similarity threshold
  --dry-run             # don't write
  --only-missing        # default: skip people who already have orcid
```

For each person without an ORCID:
  a. Build the search query: `family-name:LAST AND given-names:FIRST`.
     Fall back to `q="<full name>"` if no candidates.
  b. POST through the rules above; if a single candidate passes,
     fetch their employments and compare to facility_personnel.
  c. Insert into `people.orcid` only if ALL rules pass.
  d. Log every decision (accept / reject / no-candidates) to
     `data/seed/orcid_resolution_log.csv` for audit.

## Then re-link OpenAlex

After ORCID enrichment lands, re-run `enrich_people_openalex.py`
WITHOUT the name-only fallback (gate it behind `--allow-name-only`).
For people with an ORCID, OpenAlex returns the deterministic
match. For people still without an ORCID, we just leave them
unenriched rather than risk another misattribution.

## Effort

  * Build script: 2-3 hours
  * Run on 242 people @ ~0.4 s avg (1 search + 1 employments call):
    ~2 min wall time
  * Manual review of 'low-confidence' rejected candidates: 1-2
    hours, optional
  * Re-run OpenAlex enricher: ~10 min

Total: half a day from script to fully re-attributed dataset.

## Open question

ORCID coverage in marine science is uneven. Established US LTER PIs
have ORCIDs (most have papers in journals that require them).
Reserve Managers, NEP Directors, NPS Park Superintendents — these
administrative roles often DON'T have ORCIDs. Coverage estimate:
60-70% of our 242 people will resolve; the rest will stay NULL.
That's the honest answer; we don't manufacture identifiers.
