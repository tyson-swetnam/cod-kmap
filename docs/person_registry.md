# The person registry — one identity space for three cohorts

cod-kmap describes people in three different ways. Some are staff at a
catalogued facility. Some sit on the COD project organisational chart.
Some are members of a field-wide roster of coastal ocean science
researchers, most of whom have no connection to a catalogued site at
all. Those three descriptions were built independently, at different
times, from different sources, and each got its own table.

That was workable for as long as the only questions being asked were
per-cohort — *who staffs this reserve?*, *who leads this work-breakdown
track?*. It broke down as soon as the question spanned cohorts.
**Who does the project team already publish with?** is a question about
all three at once, and it was unanswerable: the same researcher could
appear as three rows with three unrelated keys and nothing connecting
them.

`person_registry` is the table that fixes that. It is a single identity
space: one row per human, keyed on a persistent identifier, carrying a
membership flag for each cohort the person belongs to.

## What the table is, and what it is not

The registry does **not** replace the three source tables. Those keep
their own columns and their own grain — a team member still has a
work-breakdown code and a role; a roster scholar still has cohort flags
and ranks; facility staff still have a role at a named site. What the
registry adds is a common node identifier, so that anything computed
over people can be computed **once**, over one node set, rather than
three times over three disconnected ones.

The co-publication graph is the clearest example. The older
`collaborations` table is keyed on facility-staff identifiers, so it is
structurally incapable of expressing an edge between a project team
member and a roster scholar — not because no such collaborations exist,
but because the table has nowhere to put them. `registry_collaborations`
is keyed on the registry, so it can.

## What the canonical key means

Every row is keyed on `canonical_id`, a string of one of two forms:

| Form | When it is used |
|---|---|
| `orcid:0000-0000-0000-0000` | The person has a known ORCID |
| `openalex:A1234567890` | No ORCID is known; an OpenAlex author id is |

Of the 152,008 identities in the local database, 86,620 are keyed on an
ORCID and 65,388 on an OpenAlex author id. A row must carry at least one
of the two — a registry entry with neither could never be re-resolved or
de-duplicated on a later run, so the quality gate rejects it.

The key is derived from the identifier itself, not from a hash of
mutable fields such as name, email, or affiliation. That means it is
stable across rebuilds: re-running the pipeline on refreshed source data
produces the same `canonical_id` for the same person, so links built on
top of the registry survive a refresh.

### Two rows merge only on identifier equality

This is the single most important rule in the table, and it is worth
stating plainly because it constrains what the registry can and cannot
do for you.

**Two records merge into one identity only on a shared identifier.**
Name similarity never merges anything — not a high fuzzy-match score, not
an exact name match, not an exact name match at the same institution.
Only ORCID or OpenAlex-author-id equality.

The rule is not conservatism for its own sake. This repository has had
to undo wrong-person attributions more than once, and the failure mode is
severe: attaching one researcher's identifier to another's row
misattributes their entire publication record, and every metric computed
downstream — h-index, citation count, coastal output, collaboration
degree, cohort rank — is then wrong in a way that looks entirely
plausible on the page. A missing identifier costs a blank field. A wrong
identifier costs the credibility of every number next to it.

The consequence to keep in mind when reading the site: two rows with the
same display name may be the same human. If neither carries a shared
persistent identifier, the registry will not assert that they are, and
the interface will not merge them. Deliberate under-merging is the
intended behaviour.

Every identifier the registry holds is traceable to the rule that put it
there. A companion table, `person_identity_source`, carries one row per
identifier assertion — 152,653 of them — naming the resolution method, the
evidence, the source URL, and a confidence rating. A wrong identifier can
therefore be traced back to the rule that produced it rather than being
silently overwritten.

Refusals are handled asymmetrically, and it is worth knowing which is
which. Identifier *conflicts* are written into `person_identity_source`
as first-class rows. Identifier *resolution refusals* — a name that could
not be tied to one author safely — are reported by the resolver and left
as a null column; they are not rows in this table. So an absent ORCID on a
registry row does not carry its own explanation, and "why does this person
have no ORCID?" is answered from the resolver's run output rather than
from the database.

