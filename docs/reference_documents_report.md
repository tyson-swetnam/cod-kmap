# Reference documents — what we read and what we kept

The Coastal Observatory Design (COD) team has compiled a substantial
body of background material — coastal-zone definitions, prior
observing-network surveys, NSF Mid-scale guidance, organisational
diagrams. This page summarises what is in that reference set and how
each piece informs cod-kmap.

## Source material

| Group | What it contains |
|---|---|
| **Coastal definition documents** | The Coastal Zone matrix, the Coastal Graphic v4.0, and the PCAST Earth-Observation Interoperability v2.2 — together they define the coastal critical zone we are scoping. |
| **2018 Coastal Observatory Landscape survey** | 32 community responses listing every coastal observing network the community knew about, with measured systems, scale, scope, and US / international reach. The seed for the cod-kmap `networks` and `facilities` tables. |
| **MSI handouts (2022, 2024, 2026)** | Successive proposal drafts; show how the pitch evolved over four years. |
| **NSF references** | Mid-scale R2 solicitations NSF 19-542 and NSF 21-537, the PAPPG, the EVMS Gold Card, and the Research Infrastructure Guide NSF 21-107. They define the funding mechanism and reporting rules cod-kmap aligns with. |
| **Reference images and diagrams** | Finkl 2004 Coastal Classification, NAS 2020 *Environmental Science in the Coastal Zone*, the Six-Sigma Network → Research Infrastructure transition diagram, and the Requirements Flowdown image. They give us a formal coastal taxonomy and the design lifecycle. |
| **WATERS Network documents (2006–2009)** | The full Science, Education, and Cyberinfrastructure plans for WATERS, the previous attempted national aquatic observatory that was never built. Useful as a lessons-learned source. |
| **Borer et al. 2020** (BioScience) and **Sci. Adv. 2022** | Two flagship papers on integrated coastal observation and on the critical-zone framing. |

## Organisational structure

The Clemson CCZO 2024 organisational chart and the Design Flow
diagram together describe a five-track work-breakdown structure:

- **1.0 COD Management & Support** — PI Skip Van Bloem (Clemson) +
  Deputy Hank Loescher (Battelle), with governance led by
  S. Whitmire.
- **2.0 Science Management** — Chief Scientist Allison Myers-Pigg
  (PNNL), with six Integrated Project Teams: Physical Environment,
  Social Dimension, Biotic Environment, Integrative Design, Coastal
  Informatics, and Built Environment.
- **3.0 Broader Impacts Management** — led by Christie Staudhammer,
  with a STEM Workforce pipeline integrating NEON, OBFS, and
  Clemson programmes.
- **4.0 Cyberinfrastructure** — co-CIs Nirav Merchant and Tyson
  Swetnam (University of Arizona), covering data ingest, processing,
  discovery, and the workbench design.
- **5.0 Prototype Infrastructure** — Engineering Manager K. Nitschke.
- **6.0 Project Management Office** — C. Ritz, PMP.

External community partners named in the chart include LTER, NEON,
OOI, WHOI, Scripps, RDA, EDI, AGU Biogeochem, EREN, USGCRP working
groups (IWGs and SOCCR), NOAA Earth System Research Lab, NASA
Coastal, Environmental Defense Fund, Woodwell Climate Research
Center, and the State Climate Action Alliance.

## Design lifecycle

The Design Flow document describes an iterative cycle:

1. The Science Leadership Committee and Science IPTs distil the
   Grand Challenge questions into specific data and product needs:
   what we have, what is missing, where the spatial and forecast
   gaps are.
2. The Cyberinfrastructure team captures, integrates, and harmonises
   coastal datasets across time, space, and discipline, with AI/ML
   support for synthesis.
3. A tailored AI environment with semantic ontologies and controlled
   vocabularies provides web services, open-source software, and
   ISO-compliant DOIs and provenance tracking.
4. A public data portal exposes the integrated dataset with Jupyter
   notebooks, R libraries, Docker, GitHub, CodeLabs, and Python
   packages.
5. The Draft Coastal Observatory Design goes through public community
   review, final concurrence, and acceptance, producing an
   implementation-ready package (project execution plan, total
   construction cost budget, resource-loaded baseline schedule,
   staffing plans, risk register, ConOps, decommissioning plans, and
   the Critical Design Review).

