# Funding data — sources and methods

cod-kmap aims to record every meaningful source of funding for each
of the ~210 coastal facilities in the dataset — federal, state,
private, foundation, charitable — for fiscal years **2015 through
2024**, with per-award and per-fiscal-year granularity. Every funding
record carries a confidence label (high / medium / low) and a
verifiable source URL.

## What's in the database

Funding flows through three tables:

- **`funders`** — one row per granting body (NSF, NOAA, EPA, USGS,
  NIH, Walton Family Foundation, etc.) with type, country, and URL.
- **`funding_events`** — one row per (funder, facility, award, fiscal
  year) with the dollar amount, award identifier, programme name,
  reporting source, source URL, and confidence rating.
- **`funding_links`** — a backwards-compatible view over
  `funding_events` for older queries.

Confidence ratings are applied at row-write time:

- **High** — primary federal source (NSF Awards API, USAspending.gov)
  or audited financial filing (Form 990).
- **Medium** — agency budget book line items, transcribed by hand
  from a Congressional Justification PDF or annual report.
- **Low** — press-release dollar amounts, infographic figures, or
  derivative summaries.

## Data sources

### Federal grant-making (already loaded)

**NSF Awards API.** Hits `api.nsf.gov/services/v1/awards.json` per
facility, filtered by quoted-phrase awardee name and (optionally)
programme name or keyword. Each award's per-fiscal-year obligation
list becomes one event row — no estimation, exact figures from NSF.
Coverage today: 12 facilities, 261 events, $133.4M.

Some facilities still need awardee-name verification before NSF
returns matches — typically university-hosted marine labs whose
awards are billed under the parent campus, not the lab itself.

### Federal grant-making (next)

**USAspending.gov** covers every federal grant *outside* NSF — NOAA,
EPA, DOI/USGS, DOD, DOE, NIH. Records include award ID, recipient
name, award amount, description, awarding agency, programme code,
and period of performance.

Expected coverage: roughly 75 of the 210 facilities, with overlap
against the NSF set.

### Non-profit financial filings

**ProPublica Nonprofit Explorer (Form 990).** Covers the 33 US
non-profit facilities (Hakai, Dauphin Island Sea Lab, Mote Marine
Laboratory, etc.) plus the 3 foundations. Each Form 990 reveals total
revenue, total expenses, net assets per fiscal year, and (when
disclosed) major contributors and grants disbursed.

### Agency-internal allocations

Most of the 76 US federal facility units don't appear in
USAspending: they are intramural NOAA, EPA, NPS, USGS, or USACE
programmes funded via line-item appropriations. Examples include the
Channel Islands National Marine Sanctuary's annual operating budget,
Long Island Sound Study NEP's EPA award, and NPS coastal-park
resource-management allocations.

We populate these from agency budget books:

- NOAA NOS budget rollouts (annual Congressional Justification PDFs).
- EPA NEP funding history.
- NPS Green Book (annual Congressional budget request).
- USGS budget justifications.

### State, foundation, and non-US

A long tail of smaller programmes covers state marine labs (8
facilities), international institutes (DFO Canada, Mexican and Latin
American institutes, Hakai BC — ~15 facilities), and private
foundations whose 990s aren't on ProPublica. Most of these are
populated by hand against agency websites in the relevant language.

## Validation

Before publishing each funding-data update, we cross-check against
the published agency-programme totals to confirm the rows roll up
correctly:

- NSF LTER programme: ~$28M / year
- EPA NEP total annual budget: ~$30M (28 NEPs ≈ $1M each)
- NOAA Sea Grant national network: ~$80M / year split across 33
  programmes
- ONR / DARPA / NASA earth-science marine awards: ~$50M / year
  combined

A separate sanity check ensures no single facility sums to more than
$500M / decade — only the largest ocean institutes plausibly exceed
that.

## Where the data lives in the UI

The **Network** tab uses funder identity as one of the optional
groupings. Each facility's card lists its funders with cumulative
amounts. The **Stats** tab shows the per-research-area funding
distribution. The **SQL** tab exposes the raw `funding_events` table
for ad-hoc analysis.