## Two tiers, and what the site actually shows

The application queries parquet files directly in the browser; there is
no query server. Everything the interface can see has to be downloaded
to the reader's machine first. A 152,008-row researcher table with full
bibliometrics is not something to hand to a browser on page load.

So the registry is tiered:

| Tier | Rows | Where it lives |
|---|---:|---|
| `core` | 10,000 | Shipped to the site; queryable in the browser |
| `archive` | 142,008 | Local catalogue only |

**The site shows the core tier and only the core tier.** This is the most
important thing to understand about any figure you read off a registry
view. A count of researchers in a country, a topic, or a facility is a
count within those 10,000 rows, not within the field.

Nor is `core` a random sample, so it cannot be treated as one and scaled
up. Rows were ranked on percentile ranks of coastal output volume,
h-index, recent citation impact, and collaboration degree, and the top
10,000 were taken. Percentile ranks rather than raw values, because
ranking on raw counts put prolific generalists with large non-coastal
output above genuine coastal specialists. The two tiers therefore differ
systematically, exactly as intended:

| Measure | `core` | `archive` |
|---|---:|---:|
| Rows | 10,000 | 142,008 |
| Mean h-index | 39.5 | 10.6 |
| Mean coastal output volume | 94.9 | 13.7 |
| Countries represented | 118 | 201 |

Read that table as a warning about selection, not as a finding about
researchers. The core tier is the high-visibility end of a
citation-weighted ranking. Early-career researchers, researchers
publishing in languages and venues OpenAlex indexes less completely, and
researchers at institutions with smaller publication throughput are
systematically more likely to be in the archive tier. The 84 countries
that appear in the archive tier but not the core tier are not absent
from coastal science.

One exception is deliberate: all 618 identities that were in the registry
before the field-wide harvest — the facility staff, the project team, and
the curated roster — are pinned into `core` regardless of score. A
metric threshold must never drop the roster the site exists to show.

### Cohort membership in what ships

Three boolean columns record which cohorts a person belongs to, and a
person can carry more than one flag at once. In the 10,000 shipped rows:

| Flag | Rows in `core` |
|---|---:|
| `is_scholar` — field-wide roster | 9,824 |
| `is_site_personnel` — staffs a catalogued facility | 173 |
| `is_team` — on the COD organisational chart | 14 |

Eleven people in the full registry carry more than one flag — several are
simultaneously facility staff and roster scholars, a fact none of the
three source tables could represent on its own. That overlap is the
reason the registry exists.

Identifier coverage within the shipped tier: 9,309 rows carry an ORCID,
9,996 an OpenAlex author id, 9,447 a ROR-identified affiliation, 219 a
homepage URL, and 12 a Google Scholar id.

## Links from researchers to catalogued sites

Facilities previously had no persistent organisation identifier, so a
researcher's affiliation — which OpenAlex reports with a ROR id — had
nothing to join against. A `ror` column was added to `facilities`, and
`registry_facilities` holds the resulting researcher-to-site links,
joined on ROR equality alone. No name matching is involved here either.

Coverage is partial and should be read as a floor:

- 69 facilities have a resolved ROR, out of the 210 records that are
  research organisations. The remaining organisations were not attempted
  rather than found to have none — the identifier lookup ran out of API
  budget, and re-running it extends coverage with no other change.
- The 3,309 protected-area records — state parks, refuges, wilderness
  units, aquatic preserves — are excluded by design and hold no ROR. A
  protected area is a place, not an organisation; it will never hold a
  ROR, and its null column is correct.
- 1,467 researcher-to-site links exist across 39 sites in the local
  catalogue. Only 263 of them, across 26 sites, reach the site, because
  the other 1,204 point at archive-tier researchers who are not shipped.

