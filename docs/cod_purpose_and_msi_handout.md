# Coastal Observatory Design — purpose, scope, and how cod-kmap supports it

This document captures the content of the NSF MSI handout
(`MSI Handout_2Page_2026_v2.0_HWL_AMP.pptx`) the team is shopping to
NSF program officers, and explains how the cod-kmap dataset + map
+ dashboards directly support each section of the pitch.

The Coastal Critical Zone Observatory (CCZO) is being scoped under
**NSF Mid-scale R1 Infrastructure Design (24-598)** with a
pre-proposal due **September 1, 2026** and a full proposal due
**February 8, 2027**.

## Why this matters

  - **>40 % of Americans live in coastal counties.**
  - **~50 % of the US economy is based in coastal areas.**
  - Coastal natural, managed, and socioeconomic systems incur
    complex interacting stresses.
  - **There is currently no NSF Coastal Observatory in the US**, even
    though coastal ecosystems underpin the US socio-economic fabric.

## Defined Coastal Critical Zone

The CCZO scope is the *coupled land–water–human interface* — including
terrestrial and built areas near coasts, river deltas, tidal inlands,
and estuaries.

## Proposal Pillars

### 1. Resolve Competing Coastal Research Interests
  - Integrate research and agency programs.
  - Demonstrate value added to other networks.
  - Previous attempts for a coastal observatory failed because of a
    site-based focus, which is not novel.

### 2. AI-Embedded Design Framework
  - Flip the script by having cyber-infrastructure and generative AI
    as **core infrastructure at onset** — not a bolt-on.
  - Multimodal AI identifies spatial, temporal, and data gaps.
  - Prototyping AI guides observatory design and reduces risk.

### 3. Co-Development of Coastal Ecosystem Functions and Social Dimensions
  - Coastal biogeochemical processes; saltwater intrusion + sea-level
    rise; wind effects; flooding; ecosystem functions.
  - Ecosystem services; local-to-regional economies and dependencies;
    policy frameworks to guide decision-making.

### 4. Co-Design Workforce Development
  - Science and research management is multifaceted and supports
    careers in many fields, not just science.
  - Science operations need trained personnel.
  - AI skills needed for data management.
  - Infrastructure operation curriculum.
  - Prototype workforce development in design.

## The AI-Advantage

  - Build upon prior workshops, position papers, knowledge, and
    capabilities.
  - Inclusion of existing networks and agency sites + data.
  - Continental-scale, expert-informed AI-based design to better
    understand changing coastal processes.
  - Inclusion of a social dimension is critical.
  - Workforce pipeline develops cutting-edge skillsets; AI skills will
    be needed for science technicians.
  - Design and prototype a workforce pipeline to support technical
    operations.
  - Led by an experienced team to scope NSF infrastructure.

## Team

  - **Skip Van Bloem** — Clemson University — `skipvb@clemson.edu`
  - **Allison Myers-Pigg** — PNNL — `allison.myers-pigg@pnnl.gov`
  - **Tyson Swetnam** — University of Arizona — `tswetnam@arizona.edu`
  - **Hank Loescher** — Battelle — `hloescher@battelleecology.org`

This effort builds upon a **2019 Clemson Baruch Institute Coastal
Workshop**, a 2022 follow-up meeting, and a 2024 pre-proposal where
a draft conceptual framework for a Coastal Observatory was developed.
Attendance included NSF, NOAA NERRS, DHS, Coast Guard, Sea Grant,
USGCRP IWG, and a broad range of university representation, and
scientific and programmatic expertise.

A core **science team (>40 participants)** and a formal **project
management team (>10 participants)** have met to identify major
needs and questions. The team will grow to include new partnerships,
drawing on experience scoping infrastructure and building
partnerships within NSF (NEON, others) and globally (GERI). It has a
working relationship with the NSF Large Facility Office, R1 Community
of Interest, and the G7 Group of Senior Advisors for Research
Infrastructure. The team employs a State-of-the-Art System
Engineering Approach based on a dynamic model framework.

## Aligned with National Academy Reports and Priorities

  - U.S. Global Change Research Program (USGCRP), 2024 — *Our Changing
    Planet: The U.S. Global Change Research Program for Fiscal Year 2024*
  - **Fifth National Climate Assessment**, 2023
  - NAS Report: *Next Generation of Earth Systems Science* (2021)
  - *Catalyzing Opportunities for Research in the Earth Sciences
    (CORES): Decadal Survey* (2020)
  - *Understanding the Long-Term Evolution of the Coupled
    Natural-Human Coastal System* (2018)

