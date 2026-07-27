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
  that shares at least one **distinctive token** with one of our facility
  records for that person — a proper noun or domain word, with generic
  organisation vocabulary ("research", "university", "national",
  "institute") stripped out first — and then clears the score threshold.
- If multiple candidates pass the above, prefer the most recent
  employment, then the candidate already linked to one of our
  OpenAlex authors.

The distinctive-token requirement is a gate, not a scoring term, and it
exists because character-level similarity between two normalised
organisation names sits in the same band for unrelated organisations as
it does for the same one. An earlier version scored candidates on
character similarity alone and accepted several employer-to-facility
matches between organisations with nothing in common beyond shared
letters. The character-level score is now averaged in at low weight
rather than being able to carry a match by itself.

Where only a name matches and no employment can be tested, acceptance
requires that the name resolve to exactly **one** distinct ORCID. Two or
more candidates are logged as ambiguous and left null — an earlier
version promised this in a comment but in fact took the first candidate
after a sort in which every candidate scored zero, which for common names
meant picking arbitrarily among dozens of people.

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

Coverage in the facility-staff directory is low: **49 of 280** `people`
rows carry a verified ORCID. It was briefly higher, before an audit found
identifiers already in the committed data that pointed at different
people entirely and cleared them; the strict matcher above then declined
to re-resolve most of those names. That is the intended trade. The
un-linked rows are typically:

- Reserve managers, programme directors, and similar administrative
  roles whose work doesn't appear in indexed journals.
- Researchers who haven't claimed an ORCID record yet.
- Researchers whose name is shared with enough other people that no
  candidate can be distinguished safely.

These rows stay un-linked rather than risk a wrong attribution. A
periodic re-run picks up newly-claimed ORCIDs without manual work.

ORCID coverage is far higher in `person_registry`, which draws
identifiers from author records rather than resolving names: 86,620 of
152,008 identities are keyed on an ORCID, 9,309 of the 10,000 rows served
to the browser. Those are ORCIDs asserted by the bibliographic source on
an author record, not resolutions this repository performed. See
[The Person Registry](#/docs/person-registry).

## Audit trail

Every resolution decision (accept / reject / no candidate) is logged
to `data/seed/orcid_resolution_log.csv` with the candidate ORCID,
similarity scores, and reason. The log is the source of truth for
"why does this person not have an ORCID?" questions.
