"""Multi-source harvest -> normalized JSONL records + provenance.

Sources: OpenAlex, Crossref, arXiv, Semantic Scholar, GIIISP (optional), citation chasing.
Every source degrades to `skip` on throttle/timeout; one dead source never blocks the run.
"""
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date

from .common import (Run, die, get_json, http_get, norm_arxiv, norm_doi, ok,
                    read_jsonl, write_jsonl, now)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_SCHEMA = "{http://arxiv.org/schemas/atom}"

OPENALEX_SELECT = ("id,doi,title,display_name,publication_year,publication_date,authorships,"
                   "primary_location,best_oa_location,open_access,biblio,cited_by_count,type,ids,"
                   "abstract_inverted_index,language,is_retracted,topics,referenced_works_count")

S2_FIELDS = ("title,year,publicationDate,venue,externalIds,citationCount,openAccessPdf,authors,"
             "abstract,url")


def _abs_from_invverted(inv):
    if not inv:
        return ""
    pos = {}
    for w, idxs in inv.items():
        for i in idxs:
            pos[i] = w
    return " ".join(pos[i] for i in sorted(pos))[:3000]


def _author_names(authorships):
    out = []
    for a in authorships or []:
        n = a.get("display_name") or (a.get("author") or {}).get("display_name")
        if n:
            out.append(n)
    return out


def blank(title="", **kw):
    r = {"title": (title or "").strip(), "authors": [], "year": None, "publication_date": None,
         "venue": "", "venue_issn_l": "", "venue_source_id": "", "doi": "", "arxiv_id": "",
         "openalex_id": "", "pmid": "", "s2_id": "", "url": "", "pdf_url": "", "oa_url": "",
         "oa_status": "", "abstract": "", "cited_by_count": 0, "type": "", "language": "",
         "sources": [], "query_ids": [], "discovered_via": "search", "domains": []}
    r.update(kw)
    if r.get("doi"):
        r["doi"] = norm_doi(r["doi"])
    if r.get("arxiv_id"):
        r["arxiv_id"] = norm_arxiv(r["arxiv_id"])
    return r


# ------------------------------------------------------------------ openalex
def openalex_key():
    """Env first; fall back to the Windows user-scope value so a freshly set key works
    without restarting the host process."""
    key = os.environ.get("OPENALEX_API_KEY") or ""
    if not key and hasattr(os, "name") and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = (winreg.QueryValueEx(k, "OPENALEX_API_KEY")[0] or "").strip()
        except OSError:
            key = ""
    return key.strip()


def _oa_auth(prof):
    """mailto only earns politeness; the shared-IP daily budget needs a real API key."""
    a = {"mailto": prof["http"]["polite_mailto"]}
    key = openalex_key()
    if key:
        a["api_key"] = key
    return a


OA_SORTS = {"relevance": "relevance_score:desc", "citations": "cited_by_count:desc",
              "date": "publication_date:desc"}
# arXiv (Cornell University) as an OpenAlex source; verified 2026-09-21 against a live record.
ARXIV_OA_SOURCE = "S4306400194"


def _openalex_record(w):
    loc = w.get("primary_location") or {}
    boa = w.get("best_oa_location") or {}
    src = loc.get("source") or {}
    ids = w.get("ids") or {}
    oa_pdf = boa.get("pdf_url") or ""
    r = blank(
                title=w.get("title") or w.get("display_name"),
                authors=_author_names(w.get("authorships")),
                year=w.get("publication_year"),
                publication_date=w.get("publication_date"),
                venue=src.get("display_name") or "",
                venue_issn_l=src.get("issn_l") or "",
                venue_source_id=(src.get("id") or "").rsplit("/", 1)[-1],
                doi=w.get("doi"),
                arxiv_id=norm_arxiv((ids.get("arxiv") or "") or _arxiv_from_url(boa.get("landing_page_url")) or _arxiv_from_url(oa_pdf)),
                openalex_id=(w.get("id") or "").rsplit("/", 1)[-1],
                pmid=_pmid(ids),
                url=w.get("id") or loc.get("landing_page_url") or "",
                pdf_url=oa_pdf or loc.get("pdf_url") or "",
                oa_url=(w.get("open_access") or {}).get("oa_url") or boa.get("landing_page_url") or "",
                oa_status=(w.get("open_access") or {}).get("oa_status") or "",
                abstract=_abs_from_invverted(w.get("abstract_inverted_index")),
                cited_by_count=w.get("cited_by_count") or 0,
                type=w.get("type") or "",
                language=w.get("language") or "",
                referenced_works_count=w.get("referenced_works_count"),
                is_retracted=bool(w.get("is_retracted")),
                domains=[d for d in (((t.get("domain") or {}).get("display_name") or "")
                                     for t in (w.get("topics") or [])[:3]) if d],
                # domain 只有 4 个取值，拿它分组等于没分组；subfield 才是能隔开"CS 新文 vs 医学旧文"的层
                subfields=[s for s in (((t.get("subfield") or {}).get("display_name") or "")
                                       for t in (w.get("topics") or [])[:3]) if s],
            )
    if not r["pdf_url"] and r["oa_url"] and "arxiv.org" in r["oa_url"]:
        r["pdf_url"] = r["oa_url"].replace("/abs/", "/pdf/")
    return r


