
"""OpenAlex coastal-works harvest + co-author pair extraction.

Shared by the three harvest shards. Cursor-pages works for a set of authors
restricted to a curated topic set, writes works and co-author pairs to parquet
incrementally so an interrupted run resumes instead of restarting.
"""
import http.client
import json, os, time, urllib.request, urllib.parse, urllib.error

OA = "https://api.openalex.org/"
UA = "cod-kmap-registry-validation"


def _key():
    k = os.environ.get("OPENALEX_API_KEY")
    if not k:
        raise RuntimeError("OPENALEX_API_KEY not in environment; cannot call OpenAlex")
    return k


def oa_get(path, retries=5, **params):
    """One OpenAlex GET with api_key, backoff on 429/5xx. Never sends mailto."""
    params["api_key"] = _key()
    url = OA + path + "?" + urllib.parse.urlencode(params)
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as f:
                return json.loads(f.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay); delay *= 2; continue
            raise
        except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead,
                ConnectionResetError, json.JSONDecodeError):
            # IncompleteRead is a TRUNCATED body on an otherwise-200 response —
            # observed once mid-harvest ("857785 bytes read, 93039 more
            # expected"), which killed a run that had spent real quota. It is
            # transient and retryable; JSONDecodeError is the same failure when
            # the truncation lands mid-token.
            if attempt < retries - 1:
                time.sleep(delay); delay *= 2; continue
            raise
    raise RuntimeError("unreachable")


def short_id(v):
    """'https://openalex.org/A123' -> 'A123'; passes bare ids through."""
    if v is None:
        return None
    return str(v).rsplit("/", 1)[-1]


def bare_orcid(v):
    """ORCID URL or bare -> bare 0000-0000-0000-0000, else None."""
    if not v:
        return None
    s = str(v).rsplit("/", 1)[-1].strip()
    return s or None


def harvest_author_works(author_id, topic_ids, per_page=200, max_pages=200):
    """Cursor-page one author's works restricted to topic_ids.
    Yields work dicts. topic_ids empty/None means no topic restriction."""
    filt = f"author.id:{short_id(author_id)}"
    if topic_ids:
        filt += ",topics.id:" + "|".join(short_id(t) for t in topic_ids)
    cursor = "*"
    for _ in range(max_pages):
        d = oa_get("works", filter=filt,
                   select="id,doi,publication_year,title,cited_by_count,authorships",
                   cursor=cursor, **{"per-page": str(per_page)})
        results = d.get("results") or []
        for w in results:
            yield w
        cursor = (d.get("meta") or {}).get("next_cursor")
        if not cursor or not results:
            return


def coauthor_pairs(work, focal_author_id):
    """One row per co-author on `work`, excluding the focal author.
    Returns (work_row, [pair_rows]). Provenance: every pair carries work_id."""
    wid = short_id(work.get("id"))
    year = work.get("publication_year")
    focal = short_id(focal_author_id)
    authorships = work.get("authorships") or []
    work_row = dict(work_id=wid, doi=work.get("doi"),
                    publication_year=year, title=(work.get("title") or "")[:500],
                    cited_by_count=work.get("cited_by_count"),
                    n_authors=len(authorships))
    pairs = []
    for a in authorships:
        au = a.get("author") or {}
        aid = short_id(au.get("id"))
        if not aid or aid == focal:
            continue
        rors = [i.get("ror") for i in (a.get("institutions") or []) if i.get("ror")]
        pairs.append(dict(
            focal_openalex_id=focal, coauthor_openalex_id=aid,
            coauthor_display_name=au.get("display_name"),
            coauthor_orcid=bare_orcid(au.get("orcid")),
            coauthor_ror=short_id(rors[0]) if rors else None,
            work_id=wid, publication_year=year,
            author_position=a.get("author_position")))
    return work_row, pairs