## Grand Challenge Questions

These were distilled from the wide range of planning documents and
will be used to guide the observatory design. Full text and
associated assumptions are available on request.

  - **GC1: Challenging Scientific and Economic Theory.**
    How do scientific and economic theory and observations inform our
    understanding of coastal socio-ecological systems? How can this
    understanding be challenged and improved by a modelled predictive
    capability? How can generative AI inform new observations or other
    required data for improved understandings of the coastal critical
    zone? How do we prepare the future workforce to manage the
    complexity of both socio-ecological systems and technical
    advancements?

  - **GC2: Societal Responses.**
    How do changes in coastal ecosystem functions affect coastal
    economies? How do they scale from local to region? Which markets
    and commodities do they affect and how? How is this considered
    in policy and jurisprudence?

  - **GC3: Coastal Vulnerability.**
    How stable, resilient, and resistant are coastal ecosystem
    processes with natural and anthropogenic changes? How do we
    determine tipping points that would transform a coastal ecosystem
    to a different state? Which coastal processes are particularly
    vulnerable to rapid or sustained changes and how do they scale?

  - **GC4: Uncertainties in Coastal Ecosystem Processes.**
    How will US coastal ecosystems respond to changes in natural- and
    human-induced changes such as saltwater intrusion, extreme events,
    intensifying storms, and inland flooding across a range of spatial
    and temporal scales? How do the feedbacks in coastal processes
    interact with extreme events? How do these feedbacks vary with
    ecological context and spatial and temporal scales?

---

## How cod-kmap supports the proposal

The cod-kmap dataset and web app are an **operational tool for the
"AI-Embedded Design Framework"** pillar — specifically the
"Multimodal AI identifies spatial, temporal, and data gaps" claim.

| MSI handout claim | cod-kmap support |
|-------------------|------------------|
| "Inclusion of existing networks and agency sites/data" | Map view + Browse list catalog 210 facilities across 32 networks (LTER, NERRS, NMS, NEP, IOOS, Sea Grant, NPS coastal, etc.) with full metadata. |
| "Continental-scale design" | Knowledge-map (`#/network`) shows research-area cartograms across all US coastal facilities + Latin American + Caribbean partners — visualises continent-scale coverage at a glance. |
| "Spatial / temporal / data gaps" | Stats dashboards (`#/stats`) per research area: facility counts by country, by region overlay, by facility type. Gap-callout panel surfaces under-represented strata. `docs/suitability_roadmap.md` lays out the MEOW + Köppen + GBIF ingestion that will enable "top-N candidate new sites" ranking. |
| "Inclusion of a social dimension" | Funding tab covers federal + state + nonprofit funding sources per facility; Form-990 totals expose the full revenue picture (federal + state + foundation + tuition pass-through) for the 9 nonprofit/foundation orgs in the dataset. |
| "Build on prior workshops, knowledge, capabilities" | Researcher directory (`#/people`) + per-person publication/citation metrics tie attendees to their pubs, ORCID, OpenAlex, Google Scholar profiles where available. |
| "Demonstrate value added to other networks" | Knowledge-map cross-area edges (sky-blue = researcher-bridging, gray = facility-bridging) make interdisciplinary collaboration visible — exactly the "where are the inter-network connections worth funding" question. |

## What's still missing (next-phase work)

To fully deliver against the MSI handout, cod-kmap still needs:

  - **Geographic / climatic strata layer** — MEOW marine ecoregions,
    Köppen-Geiger climate zones, EEZ boundaries (planned in
    `docs/suitability_roadmap.md`).
  - **Human-influence + biodiversity proxies** — GHSL population,
    WCMC marine pressures, GBIF/OBIS species richness (same doc).
  - **Site-suitability ranking** — H3 hex tiling + composite score so
    the dashboard can show "top-25 candidate new observatory sites
    per research area".
  - **Workforce + curriculum view** — capture training programs,
    REUs, NRTs, postdoc cohorts at each facility — feeds the
    "Co-Design Workforce Development" pillar.
  - **Time-series funding view** — already structured per fiscal year
    in `funding_events`; needs a chart UI surfacing trends per facility
    × funder × year.

These are documented as roadmap items so the proposal narrative + the
operational tool stay tied together as work progresses.