def openalex_search(run, q, cursor_paging=True, limit=None):
    prof = run.profile
    oa = prof["sources"]["openalex"]
    cap = int(limit or oa["max_records"])
    cutoff = date(date.today().year - int(prof["window"]["years"]), 1, 1).isoformat()
    # Sorting the recall by citations and then truncating at max_records systematically
    # evicts relevant-but-uncited work; citations belong in scoring, not in the cut.
    sort = OA_SORTS.get(oa.get("sort", "relevance"), "relevance_score:desc")
    recs, calls = [], 0
    cursor = "*"
    while calls < 12 and cursor:
        params = _oa_auth(prof)
        filt = f"from_publication_date:{cutoff}"
        if q.get("mode") == "filter_search":
            # OpenAlex 的 search= 是分词词袋，引号不保证短语；filter 里的
            # title_and_abstract.search 支持 AND/OR/括号，能强制概念共现。
            filt = f"title_and_abstract.search:({q['expr']}),{filt}"
        else:
            params["search"] = q["expr"]
        params.update({"per-page": min(200, oa["per_page"]),
                       "select": OPENALEX_SELECT, "filter": filt, "sort": sort})
        if cursor_paging:
            params["cursor"] = cursor
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data, err = get_json(url, profile=prof, timeout=oa["timeout_s"])
        calls += 1
        if err:
            return recs, f"openalex: {err}"
        for w in data.get("results", []):
            recs.append(_openalex_record(w))
        if not cursor_paging:
            break
        cursor = (data.get("meta") or {}).get("next_cursor")
        if len(recs) >= cap:
            break
    return recs[:cap], None


def arxiv_via_openalex(run, q):
    """Fallback route for when the arXiv API itself is unavailable.

    OpenAlex indexes every arXiv paper under DOI 10.48550/arXiv.<id>, so filtering on the
    arXiv source id recovers the preprint layer in one call. Records are marked with
    discovered_via so a reader knows the id came second-hand, not from arXiv directly."""
    prof = run.profile
    oa = prof["sources"]["openalex"]
    cutoff = date(date.today().year - int(prof["window"]["years"]), 1, 1).isoformat()
    params = _oa_auth(prof)
    params.update({"search": q["expr"], "per-page": min(200, oa["per_page"]),
                   "select": OPENALEX_SELECT,
                   "filter": f"primary_location.source.id:{ARXIV_OA_SOURCE},from_publication_date:{cutoff}",
                   "sort": OA_SORTS.get(oa.get("sort", "relevance"), "relevance_score:desc")})
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data, err = get_json(url, profile=prof, timeout=oa["timeout_s"])
    if err:
        return [], f"arxiv_via_openalex: {err}"
    recs = []
    for w in data.get("results", []):
        r = _openalex_record(w)
        r["discovered_via"] = "arxiv_via_openalex"
        recs.append(r)
    return recs[: int(prof["sources"]["arxiv"]["max_records"])], None


def _arxiv_from_url(u):
    u = u or ""
    if "arxiv.org" not in u:
        return ""
    m = urllib.parse.urlsplit(u).path.strip("/").split("/")
    return m[-1] if len(m) >= 2 and m[0] in ("abs", "pdf", "doi") else ""


