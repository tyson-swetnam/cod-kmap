# Researcher and personnel coverage

Each facility in cod-kmap is associated with the people who lead it —
typically a Director, Reserve Manager, Principal Investigator,
Research Coordinator, or equivalent. Every personnel record carries a
verifiable source URL and a confidence rating, and is loaded only
when the source can be cited.

## Coverage today

Of the 210 facilities in the catalogue, **roughly 95 % have at least
one named, verified leader recorded** in `facility_personnel`. The
remaining ~5 % are intentionally left without a leader entry: their
public-facing pages do not list a named individual (some sentinel
sites, some virtual networks).

We do not fabricate leadership records. A NULL is more honest than a
guess, and the audit log captures every facility we couldn't resolve
along with the page we read.

## Source priorities by facility group

| Group | Source |
|---|---|
| NOAA NERR reserves | The reserve's own website (Manager + Research Coordinator pages); the NERRA member directory cross-references all 30. |
| EPA NEPs | The NEP's own website; EPA's NEP programme page links every Director. |
| NSF LTREB sites | NSF Award Search by LTREB programme and site keyword returns the Principal Investigator directly. |
| Fisheries and Oceans Canada institutes | Each institute's "About Us" page on dfo-mpo.gc.ca; the Government of Canada GEDS directory confirms incumbent. |
| IOOS Regional Associations | Each Regional Association's "About / Leadership" page. |
| Latin American institutes | CICESE, INIDEP, INAPESCA, ICML-UNAM directorate pages (Spanish-language). |
| Latin American and Caribbean universities | CIMAR (Costa Rica), IO-USP (Brazil), UWI-CERMES (Barbados), UWI-PRML (Jamaica) — directorate pages, mixed Spanish, Portuguese, and English. |
| US federal headquarters and singletons | Agency director pages (NCCOS, GLERL, NRL-SSC, FRF, NASA Goddard OBPG, etc.). |

## Record fields

Each row in `facility_personnel` captures:

- `person_id` and `facility_id` (foreign keys).
- `role` and `title` — e.g. *Director*, *Lead Principal Investigator*,
  with the title verbatim from the source page.
- `is_key_personnel` — true for Directors, Reserve Managers, Research
  Coordinators, and equivalent leadership roles.
- `start_date` and `end_date` where the source page or appointment
  notice gives them.
- `source` and `source_url` — the page we cited.
- `confidence` — `high` (named on a programme page or press release),
  `medium` (named on a partner page or news article), or `low`
  (inferred from a directory listing without a recent date).
- `notes` — a short free-text qualifier (incumbent date, transition
  status, etc.).

## When a record changes

Facility leadership changes regularly (NERR managers, EPA Regional
Administrators, NEP Directors, university lab heads). Each
personnel-research pass:

- Re-checks every existing record's source URL.
- Captures incumbents with start dates where available.
- Marks superseded records with an `end_date` rather than deleting
  them, so the audit history is preserved.

## How the data surfaces

- **People** tab — every researcher card shows their facility roles
  with title, country, and a link to the facility's homepage.
- **Network** tab — the per-facility tooltip lists the named
  Director / PI(s).
- **SQL** tab — the `v_facility_key_personnel` view exposes the
  current key personnel for any facility in a single row.
