# Plan — closing the personnel gap (48 facilities)

After the round-1 personnel research (commits up to `e6e7dc6`,
173 verified key personnel across 162/210 facilities = 77% coverage),
**48 facilities still have zero rows in `facility_personnel`** and
therefore appear as isolated nodes in the knowledge graph — no
edges to people, networks, areas via personnel, or co-author
collaborations.

This plan groups them into 7 research batches, identifies the
authoritative source per batch, delegates each batch to a researcher
subagent, and ingests the results via the existing
`scripts/load_facility_personnel.py` loader.

## Coverage gap by group

| Group | Count | Source priority |
|-------|------:|-----------------|
| NOAA NERR reserves | 10 | Each NERR's own website has a "Reserve Manager" + "Research Coordinator" page; NERRA member directory |
| EPA NEPs | 5 | Each NEP's own website has a "Director" page; NEP Program Director list |
| NSF LTREB sites | 4 | NSF Award Search for the LTREB award_id → PI name |
| Fisheries and Oceans Canada (DFO) | 4 | Each institute's "About Us" page; DFO regional director list |
| IOOS Regional Associations | 2 | Each RA's "About / Leadership" page |
| Latin American federal institutes | 4 | CICESE, INIDEP, INAPESCA, ICML-UNAM directorate pages (Spanish) |
| Latin American / Caribbean universities | 4 | CIMAR, IO-USP, UWI-CERMES, UWI-PRML directorate pages |
| US federal HQs + singletons | ~14 | NCCOS, GLERL, NRL-SSC, FRF, NASA-OBPG, etc — agency director pages |
| **Total** | **48** | |

## Per-batch facility lists

### Batch 1: 10 NERRs without personnel

| facility_id | acronym | name |
|---|---|---|
| a90fce0f33af9666 | NERR | ACE Basin NERR |
| 88a71bec0521cfdb | NERR | Apalachicola NERR |
| 9b4911f8fe1a9ebd | NERR | Delaware NERR |
| acfc1994d0e6cc5e | NERR | Guana Tolomato Matanzas NERR |
| d2beef812eb53bc7 | NERR | He'eia NERR |
| f01f6bcc9502e905 | NERR | Hudson River NERR |
| bf2c2c67551f218f | NERR | Mission Aransas NERR |
| 4ac94e4e5da49682 | NERR | North Carolina NERR |
| c42a82d1eef0f701 | NERR | North Inlet-Winyah Bay NERR |
| 22defb4a3ee37d8a | NERR | San Francisco Bay NERR |

Approach: each NERR has a "Manager" + "Research Coordinator" page on
its own site; NERRA Member Directory cross-references all 30 reserves.

### Batch 2: 5 EPA NEPs

Albemarle-Pamlico, Coastal Bend Bays & Estuaries, Massachusetts Bays,
Narragansett Bay, San Juan Bay. Each NEP has a "Director" listed on
its program homepage; EPA's NEP page links them all.

### Batch 3: 4 NSF LTREBs

North Inlet, SERC GCREW, Swan's Island, West Falmouth Harbor. Each is
a single-PI NSF Long-Term Research in Environmental Biology award.
NSF Award Search by LTREB program_id + site keyword returns the PI
directly.

### Batch 4: 4 DFO Canada institutes

Bedford Institute of Oceanography (BIO), Maurice Lamontagne Institute
(IML), Northwest Atlantic Fisheries Centre (NAFC), Pacific Biological
Station (PBS). Each has a "Director" published on dfo-mpo.gc.ca.

### Batch 5: 4 Latin American federal institutes

CICESE (Mexico), INIDEP (Argentina), INAPESCA (Mexico), ICML-UNAM
(Mexico). Spanish-language directorate pages on each institute's
own site.

### Batch 6: 4 Latin American / Caribbean universities

CIMAR (Costa Rica), IO-USP (Brazil), UWI-CERMES (Barbados),
UWI-PRML (Jamaica). Mixed Spanish/English/Portuguese pages on each
institute's site.

### Batch 7: 14 US federal HQs + singletons

| facility_id | acronym | name |
|---|---|---|
| a288bd4be2da660c | GLERL | Great Lakes Environmental Research Lab |
| 885e59f7b4a660c3 |  | Hawaiian Islands Sentinel Site |
| 7fd58ccc7effab1d | GSFC-OBPG | NASA Goddard SFC Ocean Biology Processing Group |
| 581719b5d39091b8 | NCCOS | National Centers for Coastal Ocean Science |
| 02a62062148246a9 |  | North Carolina Sentinel Site |
| e133deaf60dd7db1 | SEFSC | Southeast Fisheries Science Center |
| c9c6536a9b20e89c | SPCMSC | St. Petersburg Coastal & Marine Science Center |
| 18254d33b94ff086 | NRL-SSC | US Naval Research Lab, Stennis Space Center |
| 5d3194ffda147bcd | FRF | USACE Field Research Facility (Duck NC) |
| a20ddd52546a3e50 | GCOOS | Gulf of Mexico Coastal Ocean Observing System |
| f3b529d1eda8e073 | LTER | LTER Network Office |
| 48a4a144e3ad245f | NERRS | NERR System (HQ) |
| 5168181cc7acfbfe | Sea Grant | National Sea Grant College Program |
| b186ccd53158c323 | NERACOOS | NE Regional Association of Coastal Ocean Observing |
| 0553e5e87fe9b46f | PIMS | Perry Institute for Marine Science (Bahamas) |
| c4f51733272b5589 | NC-DCM | NC Division of Coastal Management |
| 099f35eb7143a89d | URI-GSO | URI Graduate School of Oceanography |

(17 total — leadership pages on each agency's own site.)

## Delegation contract per batch

Each subagent gets a self-contained prompt with:
1. The facility_id list (verbatim — must not mutate)
2. Output CSV path: `data/seed/facility_personnel_round_2_<batch>.csv`
3. CSV header (matches existing `facility_personnel_seed.csv`):
   `facility_id,role,name,title,is_key_personnel,start_date,end_date,
    source,source_url,confidence,notes,orcid,openalex_id,homepage_url,email`
4. Validation rules (≥1 row per facility, source_url required,
   confidence in {high, medium, low})
5. The "no fabrication" rule: if you can't find a verifiable name,
   leave the facility OUT of the CSV. We'll record the gap; we won't
   fake it.

## Loader

`scripts/load_facility_personnel.py` already exists from the round-1
work. It reads each CSV, ensures person + facility_personnel rows,
and is idempotent via primary-key dedup. No new code needed.

## Order of operations

1. Delegate batches 1-3 first (US-only, public-records only — fastest
   research)
2. Then 4-6 (international, requires Spanish/Portuguese/French)
3. Then 7 (singletons — most varied, slowest per-row)
4. Run `python scripts/load_facility_personnel.py` after each batch
   completes
5. Re-export parquet (`scripts/init_people_tables.py --export-parquet`)
6. Commit + push

## Expected outcome

If we hit the same ~85-90% per-batch success rate as round 1:
- 48 starting → 5–8 still-empty after research
- ~85-100 new `facility_personnel` rows
- ~50-80 new `people` rows (some directors hold roles at multiple
  facilities — e.g. NERRA networks)
- Coverage moves from 162/210 (77%) to ≈ 200-205/210 (95-98%)

The remaining ~5 facilities will be ones whose websites genuinely
don't list named leadership (some sentinel sites, some virtual
networks). Marking those is honest; fabricating directors is harmful.
