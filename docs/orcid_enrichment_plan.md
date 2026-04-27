# ORCID-based researcher enrichment

cod-kmap links each researcher to their ORCID identifier wherever one
is available. ORCID is the international standard for persistent,
self-claimed researcher identifiers, and we use it as the primary
disambiguator before linking to OpenAlex, Scopus, or Google Scholar.

## Why ORCID is the anchor

ORCID identifiers are claimed by the researcher themselves. Once a
person's ORCID is linked, every downstream lookup becomes deterministic:

- **OpenAlex**: `/authors?filter=orcid:0000-...` returns exactly one
  author record.
- **Scopus** and **Web of Science**: ORCID linkage is published in the
  ORCID record's *external identifiers* section.
- **Publisher metadata**: increasingly required by journals, so newer
  publications are reliably attributed.

Without ORCID, downstream resolvers fall back to name search, which
collapses any two researchers who happen to share a name.

## Resolution rules

A candidate ORCID is accepted only when **all** of the following hold:

- Family name matches exactly (case-insensitive, diacritic-normalised).
- The first given name matches (handles "Sarah" vs. "Sarah J." vs.
  "Sarah Jane").
- The candidate's current or past employments include an organisation
  whose name fuzzy-matches one of our facility records for that
  person at ≥ 0.85 similarity.
- If multiple candidates pass the above, prefer the most recent
  employment, then the candidate already linked to one of our
  OpenAlex authors.

If no candidate satisfies every rule, no ORCID is recorded. A NULL
identifier is preferable to a wrong one.

## Sources and rate limits

The ORCID Public API is free and requires no key:

- `GET https://pub.orcid.org/v3.0/expanded-search/?q=<query>` —
  candidate profiles with name, current employments, and external
  identifiers (rate-limit ~24 req/s).
- `GET https://pub.orcid.org/v3.0/<orcid>/employments` — confirm a
  candidate against our facility records.

## Coverage

Roughly two thirds of the researchers in the dataset (~160 of 242)
resolve to a verified ORCID. The remainder are typically:

- Reserve managers, programme directors, and similar administrative
  roles whose work doesn't appear in indexed journals.
- Researchers who haven't claimed an ORCID record yet.

These rows stay un-linked rather than risk a wrong attribution. A
periodic re-run picks up newly-claimed ORCIDs without manual work.

## Audit trail

Every resolution decision (accept / reject / no candidate) is logged
to `data/seed/orcid_resolution_log.csv` with the candidate ORCID,
similarity scores, and reason. The log is the source of truth for
"why does this person not have an ORCID?" questions.