The Design Flow names the existing curated coastal datasets the
observatory should integrate: MarineGEO, NERR, LTER, OOI,
Neptune-Coastal, CODISS, Coastal Change Analysis Program, Coastal
Zone Management, Digital Coast, SCCOOS, the Coastal Data Information
Program (CDIP), the Sea Level Change Portal, the National Coastal
Condition Assessment, and the Integrated Ocean Observing System
(IOOS).

## What the 2018 survey changed in cod-kmap

The 2018 community survey listed 29 distinct observing networks.
Cross-referencing against cod-kmap's 32-network catalogue:

- **Already represented:** IOOS, SECOORA, LTER, NERR, USGS
  streamgauging, NDBC, MarineGEO, NWLON, CBIBS, NCCA, NPS coastal.
- **Added or scheduled:** AmeriFlux, the Coastwide Reference
  Monitoring System (CRMS, Louisiana), Gulf of Maine Ocean Observing
  System, the National Atmospheric Deposition Program, the Gulf of
  Mexico Hypoxia Watch, GEO-BON, the Mexican IMECOCAL, the Colombian
  REDCAM, Germany's COSYNA, Chesapeake Bay water and SAV monitoring,
  and the Coastal Carolina Nearshore monitor network.

## How the reference set drives the data model

The reference materials inform several extensions to the cod-kmap
schema, listed here in priority order.

### A coastal-zone strata table

The Coastal Zone matrix and Finkl 2004 classification define the
coastal critical zone along multiple axes. cod-kmap captures these
as a per-facility table:

```sql
CREATE TABLE coastal_zone_strata (
  facility_id   VARCHAR REFERENCES facilities(facility_id) PRIMARY KEY,
  zone          VARCHAR,    -- littoral, sublittoral, inner-shelf, mid-shelf, outer-shelf, abyssal
  shore_type    VARCHAR,    -- rocky, sandy, muddy, deltaic, mangrove, coral, kelp, tidal-flat, salt-marsh
  river_status  VARCHAR,    -- riverine, estuarine, tidal-river, none
  built_status  VARCHAR,    -- urban, suburban, rural, natural
  ocean_basin   VARCHAR,    -- N-Atlantic, S-Atlantic, N-Pacific, S-Pacific, Arctic, Caribbean, Gulf-of-Mexico, Great-Lakes, Hudson-Bay
  finkl_class   VARCHAR     -- one of Finkl 2004's 21 coastal classes
);
```

### IPT mapping for research areas

Each research area is tagged with its corresponding Integrated Project
Team — Physical Environment, Biotic Environment, Coastal Informatics,
Social Dimension, Integrative Design, or Built Environment — so the
Stats and Network views can filter by IPT.

### Curated coastal datasets

The 14 datasets named in the Design Flow are tracked in a dedicated
`coastal_datasets` table and linked back to the facilities that
contribute. This lets cod-kmap show "which COD partner stewards this
dataset?" alongside the facility view.

### Partner organisations

A `partner_organisations` table catalogues the named external
partners (universities, networks, agencies, NGOs, working groups) with
their COD role: science leadership, data source, workforce pipeline,
or governance.

### Grand Challenge framing

The four Grand Challenge questions (Challenging Theory, Societal
Responses, Coastal Vulnerability, Uncertainties in Coastal Ecosystem
Processes) are embedded in the Stats and Network views: each research
area surfaces which Grand Challenge it contributes to.

### COD team in the People directory

The ~30 named people in the organisational chart appear in the
researcher directory with their COD role explicitly tagged, and are
filterable via a "COD Team" facet in the People view.

## Lessons from WATERS

The WATERS Network was the prior attempt at a national aquatic
observatory. Its 2009 Science Plan was reviewed by the National
Research Council, and the network was never built — primarily
because of cost overruns and scope creep. Two design choices in COD
explicitly avoid that path:

- **AI/ML and cyberinfrastructure are core, not bolt-on.** WATERS
  treated cyberinfrastructure as a downstream dependency; COD treats
  it as Track 4 with its own work-breakdown structure.
- **Phased, distributed prototyping** rather than a single
  large-scale build. Each Integrated Project Team can deliver a
  working prototype before the full observatory commits to a final
  design.

## What the reference set explicitly leaves alone

cod-kmap's existing taxonomies — research areas, networks, facility
types — are kept as authored. The IPT mapping and other extensions
above are additive, not replacements. The OpenAlex and publication-
topic crosswalks are independent of the proposal-document review and
remain unchanged.