def _pmid(ids):
    v = (ids or {}).get("pmid")
    return str(v) if v else ""


# ------------------------------------------------------------------ crossref
def crossref_search(run, q, limit=None):
    prof = run.profile
    cr = prof["sources"]["crossref"]
    cutoff = date(date.today().year - int(prof["window"]["years"]), 1, 1).isoformat()
    params = {
        "query.bibliographic": q["expr"], "rows": min(1000, cr["per_page"]),
        "filter": f"from-pub-date:{cutoff},type:journal-article",
        "select": "DOI,title,author,issued,container-title,ISSN,is-referenced-by-count,abstract,URL,link,type,resource",
    }
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data, err = get_json(url, profile=prof, timeout=cr["timeout_s"])
    if err:
        return [], f"crossref: {err}"
    recs = []
    for it in (data.get("message") or {}).get("items", []):
        issn = (it.get("ISSN") or [""])[0]
        link = ""
        for l in it.get("link") or []:
            if l.get("content-type", "").startswith("application/pdf"):
                link = l.get("URL", "")
        auth = [f"{a.get('given','')} {a.get('family','')}".strip() or a.get("name", "") for a in it.get("author") or []]
        r = blank(
            title=(it.get("title") or [""])[0],
            authors=[a for a in auth if a],
            year=_cr_year(it.get("issued")),
            venue=(it.get("container-title") or [""])[0],
            venue_issn_l=issn,
            doi=it.get("DOI"),
            url=it.get("URL") or "",
            pdf_url=link,
            abstract=_strip_jats(it.get("abstract") or ""),
            cited_by_count=it.get("is-referenced-by-count") or 0,
            type=it.get("type") or "",
        )
        recs.append(r)
    return recs[: int(limit or cr["max_records"])], None


def _cr_year(part):
    dp = (part or {}).get("date-parts") or [[None]]
    return dp[0][0]


def _strip_jats(s):
    import re
    return re.sub(r"<[^>]+>", " ", s or "").strip()[:3000]


# ------------------------------------------------------------------ arxiv
def arxiv_search(run, q, limit=None):
    prof = run.profile
    ax = prof["sources"]["arxiv"]
    cap = int(limit or ax["max_records"])
    recs = []
    for start in range(0, cap, int(ax["per_page"])):
        params = {"search_query": q["expr"], "start": start, "max_results": min(200, ax["per_page"]),
                  "sortBy": "relevance", "sortOrder": "descending"}
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
        st, body, err = http_get(url, profile=prof, timeout=ax["timeout_s"])
        if err:
            return (recs, f"arxiv: {err}") if not recs else (recs, None)
        try:
            root = ET.fromstring(body)
        except Exception as e:
            return recs, f"arxiv xml: {e}"
        for e in root.findall(f"{ATOM}entry"):
            def txt(tag):
                n = e.find(f"{ATOM}{tag}")
                return (n.text or "").strip() if n is not None else ""
            absurl = txt("id")
            r = blank(
                title=" ".join(txt("title").split()),
                authors=[(a.find(f"{ATOM}name").text or "").strip() for a in e.findall(f"{ATOM}author")],
                year=(txt("published") or "")[:4] and int(txt("published")[:4]) or None,
                publication_date=txt("published"),
                venue="arXiv",
                arxiv_id=norm_arxiv(absurl),
                url=absurl,
                pdf_url=txt("link").replace("abstract", "pdf") or (absurl.replace("/abs/", "/pdf/") + ""),
                oa_url=absurl.replace("/abs/", "/pdf/") if "/abs/" in absurl else "",
                abstract=txt("summary")[:3000],
                doi=_arxiv_doi(e),
                type="preprint",
                language="en",
            )
            r["oa_status"] = "OA" if r["arxiv_id"] else ""
            recs.append(r)
        if len(recs) >= cap:
            break
    return recs[:cap], None