An absent link therefore means one of three different things — the
facility has no ROR yet, the researcher's affiliation is elsewhere, or
the researcher is in the archive tier — and the interface cannot
distinguish them for you.

## Data caveats

These are properties of the data, not defects awaiting a fix. Anything
you build on the registry needs to account for them.

### Coastal output volume is an upper bound, not a paper count

`coastal_works_count` is the sum of a researcher's work counts across
the coastal topic set. OpenAlex assigns a work to *every* topic it carries, so a
paper spanning three coastal topics is counted three times in that sum.
The number is the best available signal of **coastal output volume**, and
it is useful for ranking, but it is not a count of distinct papers and
must not be labelled as one. The build recorded the scale of that
double-counting directly: before the value was clamped to the researcher's
own total work count, it exceeded that total for 10,571 rows, reaching
roughly three times it at the extreme. The shipped column is clamped, so
the inflation is no longer visible in the data — but it is still in the
measure.

`coastal_share`, derived from it, inherits the same inflation and should
be read the same way.

### The co-publication graph covers a subset of the nodes

`registry_collaborations` holds 5,300 co-publication edges. Building the
graph over all 152,008 identities would require on the order of 152,000
publication queries against an external API, so it was computed over the
618 identities that were in the registry before the field-wide harvest —
the COD-relevant subgraph.

The practical consequence: **510 nodes carry at least one edge.** The
other 9,490 of the 10,000 shipped rows have none. For nearly every
core-tier researcher, an empty collaboration list means *the graph has
not been computed for this person*, not *this person has no
collaborators*. Those two statements are entirely different and must
never be conflated — an interface that renders the first as the second is
reporting an artifact of compute budget as a finding about a
researcher's career.

The same asymmetry propagates into tier scoring: collaboration degree
contributes to a row's score only for nodes that were in the registry
before the harvest, and is silently zero for the rest.

### Two ORCID conflicts are logged, not resolved

Two identities exist where an external source reports a different ORCID
than the registry holds. Neither has been applied. Both sit in
`person_identity_source` with `field = 'orcid-conflict'`, awaiting human
curation. Overwriting an identifier on the strength of one source
disagreeing with another is exactly the operation that causes
wrong-person attribution.

### Affiliation strings are whatever the source last attached

`affiliation` is the display string from the OpenAlex author record,
reproduced as-is. It is sometimes wrong, or a department name where an
institution is expected, even when the bibliometrics on the same row are
correct. `affiliation_ror`, where present, is the more reliable field and
is what the facility join uses. Treat the free-text string as a label,
not as evidence.

### Google Scholar ids are effectively absent

Twelve of 152,008 rows carry one. OpenAlex does not populate a Scholar
identifier for most authors, and there is no other free deterministic
source, so this is a property of the upstream data rather than a gap in
the pipeline. Filling the column would require a different source or a
hand-curation pass.

### Career-stage fields are unreliable at the tails

`first_pub_year` comes from the earliest work on the author record, and a
small number of records carry implausible values — years in the 1800s
arising from a mis-dated indexed work. Any analysis keyed on career stage
should filter these rather than assume the column is clean.
`two_yr_mean_citedness` measures recent citation impact, which is a proxy
for recent activity and not a measure of recent output volume.

## Where this is defined

The table definitions, including the reasoning above in comment form,
are in
[`schema/schema.sql`](https://github.com/tyson-swetnam/cod-kmap/blob/main/schema/schema.sql).
The registry and its companion tables are built by
`scripts/build_person_registry.py`, tiered by
`scripts/rank_person_registry.py`, given a co-publication graph by
`scripts/compute_registry_collaborations.py`, and linked to facilities by
`scripts/link_registry_facilities.py`. `scripts/qa.py` enforces the
invariants described here — identifier uniqueness, at least one
persistent identifier per row, provenance for every assertion — and is
the gate a rebuild has to pass.

The full 152,008-row population is exported to `db/parquet/`; only the
core tier and the links whose endpoints ship are mirrored to
`public/parquet/`, which is what the browser reads.
