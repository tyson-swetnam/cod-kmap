# Google Scholar profile linkage

Where a researcher has a public Google Scholar profile, cod-kmap
surfaces a direct link on the researcher card. Scholar's h-index and
citation history are the most widely-recognised academic metrics, and
the link is high-value for non-specialist readers.

Google Scholar has no official API. We populate the field via a
tiered approach, preferring deterministic sources over scraping.

## Tiered sources

| Tier | Source            | Method                                | Measured coverage |
|-----:|-------------------|---------------------------------------|---------:|
| 1    | OpenAlex          | `ids.scholar` field                   | ~0%      |
| 2    | ORCID             | `external-identifiers` block          | ~0%      |
| 3    | Institutional homepage | Stored in `people.homepage_url`; reader follows the link | indirect |
| 4    | Paid SerpAPI / scholar_author | JSON; reserved for high-value queries | not run |

**The first two tiers have now been run, and they return almost nothing.**
The projected coverage in an earlier version of this page — roughly half
the directory — was an estimate, and it was wrong by two orders of
magnitude. Measured results:

| Table | Rows | With a Scholar id |
|---|---:|---:|
| `people` (facility staff) | 280 | 4 |
| `community_scholars` (roster) | 523 | 12 |
| `person_registry` (unified) | 152,008 | 12 |

OpenAlex does not populate `ids.scholar` for the overwhelming majority of
author records, and ORCID's external-identifiers block rarely carries one
either. This is a property of the upstream sources, not a defect in the
scripts: the field the tiers read is simply empty. The 12 ids in
`person_registry` were all carried in from seed data rather than
resolved, but not all from the team seed as this page previously
claimed: 2 came from `people` (Myers-Pigg and Swetnam) and 10 from
`community_scholars`.

Anything approaching useful coverage would require a different source or a
hand-curation pass. Until then, treat a missing Scholar link as the
default state rather than as a gap to be explained.

## Schema

```
people.google_scholar_id             : VARCHAR
community_scholars.google_scholar_id : VARCHAR
person_registry.google_scholar_id    : VARCHAR
```

Format: the `user_id` segment of the Scholar URL, e.g.
`xKqqKf4AAAAJ` for `https://scholar.google.com/citations?user=xKqqKf4AAAAJ`.

## Front-end

When `google_scholar_id` is present, the researcher card adds a
**Google Scholar** link beside the homepage and ORCID buttons.
Otherwise the card silently omits the link rather than showing a
broken affordance.

## Why we don't scrape Scholar by default

The community `scholarly` Python package can scrape Scholar pages, but
Google rotates anti-bot measures every few months. Any pipeline built
on `scholarly` becomes operationally fragile and requires occasional
configuration changes. We've chosen to defer this work until there's
a clear product reason to need 100% Scholar coverage.