def _arxiv_doi(entry):
    n = entry.find(f"{ARXIV_SCHEMA}doi")
    if n is not None and (n.text or "").strip():
        return norm_doi(n.text.strip())
    for l in entry.findall(f"{ATOM}link"):
        if (l.get("title") or "").lower() == "doi" and "doi.org" in (l.get("href") or ""):
            return norm_doi(l["href"].split("doi.org/")[-1])
    return ""


# ------------------------------------------------------------------ semanticscholar
def s2_search(run, q, limit=None):
    prof = run.profile
    ss = prof["sources"]["semanticscholar"]
    cutoff = date.today().year - int(prof["window"]["years"])
    params = {"query": q["expr"], "limit": min(100, ss["per_page"]), "fields": S2_FIELDS,
              "fieldsOfStudy": "Computer Science,Chemistry,Materials Science,Engineering,Medicine,Physics"}
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    data, err = get_json(url, profile=prof, timeout=ss["timeout_s"])
    if err:
        return [], f"s2: {err}"
    recs = []
    for it in data.get("data") or []:
        ext = it.get("externalIds") or {}
        y = it.get("year")
        if y and int(y) < cutoff:
            continue
        r = blank(
            title=it.get("title"),
            authors=[a.get("name", "") for a in it.get("authors") or [] if a.get("name")],
            year=y,
            publication_date=it.get("publicationDate"),
            venue=it.get("venue") or "",
            doi=ext.get("DOI"),
            arxiv_id=ext.get("ArXiv"),
            pmid=ext.get("PMID") or "",
            s2_id=it.get("paperId") or "",
            url=f"https://www.semanticscholar.org/paper/{it.get('paperId','')}",
            pdf_url=(it.get("openAccessPdf") or {}).get("url") or "",
            abstract=(it.get("abstract") or "")[:3000],
            cited_by_count=it.get("citationCount") or 0,
            type="preprint" if ext.get("ArXiv") and not ext.get("DOI") else "journal-article",
        )
        if r["pdf_url"]:
            r["oa_url"] = r["pdf_url"]
            r["oa_status"] = "OA"
        recs.append(r)
    return recs[: int(limit or ss["max_records"])], None


# ------------------------------------------------------------------ giiisp
def giiisp_search(run, q, limit=None):
    prof = run.profile
    tok = os.environ.get("GIIISP_AUTH_TOKEN") or ""
    if not tok:
        return [], "giiisp: GIIISP_AUTH_TOKEN not set"
    url = "https://giiisp.com/first/oaPaper/searchArticlesByQuery1"
    st, body, err = http_get(url, profile=prof, method="POST", body={"titleAndAbs": q["expr"]},
                              ct="application/json", headers={"Authorization": f"Bearer {tok}"}, timeout=40)
    if err:
        return [], f"giiisp: {err}"
    try:
        data = json.loads(body.decode("utf8", "replace"))
    except Exception as e:
        return [], f"giiisp json: {e}"
    recs = []
    rows = data.get("data") or data.get("records") or []
    if isinstance(rows, dict):
        rows = rows.get("records") or rows.get("list") or []
    for it in rows if isinstance(rows, list) else []:
        r = blank(
            title=it.get("title") or it.get("titleCn") or it.get("name") or "",
            authors=[a.get("name", "") if isinstance(a, dict) else str(a) for a in (it.get("authors") or it.get("authorList") or [])],
            year=(str(it.get("publishDate") or it.get("year") or ""))[:4] or None,
            publication_date=it.get("publishDate") or "",
            venue=it.get("journalName") or it.get("source") or "",
            doi=it.get("doi") or "",
            arxiv_id=it.get("arxivId") or "",
            url=it.get("url") or it.get("link") or "",
            pdf_url=it.get("pdfUrl") or "",
            abstract=(it.get("abstract") or it.get("abstractCn") or "")[:3000],
            language="zh" if (it.get("titleCn") and not it.get("title")) else "en",
            type="journal-article",
        )
        if r["pdf_url"]:
            r["oa_url"], r["oa_status"] = r["pdf_url"], "OA"
        if r["title"]:
            recs.append(r)
    return recs[: int(limit or prof["sources"]["giiisp"]["max_records"])], None


