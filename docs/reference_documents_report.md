# Reference_Documents/ → cod-kmap update report

This report reads the original Coastal Observatory Design (COD)
proposal-era reference materials in `Reference_Documents/` plus the
two organisational diagrams the user just sent (`Clemson CCZ ORG
Structure 09292024.pptx` and `Design Flow.pptx`) and identifies
**concrete updates needed in cod-kmap data + UI** so the tool
better mirrors the original proposal design.

It is not a verbatim summary of every PDF — instead it pulls the
*decisions, taxonomies and survey responses* that should change our
schema, vocabularies, and views.

## What's in `Reference_Documents/`

| Group | Highlights |
|-------|-----------|
| **Coastal_Definition_Docs/** | The "Coastal Zone matrix" docx + "Coastal Graphic v4.0" pptx + PCAST Earth-Observation Interoperability v2.2 — these define **what counts as the coastal critical zone**. |
| **Current_Coastal_Observatory_Landscape_Documents_RESPONSES.xlsx** | 32 survey responses (May 2018) listing every coastal observing network the community knew about, including measured systems, scale, scope, US/intl. **The seed for our `networks` and `facilities` tables.** |
| **MSI Handouts (2022)** | Earlier proposal versions; show how the pitch evolved from 2022 → 2024 → 2026. |
| **NSF_References/** | Mid-scale R2 solicitation NSF 19-542 → NSF 21-537, NSF PAPPG, EVMS Gold Card, Research Infrastructure Guide NSF 21-107. **Defines the funding mechanism + reporting rules cod-kmap must align with.** |
| **Reference_Images_Diagrams_Docs/** | Finkl 2004 Coastal Classification, NAS 2020 Environmental Science in the Coastal Zone, Six-Sigma Network → RI transition diagram, Requirements Flowdown image. **Source for a formal coastal taxonomy + the design lifecycle.** |
| **WATERS reference documents/** | The full WATERS Network science / education / cyberinfrastructure plans (2006-2009) — the previous attempted national aquatic observatory that **was not built**. Lessons-learned source. |
| **biaa034.pdf, sciadv.abl9155.pdf** | Two flagship papers (Borer et al. 2020 BioScience integrated coastal observation framework; Sci Adv 2022 critical-zone paper). |

## What's in the two new pptx diagrams

### `Clemson CCZ ORG Structure 09292024.pptx`

A formal **work-breakdown structure** for the COD organisation:

  - 1.0 COD Management & Support → PI **Skip Van Bloem** + Deputy
    **Hank Loescher**, governance led by S. Whitmire
  - 2.0 Science Management → Chief Scientist **Allison Myers-Pigg**
    with **6 Integrated Project Teams** (IPTs):
       * 2.1.1 Physical Environment  (Greg Starr)
       * 2.1.2 Social Dimension       (Craig Landry, Reed Goodman)
       * 2.1.3 Biotic Environment     (Austin Gray, Jim Anderson)
       * 2.1.4 Integrative Design     (Hank Loescher)
       * 2.1.5 Coastal Informatics    (TBD)
       * 2.1.6 Built Environment      (Sarah Waikowski)
  - 3.0 Broader Impacts Management → Christie Staudhammer
       * 3.3 STEM Workforce Pipeline (T. Maslak) with sub-pipelines
         3.3.1 NEON, 3.3.2 OBFS, 3.3.3 Clemson
  - 4.0 **Cyber Infrastructure** → co-CIs **Nirav Merchant + Tyson
    Swetnam** (UA), with 4.1-4.5 covering data ingest, processing,
    discovery / workbench design, and cyberinfrastructure IPT.
  - 5.0 Prototype Infrastructure (K. Nitschke, Engineering Mgr)
  - 6.0 Project Management Office (C. Ritz, PMP)
  - **External Community partners** explicitly named:
       * Universities: Alabama, Arizona, Battelle, PNNL, Clemson,
         Virginia Tech, Delaware, Georgia, OBFS/UCSB, USC
       * Networks/orgs: LTER, RDA, EDI, AGU Biogeochem, EREN, OOI,
         NEON, WHOI, Scripps
       * Agencies: USGCRP (IWGs and SOCCR), NOAA Earth System
         Research Lab, NASA Coastal, National Labs
       * NGOs: Environmental Defense Fund, Woodwell Climate Research
         Center, State Climate Action Alliance

### `Design Flow.pptx`

The **iterative design lifecycle** for the COD:

  1. **Science Leadership Committee** + Science IPTs distill Grand
     Challenge questions → "What data / data products do we have,
     what is missing? Where are spatial/forecast gaps? What are the
     knowledge gaps?"
  2. **Cyberinfrastructure** captures, integrates, harmonises coastal
     datasets across time/space/discipline; uses AI/ML to query and
     synthesise the design-application questions.
  3. **Tailored AI Environment** with semantic ontologies / controlled
     vocabularies; new web services, open-source software, ISO 11915
     DOIs and provenance tracking.
  4. **Public Data Portal** (proof-of-concept) for external research
     community, with Jupyter notebooks, R libraries, Docker, GitHub,
     CodeLabs, Python libraries.
  5. **Draft Coastal Observatory Design** → **Public Community Review** →
     **Final Concurrence/Acceptance** → Implementation-ready design
     (PEP, TCP budget, resource-loaded baseline schedule, staffing
     plans, risk registry, ConOps, decommissioning plans, CDR/CR).

It also names the **existing curated coastal datasets** the
observatory should integrate:
  - MarineGEO
  - National Estuarine Research Reserve (NERR)
  - Coastal Long-Term Ecological Research (LTER)
  - Ocean Observation Initiative (OOI)
  - Neptune – Coastal
  - Coastal Observing Data and Information Sharing for Security (CODISS)
  - Coastal Change Analysis Program
  - Coastal Zone Management
  - Digital Coast
  - Southern California Coastal Ocean Observing System (SCCOOS)
  - Coastal Data Information Program (CDIP)
  - Sea Level Change Portal
  - National Coastal Condition Assessment (NCCA)
  - **Regional**: Integrated Ocean Observing System (IOOS)

## What the survey responses (32 networks) tell us

The May-2018 community survey listed **29 distinct observing
networks**. Cross-referencing against cod-kmap's current `networks`
table (32 networks):

✅ **Already in cod-kmap**: IOOS, SECOORA (regional IOOS), LTER, NERR
(NEERS in survey), USGS streamgauging, NDBC, MarineGEO, NWLON,
CBIBS, NCCA, NPS coastal — these are well represented.

❌ **Missing or under-represented in cod-kmap** (action items):
  - **AmeriFlux** — flux tower network with ~5 coastal sites; should
    be a `networks` row of its own and have those 5 sites linked.
  - **Coastwide Reference Monitoring System (CRMS)** — Louisiana
    state network with hundreds of monitoring stations; not in cod-kmap.
  - **GoMOOS** — Gulf of Maine Ocean Observing System (a regional
    IOOS we don't list separately).
  - **NADP (National Atmospheric Deposition Program)** — relevant for
    coastal biogeochemistry but not in cod-kmap.
  - **Gulf of Mexico Hypoxia Watch** — NOAA NCEI program, missing.
  - **Coastal Carolina Nearshore monitor network** — state-level,
    high spatial detail, missing.
  - **GEOBON** — Group on Earth Observations Biodiversity Observation
    Network; international, missing.
  - **IMECOCAL** (Mexico) and **REDCAM** (Colombia) — international
    Latin American networks; should be added under
    `international-federal` facility_type.
  - **COSYNA** (Germany) — international peer; useful for "what does
    a peer national observatory look like" comparison.
  - **Chesapeake Bay SAV Monitoring**, **Chesapeake Bay Water
    Monitoring Program** — regional EPA-state programs not yet in
    cod-kmap as networks (though individual facilities may be).

## Concrete cod-kmap updates recommended

### 1. Add a formal `coastal_zone_strata` taxonomy

The "Coastal Zone matrix" docs + Finkl 2004 classification define
the coastal critical zone along multiple axes. We should add a new
`coastal_zone_strata` table:

```sql
CREATE TABLE coastal_zone_strata (
  facility_id   VARCHAR REFERENCES facilities(facility_id) PRIMARY KEY,
  zone          VARCHAR,    -- littoral | sublittoral | inner-shelf | mid-shelf | outer-shelf | abyssal
  shore_type    VARCHAR,    -- rocky | sandy | muddy | deltaic | mangrove | coral | kelp | tidal-flat | salt-marsh
  river_status  VARCHAR,    -- riverine | estuarine | tidal-river | none
  built_status  VARCHAR,    -- urban | suburban | rural | natural
  ocean_basin   VARCHAR,    -- N-Atlantic | S-Atlantic | N-Pacific | S-Pacific | Arctic | Caribbean | Gulf-of-Mexico | Great-Lakes | Hudson-Bay
  finkl_class   VARCHAR     -- one of Finkl 2004's 21 coastal classes
);
```

These are the strata the proposal's **Grand Challenge questions**
implicitly partition coastal data along. Today cod-kmap only
captures `country` + `region` (free text). The MEOW + Köppen-Geiger
ingestion in `docs/suitability_roadmap.md` partly covers this; the
Finkl classification is an additional axis.

### 2. Map cod-kmap research-areas to the 6 IPTs

The org chart has **6 Science IPTs** that match closely but not
identically to our 35 research-areas. Add a column or join table:

```sql
ALTER TABLE research_areas ADD COLUMN ipt VARCHAR;
-- e.g.:
UPDATE research_areas SET ipt = 'Physical Environment'
  WHERE area_id IN ('coastal-processes','sediment-transport',
                    'shoreline-change','climate-and-sea-level',
                    'physical-oceanography');
UPDATE research_areas SET ipt = 'Biotic Environment'
  WHERE area_id IN ('marine-ecosystems','coral-reefs','seagrass',
                    'kelp-forests','mangroves','marine-mammals',
                    'seabirds','salt-marshes','tidal-wetlands');
UPDATE research_areas SET ipt = 'Coastal Informatics'
  WHERE area_id IN ('remote-sensing','ocean-observing-systems');
UPDATE research_areas SET ipt = 'Social Dimension'
  WHERE area_id IN ('marine-policy-and-socio-economics');
-- etc.
```

The Stats and Network views can then add an "IPT" filter so users
can see which facilities + researchers map to each IPT.

### 3. Add the 12+ missing networks from the survey

A new `data/seed/networks_round_2.csv` capturing AmeriFlux, CRMS,
GoMOOS, NADP, GoM Hypoxia Watch, GEOBON, IMECOCAL, REDCAM, COSYNA,
CBIBS, MyMobileBay, ChesBay water + SAV monitoring, etc., with
their member facilities. Loaded via the existing
`scripts/load_networks.py`.

### 4. Add a `partner_organisations` table

The org chart lists **named external partners** (LTER, RDA, EDI,
AGU Biogeochem, EREN, NEON, WHOI, Scripps, NASA Coastal, NOAA ESRL,
EDF, Woodwell, Climate Action Alliance) that cod-kmap should
explicitly catalog as "COD partners" so the dashboard can
distinguish them from generic facilities:

```sql
CREATE TABLE partner_organisations (
  org_id        VARCHAR PRIMARY KEY,
  name          VARCHAR,
  category      VARCHAR,     -- university | research-org | agency | NGO | working-group
  url           VARCHAR,
  cod_role      VARCHAR,     -- science-leadership | data-source | workforce-pipeline | governance
  facility_id   VARCHAR REFERENCES facilities(facility_id)  -- when the org IS one of our facilities
);
```

### 5. Add **Existing Curated Coastal Datasets** as first-class records

The Design Flow doc lists 14 curated datasets the observatory
should integrate. cod-kmap should add a `coastal_datasets` table:

```sql
CREATE TABLE coastal_datasets (
  dataset_id    VARCHAR PRIMARY KEY,
  name          VARCHAR,
  steward_org   VARCHAR,
  url           VARCHAR,
  data_types    VARCHAR,      -- e.g. 'biogeochem,physical,benthic'
  spatial_scope VARCHAR,      -- 'national' | 'regional' | 'global'
  temporal_min  INTEGER,
  temporal_max  INTEGER,
  doi           VARCHAR,
  notes         VARCHAR
);
```

Seed rows: MarineGEO, NERR-SWMP, LTER LNO, OOI, Neptune-Coastal,
CODISS, Coastal Change Analysis Program, Coastal Zone Management,
Digital Coast, SCCOOS, CDIP, Sea Level Change Portal, NCCA, IOOS RA
data products. Each gets linked to the facilities that contribute.

### 6. Add COD team members + roles to the People directory

The org chart has ~30 named people (Skip Van Bloem, Hank Loescher,
Allison Myers-Pigg, Tyson Swetnam, Nirav Merchant, Christie
Staudhammer, Greg Starr, Craig Landry, Reed Goodman, Austin Gray,
Sarah Waikowski, Eric Maclamore, T. Maslak, Conner Philson, Kelly
Lazar, Jim Anderson, S. Whitmire, K. Nitschke, F. Moliac, etc.) who
should appear in the People directory with their COD role
explicitly tagged. New `data/seed/cod_team.csv` with role + IPT
attribution; a dedicated "COD Team" filter in the People view.

### 7. Replace the LTER tagline with the original GC framing

The Knowledge-graph + Stats views currently describe the dataset
neutrally. The proposal's framing centers around the **4 Grand
Challenge questions** (challenging theory, societal responses,
coastal vulnerability, uncertainties in coastal ecosystem
processes). Add a `grand_challenges` table or static doc, and have
each research-area dashboard show which GC question(s) it
contributes to.

### 8. Tag the WATERS-era lessons learned

WATERS Network's **2009 Science Plan** was the prior attempt at a
national aquatic observatory. NRC reviewed it (`NRC review science,
education WATERs 2008.pdf`) and the network was **never built**
because of cost overruns + scope creep. Add a "Lessons-learned"
section in `docs/methods.md` explaining what the COD design avoids.
This isn't a database update but it's a documentation update that
the proposal team should see in the in-app Docs tab.

## Priority order

If we have ~3 days of work to allocate, do them in this order:

1. **Network gaps (item #3)** — adds 12 missing networks, immediately
   visible on the Map + Knowledge graph. Lowest effort, highest
   visible improvement. ~3-4 hours.
2. **COD team + roles (item #6)** — names the people the proposal
   team is working with; high political/community visibility. ~3 hours.
3. **IPT mapping (item #2)** — adds an analytical dimension to all
   existing dashboards. ~2 hours.
4. **Coastal datasets (item #5)** — links cod-kmap to the data
   sources the proposal will integrate. ~1 day if seeded with the 14
   datasets listed in Design Flow.
5. **Coastal-zone strata (item #1)** — bigger schema work + spatial
   overlay; defer until the suitability roadmap (`docs/suitability_roadmap.md`)
   ingestion is done since both want similar layers.
6. **Partner orgs (item #4)** — derived from #2 + #6 mostly.
7. **Grand Challenge framing (item #7)** — UI + content work; touches
   docs.js, stats.js, network.js help blurbs.
8. **WATERS-era doc (item #8)** — pure docs, low urgency.

## What to NOT change

  - Don't replace the existing `research_areas` taxonomy — it works,
    and the IPT mapping in #2 is additive, not a rewrite.
  - Don't reorganize the People directory by IPT — leave the existing
    research-area grouping; add IPT as a filter chip.
  - Don't change the OpenAlex / publication-topic crosswalk —
    independent of these proposal-document updates.
