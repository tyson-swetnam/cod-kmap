# Google Scholar profile linkage

Where a researcher has a public Google Scholar profile, cod-kmap
surfaces a direct link on the researcher card. Scholar's h-index and
citation history are the most widely-recognised academic metrics, and
the link is high-value for non-specialist readers.

Google Scholar has no official API. We populate the field via a
tiered approach, preferring deterministic sources over scraping.

## Tiered sources

| Tier | Source            | Method                                | Coverage |
|-----:|-------------------|---------------------------------------|---------:|
| 1    | OpenAlex          | `external_ids.scholar` field          | ~30–50%  |
| 2    | ORCID             | `external-identifiers` block          | +10–20%  |
| 3    | Institutional homepage | Stored in `people.homepage_url`; reader follows the link | indirect |
| 4    | Paid SerpAPI / scholar_author | JSON; reserved for high-value queries | optional |

The first two tiers are deterministic, free, and run as part of the
nightly enrichment pass. They cover roughly half of the researchers
in the dataset.

## Schema

```
people.google_scholar_id : VARCHAR
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