# ------------------------------------------------------------------ citation chasing
def snowball(run, seeds):
    """One hop of references + citations per seed via OpenAlex. Degrades silently."""
    prof = run.profile
    cfg = prof["sources"]["citation_chasing"]
    if not cfg["enabled"] or not seeds:
        return [], "citation_chasing disabled / no seeds"
    added, errs = [], []
    for s in seeds[: int(cfg["seeds_max"])]:
        wid = s
        ax = norm_arxiv(s.replace("arxiv:", "")) if s.lower().startswith("arxiv:") else ""
        if not ax and re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", s):
            ax = norm_arxiv(s)
        if ax:
            # arXiv itself cannot say who cited a preprint; OpenAlex can. The constructed DOI
            # 10.48550/arXiv.<id> only covers part of the corpus (1706.03762 404s there), so
            # fall back to searching the bare id before giving up.
            p = _oa_auth(prof)
            p["per-page"] = 1
            s2, e = get_json(f"https://api.openalex.org/works/doi:10.48550/arXiv.{ax}",
                             profile=prof, headers={"Accept": "application/json"}, timeout=30)
            if e:
                p["filter"] = f"title_and_abstract.search:{ax}"
                s2, e = get_json("https://api.openalex.org/works?" + urllib.parse.urlencode(p),
                                 profile=prof, timeout=30)
                hits = (s2 or {}).get("results") or []
                s2 = hits[0] if hits else None
            if e or not s2:
                errs.append(f"{s}: 无法在 OpenAlex 定位该 arXiv 号（{e or 'no hit'}）")
                continue
            wid = (s2.get("id") or "").rsplit("/", 1)[-1]
        elif wid.startswith("10.") or s.startswith("doi:"):
            d, e = get_json(f"https://api.openalex.org/works/doi:{norm_doi(s.replace('doi:', ''))}",
                            profile=prof, headers={"Accept": "application/json"}, timeout=30)
            if e:
                errs.append(f"{s}: {e}")
                continue
            wid = (d.get("id") or "").rsplit("/", 1)[-1]
        # 过滤器名必须是 referenced_works；写成 references 会 400，
        # 而这一向是静默降级的，所以引用网络的一半一直没能真的跑起来。
        for direction, filt in (("ref", "referenced_works"), ("cit", "cites")):
            params = _oa_auth(prof)
            params.update({"filter": f"{filt}:{wid}", "per-page": 25, "select": OPENALEX_SELECT,
                           "sort": "cited_by_count:desc"})
            url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
            d, e = get_json(url, profile=prof, timeout=30)
            if e:
                errs.append(f"{wid}/{direction}: {e}")
                continue
            for w in d.get("results", []):
                r = _openalex_record(w)
                r["discovered_via"] = f"snowball_{direction}"
                r["seed"] = wid
                added.append(r)
        if len(added) >= int(cfg["max_added"]):
            break
    return added[: int(cfg["max_added"])], ("; ".join(errs[:3]) or None)


# ------------------------------------------------------------------ driver
DISPATCH = {"openalex": openalex_search, "crossref": crossref_search, "arxiv": arxiv_search,
            "semanticscholar": s2_search, "giiisp": giiisp_search}


def _matches(api, q):
    t = q.get("api", "all")
    return True if t == "all" else api if api in (t if isinstance(t, list) else [t]) else False


