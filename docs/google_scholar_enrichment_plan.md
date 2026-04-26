# Google Scholar profile enrichment plan

After ORCID enrichment we still want a Google Scholar profile link
per researcher (the Scholar h-index + citation history is the
single most-recognised metric in academia and the people-card link
is a high-value affordance). Scholar has no official API; this doc
captures the tiered approach.

## Tier 1 — derive from OpenAlex (deterministic, free) ✅ ship today

OpenAlex Author records carry an `external_ids` block that often
lists `"google_scholar"` when the author has claimed it on their
ORCID or has been merged from another scraper. Coverage is partial
(~30-50% of marine researchers) but it's the only ZERO-cost,
ZERO-rate-limit path that doesn't risk Google blocking us.

`scripts/enrich_people_gscholar.py --source openalex` reads each
person's openalex_id, hits `/authors/<id>` (already cached
via the rest of the enrichment pipeline if we're polite), reads
`ids.scholar` if present, and writes to `people.google_scholar_id`.

## Tier 2 — derive from ORCID record (deterministic, free)

ORCID `/v3.0/<orcid>/external-identifiers` sometimes lists Scholar
when researchers have claimed it. Same pattern as OpenAlex; ~10-20%
incremental coverage on people who DIDN'T have it on OpenAlex.

`scripts/enrich_people_gscholar.py --source orcid`

## Tier 3 — `scholarly` library scrape (fragile)

The `scholarly` Python package scrapes Scholar's HTML with rotating
user-agents and proxy support. Without proxies it gets blocked after
~40-60 author lookups. With Tor/proxies it's slower but functional.
Realistic: 80-90% incremental coverage of the long tail, but
**operationally fragile** — Google rotates anti-bot tactics every
few months, and any future re-run requires a config check.

Recommendation: defer Tier 3 until we have a clear need. Most
researchers' Scholar profiles are ALSO on their institutional
homepage, and we already store `homepage_url`. A user clicking a
homepage link is one extra hop to find Scholar.

## Tier 4 — paid SerpAPI ($50/mo, deterministic)

`scholar_author` endpoint at https://serpapi.com — clean JSON,
unlimited rate (within plan), no scraping fragility. Justified ONLY
if we end up needing 100% Scholar coverage for a publication or
peer-review tool.

## Schema

`people.google_scholar_id` already exists (added in the original
schema). Format is the user_id segment of
`https://scholar.google.com/citations?user=<ID>`, e.g. `xKqqKf4AAAAJ`.

## Honest coverage estimate

  Tier 1 (OpenAlex):  ~30-50%
  Tier 1+2:           ~40-60%
  Tier 1+2+3:         ~80-90%
  Tier 1+2+3+4:       ~95%+

For ship-today, expect ~70-100 of our 242 people to get a Scholar link.

## Next steps after this commit

1. Run `python scripts/enrich_people_gscholar.py --source openalex` once
   to populate Tier 1.
2. After ORCID enrichment lands, run with `--source orcid` for Tier 2.
3. Update `src/views/people.js` card footer to show "Scholar" link
   when `google_scholar_id` is present.
4. (Optional, later) build Tier 3 with `scholarly` + delays + retry.
