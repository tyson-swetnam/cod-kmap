# Funding research pipeline — plan of record

Goal: identify, factually and verifiably, every meaningful source of
funding for each of the ~210 cod-kmap facilities — federal, state,
private, foundation, charitable, fundraising — for **FY2015 through
FY2024**, with **per-award + per-fiscal-year granularity** and **mixed
confidence labels** (high / medium / low) attached to every row so
users can audit upstream.

This is a multi-phase effort. The schema (`funding_events`,
`funding_links` view, `funders`) was already designed for it. What's
new is the ingestion + research pipeline.

---

## Phase 1 — automated pulls from federal data APIs

These cover the ~90 of 210 facilities whose funding flows through
federally-tracked grant systems.

### 1a. NSF Awards API  ✅ **shipped (commit 784b347)**

Script: `scripts/fetch_funding_nsf.py`
Override CSV: `data/funding_overrides/nsf_recipient_overrides.csv`

What it does:
- Hits `api.nsf.gov/services/v1/awards.json` per facility, filtered by
  quoted-phrase `awardeeName` + optional `fundProgramName` + `keyword`.
- Each award's `fundsObligated[]` list (e.g. `"FY 2020 = $1,127,000.00"`)
  becomes one `funding_events` row per fiscal year — exact, no
  estimation.
- Confidence: `high` (primary source).
- Idempotent via `event_id = hash(funder|facility|award|fy)`.

Coverage so far: 12 facilities, 261 events, $133.4M total.

**Open items:**
- ~7 LTER sites still return 0 because their NSF awardee names need
  verification (`Beaufort Lagoon LTER` → maybe "University of Texas at
  Austin" not "UT Marine Science Institute"; `NTL LTER`; `NES LTER`).
  Each needs a 30-second look at https://nsf.gov/awardsearch/ to
  pin the actual awardee.
- University-hosted marine labs (FHL, HMSC, HIMB, HMS) need PI-based
  search: walk `people.openalex_id` → match to NSF PI → list awards.

### 1b. USAspending.gov  ⏳ **next**

Script: `scripts/fetch_funding_usaspending.py` (TODO)
Endpoint: `POST /api/v2/search/spending_by_award/`
Award type codes: `["02","03","04","05"]` (block / formula / project
grants + cooperative agreements).

Why it matters:
- Captures **NOAA, EPA, DOI/USGS, DOD, DOE, NIH** grants — i.e. every
  federal funder *except* NSF, which has its own API.
- Returns `Award ID`, `Recipient Name`, `Award Amount`, `Description`,
  `Awarding Agency`, `Funding Agency`, `CFDA Number`,
  `Period of Performance Start/Current End Date`,
  `generated_internal_id` (used to build the citable source URL).
- Pagination via `page` + `limit` (max 100/page).

Caveats discovered while probing:
- `Period of Performance Start Date` comes back as `null` in
  `spending_by_award/` results — need a follow-up call to
  `/api/v2/awards/<generated_internal_id>/` to get exact dates and the
  per-FY transaction breakdown.
- Recipient name disambiguation is the same problem as NSF: the
  recipient is usually the parent org, not the facility. Reuse the
  override CSV pattern.

### 1c. ProPublica Nonprofit Explorer (Form 990s)  ⏳ **next**

Script: `scripts/fetch_funding_990.py` (TODO)
Endpoint: `https://projects.propublica.org/nonprofits/api/v2/...`

Covers the 33 US `nonprofit` facilities (Hakai, MBARI partner orgs,
Dauphin Island Sea Lab, Mote Marine Lab, etc.) plus the 3 `foundation`
facilities. Form 990s give:
- Total revenue, total expenses, net assets per fiscal year
- Top contributors (Schedule B, often redacted on public copies)
- Major grants disbursed (Schedule I)

Confidence: `high` for IRS-filed numbers; `medium` for Schedule-B
contributor extracts where amounts are bracketed.

---

## Phase 2 — agency-internal allocations (web research + budget books)

Most of the 76 US `federal` facility units are **not** USAspending
recipients — they are intramural NOAA / EPA / NPS / USGS programs
funded via line-item appropriations. Examples: Channel Islands
National Marine Sanctuary's annual operating budget, Long Island
Sound Study NEP's EPA award, NPS coastal park resource-management
allocations.

Sources:
- NOAA NOS budget rollouts (annual Congressional Justification PDFs)
- EPA NEP funding history page (https://www.epa.gov/nep)
- NPS Green Book (annual congressional budget request)
- USGS budget justifications

Approach:
- Delegate to researcher subagents in batches of 10-20 facilities.
- Each subagent: web-search the program name + "FY2024 budget" /
  "annual operating budget" / "Congressional appropriation"; transcribe
  to CSV with `source_url` and `confidence` per row.
- Confidence: `medium` for agency budget book line items, `low` for
  press-release dollar amounts.

---

## Phase 3 — state + foundation + non-US

Smaller facility counts; mostly hand research:
- 8 US `state` facilities (state marine labs) — state agency budgets
- ~15 international (DFO Canada, Mexican / Latin American institutes,
  Hakai BC) — agency websites in English / Spanish / Portuguese /
  French; CIHR, NSERC, Canada Foundation for Innovation public
  databases for Canada
- Private foundations whose 990s aren't on ProPublica (rare)

---

## Validation checklist (per pass, before commit)

- [ ] Spot-check 20 random rows by clicking `source_url` — does the
      cited primary source confirm the amount + year?
- [ ] Aggregate by funder → does the total roll up to a published
      agency-program total?
  * NSF LTER program total budget ~$28M/yr
  * EPA NEP total annual budget ~$30M (28 NEPs ≈ $1M/each)
  * NOAA Sea Grant national network ~$80M/yr split across 33 programs
  * ONR / DARPA / NASA earth-science marine awards ~$50M/yr combined
- [ ] Check `confidence` distribution — should be ~70% high, 25% medium,
      5% low. If `low` exceeds 15%, downgrade those rows to `low` until
      a primary source is found.
- [ ] No facility should sum to >$500M / decade (sanity check; only the
      ~3 largest ocean institutes plausibly exceed that).