def harvest(run, only=None, dry=False):
    qf = run.path("queries.json")
    if not os.path.isfile(qf):
        die("queries.json missing — write the search recipe first (see references/01-query-recipe.md)")
    plan = json.load(open(qf, encoding="utf-8"))
    strings = plan.get("strings") or []
    if not strings:
        die("queries.json has no `strings[]`")
    prof = run.profile
    out, log = [], []
    for api, cfg in prof["sources"].items():
        if api == "citation_chasing" or not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        if only and api not in only:
            continue
        if cfg.get("requires_env") and not os.environ.get(cfg["requires_env"]):
            log.append({"source": api, "status": "skip", "reason": f"env {cfg['requires_env']} unset"})
            continue
        qs = [q for q in strings if _matches(api, q)]
        if not qs:
            log.append({"source": api, "status": "skip", "reason": "no query targets this source"})
            continue
        if dry:
            for q in qs[:2]:
                print(f"[dry] {api} <- {q['id']}: {q['expr'][:120]}")
            continue
        got, got_err = [], []
        # 按式子配额：整源一个全局上限时，第一条宽式就能把额度吃满，
        # 后面的精检式与相邻域式子一条都不会跑，召回结构被悄悄改写。
        per_q = max(25, int(cfg.get("max_records", 300)) // max(1, len(qs)))
        for q in qs:
            recs, err = DISPATCH[api](run, q, limit=per_q)
            for r in recs:
                r["sources"] = [api]
                r["query_ids"] = [q["id"]]
                r["harvested_at"] = now()
            got.extend(recs)
            got_err.append({"query": q["id"], "n": len(recs), "err": err})
        errs_here = [g for g in got_err if g.get("err")]
        status = "ok" if got and not errs_here else ("degraded" if got or not errs_here else "failed")
        if not got and errs_here:
            status = "failed"
        log.append({"source": api, "status": status,
                    "returned": len(got), "queries": got_err})
        out.extend(got)

    if prof["sources"].get("arxiv", {}).get("fallback_via_openalex", True) and not only:
        ax = next((l for l in log if l["source"] == "arxiv"), None)  # arXiv 主通道降级时补一次
        oa_ok = prof["sources"]["openalex"].get("enabled") and not any(
            l["source"] == "openalex" and l["status"] == "failed" for l in log)
        if ax and ax["status"] in ("degraded", "failed") and oa_ok:
            fb, nfb = [], 0
            for q in [s for s in strings if _matches("arxiv", s)][:4]:
                recs, err = arxiv_via_openalex(run, q)
                for r in recs:
                    r["sources"] = ["arxiv_via_openalex"]
                    r["query_ids"] = [q["id"]]
                    r["harvested_at"] = now()
                fb.extend(recs)
                nfb += 1
                if err or len(fb) >= int(prof["sources"]["arxiv"]["max_records"]):
                    break
            if fb:
                log.append({"source": "arxiv_via_openalex", "status": "ok", "returned": len(fb),
                            "queries": [{"note": f"arXiv API 不可用，改走 OpenAlex 的 10.48550/arXiv 索引（{nfb} 条查询）",
                                         "mark": "discovered_via=arxiv_via_openalex"}]})
                out.extend(fb)
                print(f"  arXiv 降级已回落 OpenAlex：补回 {len(fb)} 条预印本（标记 discovered_via=arxiv_via_openalex）")

    if (not only) or "citation_chasing" in (only or []):
        cc = prof["sources"].get("citation_chasing", {})
        seeds = plan.get("seed_works") or []
        if cc.get("enabled") and seeds and not dry:
            extra, serr = snowball(run, seeds)
            for r in extra:
                r["sources"] = ["openalex"]
                r["query_ids"] = ["SNOWBALL"]
                r["harvested_at"] = now()
            out.extend(extra)
            log.append({"source": "citation_chasing", "status": "ok" if extra else "degraded",
                        "returned": len(extra), "queries": [{"seeds": len(seeds), "err": serr}]})

    if dry:
        ok(f"dry-run plan built for {len(strings)} query strings")
        return out, log

    prev = read_jsonl(run.path("01_raw.jsonl"))
    merged = prev + out
    n = write_jsonl(run.path("01_raw.jsonl"), merged)
    run.add_provenance("harvest", new=len(out), total=n, log=log)
    run.save_state("harvest", records=n, sources={l["source"]: l.get("returned", 0) for l in log})
    for l in log:
        print(f"  {l['source']:<16} {l['status']:<9} {l.get('returned',0):>5}  {json.dumps(l.get('queries'),ensure_ascii=False)[:110] if l.get('queries') else l.get('reason','')}")
    floor = int(prof["sources"].get("min_union_after_degrade", 120))
    degraded = [l["source"] for l in log if l["status"] in ("degraded", "failed")]
    if degraded:
        print(f"  WARN 降级来源：{', '.join(degraded)} — 本次召回不完整，report.md 会记录")
    if n < floor:
        print(f"  WARN 总召回 {n} < 阈值 {floor}：放宽检索式（减少 AND 组）、补跑 snowball，或换用未限流的源")
    ok(f"01_raw.jsonl = {n} records ({len(out)} new)")
    return out, log
