"""Open-access PDF retrieval. Lawful locators only.

Routes tried, in order: what the harvest record already carries -> OA-index services queried by
DOI (Semantic Scholar per-DOI, every OpenAlex location, every Unpaywall location). A paywall or
a bot wall is classified and handed to the user with legal acquisition routes; it is never crossed.
"""
import base64
import csv
import json
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET

from .common import (http_get, http_stream, norm_arxiv, norm_doi, norm_title, ok,
                     read_jsonl, write_jsonl, slugify, title_sim, die)


def arxiv_pdf(a):
    return f"https://arxiv.org/pdf/{norm_arxiv(a)}" if norm_arxiv(a) else ""


def record_routes(rec):
    """Locators the record already carries — zero extra requests."""
    out = []
    oa = rec.get("oa_url") or ""
    pdf = rec.get("pdf_url") or ""
    if rec.get("arxiv_id"):
        out.append(("arxiv", arxiv_pdf(rec["arxiv_id"])))
    if pdf:
        out.append(("arxiv", pdf) if "arxiv.org" in pdf else ("openalex_oa_url", pdf))
    if oa and oa != pdf:
        out.append(("openalex_oa_url", oa))
    return out


def enrich_by_doi(rec, prof):
    """Ask the OA indexes for locators the harvest record lacks.

    Deliberately DOI-keyed: title matching produced 0 hits on a measured batch and would risk
    attaching the wrong paper's PDF. Returns (routes, pointers, notes, oa_status).
    `pointers` are lawful landing pages that are not fetchable PDFs — they go into the handoff doc.
    """
    routes, pointers, notes = [], [], []
    doi = norm_doi(rec.get("doi") or "")
    if not doi:
        return routes, pointers, notes, ""
    q = urllib.parse.quote(doi)

    st, body, err = http_get(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{q}"
        "?fields=title,externalIds,openAccessPdf,isOpenAccess", profile=prof, tries=3, timeout=20)
    if st == 200:
        try:
            d = json.loads(body.decode("utf8", "replace"))
        except Exception as e:
            d = None
            notes.append(f"s2:json {e}")
        if d:
            aid = (d.get("externalIds") or {}).get("ArXiv")
            if aid:
                routes.append(("semanticscholar_pdf", arxiv_pdf(aid)))
            pdf = (d.get("openAccessPdf") or {}).get("url") or ""
            if pdf:
                routes.append(("semanticscholar_pdf", pdf))
    else:
        notes.append(f"s2:{err or 'HTTP ' + str(st)}")

    mail = prof["http"]["polite_mailto"]
    st, body, err = http_get(
        f"https://api.openalex.org/works/doi:{q}?select=id,open_access,locations"
        f"&mailto={urllib.parse.quote(mail)}", profile=prof, tries=2, timeout=30)
    oa_status = ""
    if st == 200:
        try:
            w = json.loads(body.decode("utf8", "replace"))
        except Exception as e:
            w = None
            notes.append(f"openalex:json {e}")
        if w:
            oa_status = (w.get("open_access") or {}).get("oa_status") or ""
            for L in (w.get("locations") or []):
                if L.get("pdf_url"):
                    routes.append(("openalex_locations", L["pdf_url"]))
                src = ((L.get("source") or {}).get("display_name") or "").split("(")[0].strip()[:36]
                land = L.get("landing_page_url") or ""
                if land and not land.lower().startswith("https://doi.org"):
                    pointers.append({"kind": "repository" if src else "oa_landing", "url": land, "note": src})
    else:
        notes.append(f"openalex:{err or 'HTTP ' + str(st)}")

    st, body, err = http_get(f"https://api.crossref.org/works/{q}", profile=prof, tries=2, timeout=30)
    if st == 200:
        try:
            m = (json.loads(body.decode("utf8", "replace")) or {}).get("message") or {}
        except Exception as e:
            m = None
            notes.append(f"crossref:json {e}")
        if m:
            for L in (m.get("link") or []):
                u = L.get("URL") or ""
                # TDM endpoints (Elsevier etc.) need an API key we don't carry; they are
                # still lawful locators — the attempt just fails and gets recorded.
                if u and (L.get("content-type") == "application/pdf" or u.lower().endswith(".pdf")):
                    routes.append(("crossref_pdf", u))
    else:
        notes.append(f"crossref:{err or 'HTTP ' + str(st)}")

    # Europe PMC occasionally returns a truncated body like {"version":"6.9"} with HTTP 200
    # (measured on this host, header-independent). A body without hitCount is treated as a
    # transient fault and retried once instead of silently reading as "no hits".
    epmc = None
    for attempt in range(2):
        st, body, err = http_get(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:%22{q}%22"
            "&format=json&resultType=core", profile=prof, tries=2, timeout=30)
        if st != 200:
            notes.append(f"europepmc:{err or 'HTTP ' + str(st)}")
            break
        try:
            j = json.loads(body.decode("utf8", "replace"))
        except Exception as e:
            j = None
            notes.append(f"europepmc:json {e}")
        if isinstance(j, dict) and "hitCount" in j:
            epmc = j
            break
        if attempt == 0:
            notes.append("europepmc:truncated,retry")
            time.sleep(2)
    if epmc:
        hits = ((epmc.get("resultList") or {}).get("result")) or []
        if hits:
            h = hits[0]
            pmcid = h.get("pmcid") or ""
            if pmcid and h.get("isOpenAccess") == "Y":
                routes.append(("europepmc_pdf", f"https://europepmc.org/articles/{pmcid}?pdf=render"))

    st, body, err = http_get(f"https://api.unpaywall.org/v2/{q}?email={urllib.parse.quote(mail)}",
                             profile=prof, tries=2, timeout=25)
    if st == 200:
        try:
            d = json.loads(body.decode("utf8", "replace"))
        except Exception as e:
            d = None
            notes.append(f"unpaywall:json {e}")
        if d:
            oa_status = oa_status or ("OA" if d.get("is_oa") else "closed")
            for L in (d.get("oa_locations") or []):
                if L.get("url_for_pdf"):
                    routes.append(("unpaywall_repo", L["url_for_pdf"]))
                elif L.get("url"):
                    pointers.append({"kind": "repository" if L.get("host_type") != "publisher" else "oa_landing",
                                     "url": L["url"], "note": f"{L.get('host_type')}/{L.get('version')}"})
    else:
        notes.append(f"unpaywall:{err or 'HTTP ' + str(st)}")

    seen, uniq = set(), []
    for route, url in routes:
        key = (url or "").split("?")[0].rstrip("/")
        if url and key not in seen:
            seen.add(key)
            uniq.append((route, url))
    return uniq, pointers, notes, oa_status


def arxiv_title_fallback(rec, prof):
    """No DOI and no carried locator: ask arXiv by title. Only a near-exact match wins —
    a wrong paper's PDF is worse than no PDF, so the match needs both a high title-bigram
    overlap and the record's first author among the entry's authors."""
    title = (rec.get("title") or "").strip()
    if len(title) < 15:
        return ""
    q = urllib.parse.quote(f'ti:"{title}"')
    st, body, err = http_get(f"https://export.arxiv.org/api/query?search_query={q}&max_results=5",
                             profile=prof, tries=2, timeout=30)
    if st != 200:
        return ""
    try:
        root = ET.fromstring(body.decode("utf8", "replace"))
    except Exception:
        return ""
    ns = {"a": "http://www.w3.org/2005/Atom"}
    want = norm_title(title)
    fam = ((rec.get("authors") or [""])[0] or "").split()[-1].lower() if rec.get("authors") else ""
    for e in root.findall("a:entry", ns):
        et = (e.findtext("a:title", "", ns) or "").strip()
        if title_sim(norm_title(et), want) < 0.8:
            continue
        if fam:
            names = [((a.findtext("a:name", "", ns) or "").strip())
                     for a in e.findall("a:author", ns)]
            if not any(n.lower().endswith(fam) or n.lower().startswith(fam + " ") for n in names):
                continue
        aid = (e.findtext("a:id", "", ns) or "").rsplit("/", 1)[-1]
        return arxiv_pdf(aid)
    return ""


def _kind(err, code):
    e = err or ""
    if e.startswith("超过"):
        return "too_large"
    if e.startswith("非 PDF"):
        return "not_pdf"
    if code in (401, 403):
        return "denied"
    return "transient"


def classify(attempts, oa_status, has_pointer, asked_indexes):
    """Which kind of 'no' this is — the user's next action differs per class."""
    kinds = [a["kind"] for a in attempts]
    if not attempts:
        return "no_open_copy" if asked_indexes else "no_locator"
    if "too_large" in kinds:
        return "too_large"
    open_copy = oa_status in ("gold", "hybrid", "green", "OA") or bool(has_pointer)
    if "denied" in kinds:
        return "anti_bot_blocked" if open_copy else "closed_paywall"
    if all(k == "transient" for k in kinds):
        return "transient_error"
    if "not_pdf" in kinds:
        return "landing_only" if open_copy else "closed_paywall"
    return "closed_paywall"


def _pdf_outdir(run, prof):
    # storage_root shifts the PDFs out of the run dir into one place the user actually reads
    # (e.g. D:/ResearchPrograms/<run-id>/); manifest then records absolute paths.
    root = (prof["pdf"].get("storage_root") or "").strip()
    return os.path.join(root, run.run_id) if root else run.path("pdfs")


def _pdf_name(rec):
    return f"{rec['uid']}_{slugify((rec.get('title') or '')[:48], 40)}.pdf"


def fetch(run, limit=None, overwrite=False):
    prof = run.profile
    recs = read_jsonl(run.path("06_selected.jsonl"))
    if not recs:
        die("06_selected.jsonl missing — run review apply first")
    allow = set(prof["pdf"]["allow_sources"])
    max_bytes = int(prof["pdf"].get("max_mb", 80)) * 1024 * 1024
    magic = (prof["pdf"].get("verify_magic", "%PDF")).encode()
    outdir = _pdf_outdir(run, prof)
    os.makedirs(outdir, exist_ok=True)
    abs_paths = os.path.abspath(outdir) != os.path.abspath(run.path("pdfs"))
    # A cached row that reports route="local" throws away which lawful channel produced the file,
    # so provenance survives re-runs.
    seen_routes = {}
    try:
        seen_routes = json.load(open(os.path.join(outdir, ".routes.json"), encoding="utf-8"))
    except Exception:
        pass

    rows, enrichment = [], {}
    for rec in (recs[:limit] if limit else recs):
        fname = _pdf_name(rec)
        dest = os.path.join(outdir, fname)
        row = {"uid": rec["uid"], "doi": rec.get("doi", ""), "title": (rec.get("title") or "")[:90],
               "status": "", "pdf_class": "", "detail": "", "path": "", "bytes": 0, "route": ""}
        if os.path.isfile(dest) and not overwrite:
            row.update(status="cached", pdf_class="",
                       path=dest if abs_paths else os.path.relpath(dest, run.dir),
                       bytes=os.path.getsize(dest), route=seen_routes.get(rec["uid"], "local"))
            rows.append(row)
            continue
        attempts = []

        def grab(pairs, seen_urls):
            """First route that yields a real PDF wins; every miss is recorded, not swallowed."""
            for route, url in pairs:
                key = (url or "").split("?")[0]
                if route not in allow or key in seen_urls:
                    continue
                seen_urls.add(key)
                st, n, err = http_stream(url, dest, profile=prof, max_bytes=max_bytes, magic=magic)
                if st == 200 and err is None:
                    return route, n
                attempts.append({"route": route, "code": st, "kind": _kind(err, st)})
                row["detail"] = (row["detail"] + f" | {route}:{err or 'HTTP ' + str(st)}")[:240]
            return "", 0

        routes = record_routes(rec)
        seen_urls = set()
        route, n = grab(routes, seen_urls)
        oa_status, pointers = "", []
        if not route:
            more, pointers, notes, oa_status = enrich_by_doi(rec, prof)
            for nt in notes:
                row["detail"] = (row["detail"] + f" | {nt}")[:240]
            enrichment[rec["uid"]] = {"oa_status": oa_status, "pointers": pointers,
                                      "indexes_queried": bool(norm_doi(rec.get("doi") or "")),
                                      "tried": [a["route"] for a in attempts]}
            route, n = grab(more, seen_urls)
        if not route and not norm_doi(rec.get("doi") or ""):
            fb = arxiv_title_fallback(rec, prof)
            if fb:
                route, n = grab([("arxiv_title", fb)], seen_urls)
                if route:
                    row["detail"] = (row["detail"] + " | matched by arXiv title+author")[:240]
        if route:
            row.update(status="downloaded", route=route,
                       path=dest if abs_paths else os.path.relpath(dest, run.dir), bytes=n)
            seen_routes[rec["uid"]] = route
        elif not row["path"]:
            row["status"] = "manual_required"
            row["pdf_class"] = classify(attempts, oa_status, pointers,
                                        bool(norm_doi(rec.get("doi") or "")))
        rows.append(row)

    json.dump(seen_routes, open(os.path.join(outdir, ".routes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    with open(run.path("pdf_manifest.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["uid", "doi", "title", "status", "pdf_class", "route",
                                           "bytes", "path", "detail"])
        w.writeheader()
        w.writerows(rows)
    json.dump(enrichment, open(run.path("07_pdf_enrichment.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    status = {r["uid"]: {"status": r["status"], "path": r["path"], "route": r["route"]} for r in rows}
    json.dump(status, open(run.path("07_pdf_status.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n_ok = sum(1 for r in rows if r["status"] in ("downloaded", "cached"))
    manual = [r for r in rows if r["status"] == "manual_required"]
    run.save_state("pdfs", downloaded=n_ok, manual_required=len(manual),
                   by_class={c: sum(1 for r in manual if r["pdf_class"] == c)
                             for c in sorted({r["pdf_class"] for r in manual})})
    print(f"  downloaded/cached={n_ok}  manual_required={len(manual)}  (共 {len(rows)})")
    for r in manual:
        print(f"    {r['pdf_class']:17s} {r['uid']:>7} {(r.get('doi') or r['title'])[:52]} {r['detail'][:56]}")
    write_unresolved(run, rows, enrichment)
    ok("pdf_manifest.csv + 07_pdf_status.json + 07_pdf_enrichment.json + pdf_unresolved.md 写出；"
       "用 `lit.py zotero retag` 把 状态/需手动获取 打回标签")
    return rows


BROWSER_COMMIT_DOC = """browser 通道约定（给驱动浏览器的 agent）：
1. 前置：在 Qoder 内置浏览器里完成 CARSI 登录（ds.carsi.edu.cn → 选学校 → 学校 IdP，凭据用户本人输入）。
2. 逐篇处理 08_browser_queue.json：先试 pdf_candidates 里每条 URL —— 在论文落地页上下文中
   `fetch(url, {credentials:'include'})`，确认响应以 %PDF 开头且不超过 15MB（超出改走磁盘另存并记 path）。
   直链不通就打开落地页，从页面里的 Download PDF 按钮拿真实地址再 fetch。
3. 成功：把 PDF 字节转 base64，可按 ~3MB 一段切进 "pdf_b64_parts"；失败如实记 status=failed + detail。
4. 全部结果写 run 目录 08_browser_results.json：{"<uid>": {"status": "ok|failed", "detail": "...",
   "pdf_b64_parts": [...]}}。只记事实，禁止为了凑数伪造 status。
"""


def browser_queue(run, limit=None):
    """Collect the manual_required rows and package them for a human-entitled browser session."""
    prof = run.profile
    recs = {r["uid"]: r for r in read_jsonl(run.path("06_selected.jsonl"))}
    if not run.has("pdf_manifest.csv"):
        die("pdf_manifest.csv missing — run `pdfs fetch` first")
    rows = list(csv.DictReader(open(run.path("pdf_manifest.csv"), encoding="utf-8-sig")))
    enrich = {}
    if run.has("07_pdf_enrichment.json"):
        enrich = json.load(open(run.path("07_pdf_enrichment.json"), encoding="utf-8"))
    queue, skip = [], 0
    for row in (rows[:limit] if limit else rows):
        if row["status"] in ("downloaded", "cached"):
            skip += 1
            continue
        rec = recs.get(row["uid"]) or {"uid": row["uid"], "title": row["title"]}
        cands = []
        doi = norm_doi(row.get("doi") or "")
        if doi:
            cands.append(f"https://doi.org/{doi}")
        for p in (enrich.get(row["uid"]) or {}).get("pointers") or []:
            u = p.get("url") or ""
            if u and u not in cands:
                cands.append(u)
        queue.append({"uid": row["uid"], "doi": row.get("doi", ""), "pdf_class": row["pdf_class"],
                      "title": row["title"], "pdf_candidates": cands, "filename": _pdf_name(rec)})
    json.dump(queue, open(run.path("08_browser_queue.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    md = ["# 浏览器通道任务清单（CARSI 合法代取）", "",
          f"待处理 {len(queue)} 篇（另有 {skip} 篇已到手）。", "", "```json",
          json.dumps({"contract": BROWSER_COMMIT_DOC}, ensure_ascii=False, indent=2), "```", ""]
    for q in queue:
        md.append(f"## {q['uid']} · {q['title']}")
        md.append(f"- 分类：{q['pdf_class']}  文件名：`{q['filename']}`")
        for c in q["pdf_candidates"][:5]:
            md.append(f"- 入口：{c}")
        md.append("")
    open(run.path("08_browser_queue.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    run.save_state("pdfs_browser_queue", queued=len(queue), skipped_already_have=skip)
    ok(f"08_browser_queue.json/.md 写出：{len(queue)} 篇排队；"
       "浏览器下载完成后把结果写进 08_browser_results.json 再跑 `pdfs browser-commit`")
    return queue


def browser_commit(run):
    """Fold browser-session downloads (user-entitled, CARSI etc.) back into the manifest/tags."""
    prof = run.profile
    if not run.has("08_browser_results.json"):
        die("08_browser_results.json missing — the browser agent must write results first")
    results = json.load(open(run.path("08_browser_results.json"), encoding="utf-8"))
    recs = {r["uid"]: r for r in read_jsonl(run.path("06_selected.jsonl"))}
    magic = (prof["pdf"].get("verify_magic", "%PDF")).encode()
    outdir = _pdf_outdir(run, prof)
    os.makedirs(outdir, exist_ok=True)
    abs_paths = os.path.abspath(outdir) != os.path.abspath(run.path("pdfs"))

    mrows = list(csv.DictReader(open(run.path("pdf_manifest.csv"), encoding="utf-8-sig")))
    fields = list(mrows[0].keys()) if mrows else ["uid", "doi", "title", "status", "pdf_class",
                                                  "route", "bytes", "path", "detail"]
    by_uid = {r["uid"]: r for r in mrows}
    status = {}
    if run.has("07_pdf_status.json"):
        status = json.load(open(run.path("07_pdf_status.json"), encoding="utf-8"))
    try:
        seen_routes = json.load(open(os.path.join(outdir, ".routes.json"), encoding="utf-8"))
    except Exception:
        seen_routes = {}

    ok_n, fail = 0, 0
    for uid, r in results.items():
        row = by_uid.get(uid)
        if not row:
            continue
        detail = (r.get("detail") or "")[:160]
        if r.get("status") != "ok":
            row["detail"] = (row["detail"] + f" | browser:{detail or 'failed'}")[:240]
            fail += 1
            continue
        try:
            if r.get("path"):
                # Browser saved the file natively (real Chrome downloads) — copy from path.
                src = r["path"]
                if not os.path.isfile(src):
                    row["detail"] = (row["detail"] + f" | browser:文件不存在 {src[-40:]}")[:240]
                    fail += 1
                    continue
                with open(src, "rb") as fh:
                    data = fh.read()
            else:
                data = b"".join(base64.b64decode(p) for p in (r.get("pdf_b64_parts") or []))
        except Exception as e:
            row["detail"] = (row["detail"] + f" | browser:读取失败 {e}")[:240]
            fail += 1
            continue
        if not data.startswith(magic):
            row["detail"] = (row["detail"] + " | browser:非PDF内容,拒收")[:240]
            fail += 1
            continue
        rec = recs.get(uid) or {"uid": uid, "title": row["title"]}
        dest = os.path.join(outdir, _pdf_name(rec))
        with open(dest, "wb") as fh:
            fh.write(data)
        row.update(status="downloaded", route="browser", bytes=len(data),
                   path=dest if abs_paths else os.path.relpath(dest, run.dir),
                   pdf_class="", detail=(row["detail"] + f" | browser:{detail or 'ok'}")[:240])
        seen_routes[uid] = "browser"
        status[uid] = {"status": "downloaded", "path": row["path"], "route": "browser"}
        ok_n += 1
    with open(run.path("pdf_manifest.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(mrows)
    json.dump(status, open(run.path("07_pdf_status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump(seen_routes, open(os.path.join(outdir, ".routes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    run.save_state("pdfs_browser_commit", downloaded=ok_n, failed=fail)
    ok(f"browser-commit：新到手 {ok_n}，失败/拒收 {fail}；跑 `pdfs retag` 同步 状态/ 标签")
    return ok_n, fail


def _load_manifest_status(run):
    mrows = list(csv.DictReader(open(run.path("pdf_manifest.csv"), encoding="utf-8-sig")))
    fields = list(mrows[0].keys()) if mrows else ["uid", "doi", "title", "status", "pdf_class",
                                                  "route", "bytes", "path", "detail"]
    status = {}
    if run.has("07_pdf_status.json"):
        status = json.load(open(run.path("07_pdf_status.json"), encoding="utf-8"))
    return mrows, fields, {r["uid"]: r for r in mrows}, status


def _write_manifest_status(run, mrows, fields, status, seen_routes, outdir):
    with open(run.path("pdf_manifest.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(mrows)
    json.dump(status, open(run.path("07_pdf_status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump(seen_routes, open(os.path.join(outdir, ".routes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _abs_outdir(run, prof):
    outdir = _pdf_outdir(run, prof)
    return outdir, os.path.abspath(outdir) != os.path.abspath(run.path("pdfs"))


def browser_serve(run, timeout=900, port=0):
    """Local ingest server: the entitled browser page fetches the PDF and POSTs the bytes to
    127.0.0.1, so large files never pass through the agent's context. Blocks until every queued
    uid arrived (or timeout), then folds results into manifest/status/tags."""
    import http.server
    import secrets
    import threading
    prof = run.profile
    if not run.has("08_browser_queue.json"):
        die("08_browser_queue.json missing — run `pdfs browser-queue` first")
    queue = json.load(open(run.path("08_browser_queue.json"), encoding="utf-8"))
    if not queue:
        ok("队列为空，无需浏览器通道")
        return
    expected = {q["uid"]: q for q in queue}
    magic = (prof["pdf"].get("verify_magic", "%PDF")).encode()
    outdir, abs_paths = _abs_outdir(run, prof)
    os.makedirs(outdir, exist_ok=True)
    recs = {r["uid"]: r for r in read_jsonl(run.path("06_selected.jsonl"))}
    token = secrets.token_urlsafe(16)
    received, rejected = {}, {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(u.query)
            if u.path != "/ingest" or qs.get("token", [""])[0] != token:
                self.send_response(403)
                self.end_headers()
                return
            uid = qs.get("uid", [""])[0]
            if uid not in expected or uid in received:
                self.send_response(400)
                self._cors()
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else b""
            received[uid] = data
            self.send_response(200)
            self._cors()
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    base = f"http://127.0.0.1:{srv.server_port}"
    json.dump({"url": base, "token": token, "uids": sorted(expected)},
              open(run.path("08_browser_serve.json"), "w", encoding="utf-8"), indent=2)
    print(f"SERVE {base}/ingest token={token[:6]}… uids={sorted(expected)} timeout={timeout}s")
    srv.timeout = 1
    deadline = time.time() + timeout
    while len(received) < len(expected) and time.time() < deadline:
        srv.handle_request()
    srv.server_close()

    mrows, fields, by_uid, status = _load_manifest_status(run)
    seen_routes = {}
    try:
        seen_routes = json.load(open(os.path.join(outdir, ".routes.json"), encoding="utf-8"))
    except Exception:
        pass
    ok_n = fail = 0
    for uid in expected:
        row = by_uid.get(uid)
        if not row:
            continue
        data = received.get(uid)
        if data is None:
            row["detail"] = (row["detail"] + " | browser:未回传(超时)")[:240]
            fail += 1
            continue
        if not data.startswith(magic):
            row["detail"] = (row["detail"] + f" | browser:非PDF({len(data)}B),拒收")[:240]
            rejected[uid] = True
            fail += 1
            continue
        rec = recs.get(uid) or {"uid": uid, "title": row["title"]}
        dest = os.path.join(outdir, _pdf_name(rec))
        with open(dest, "wb") as fh:
            fh.write(data)
        row.update(status="downloaded", route="browser", bytes=len(data), pdf_class="",
                   path=dest if abs_paths else os.path.relpath(dest, run.dir))
        seen_routes[uid] = "browser"
        status[uid] = {"status": "downloaded", "path": row["path"], "route": "browser"}
        ok_n += 1
    _write_manifest_status(run, mrows, fields, status, seen_routes, outdir)
    run.save_state("pdfs_browser_serve", downloaded=ok_n, failed=fail)
    ok(f"browser-serve：到手 {ok_n}，失败/拒收 {fail}；跑 `pdfs retag` 同步 状态/ 标签")
    return ok_n, fail


def write_unresolved(run, rows, enrichment):
    """Handoff doc: for each missing PDF, the lawful routes and nothing else."""
    manual = [r for r in rows if r["status"] == "manual_required"]
    lines = ["# 未获取全文 — 合法获取路径", "",
             f"共 {len(manual)} 篇。**本技能不绕过付费墙与反爬墙**：下列路径都是出版商/作者/机构"
             "自己公开的副本，或你本人有权使用的机构订阅。", ""]
    why = {"anti_bot_blocked": "索引认定有开放副本，但本机取直链被拒（CDN 拦截或订阅墙，二者不可从外部区分）",
           "closed_paywall": "订阅制，无合法开放副本",
           "landing_only": "开放索引认定有 OA 版本，但只解析到落地页 HTML，没有 PDF 直链",
           "too_large": "文件超出 pdf.max_mb 上限",
           "transient_error": "网络/超时抖动，重试即可",
           "no_open_copy": "已按 DOI 逐一问过 Semantic Scholar / OpenAlex 全部 locations / Unpaywall 全部 "
                           "oa_locations，三个开放索引都没有合法 PDF 直链",
           "no_locator": "无 DOI 可查，且记录里没有任何 OA 位置"}
    for r in manual:
        e = enrichment.get(r["uid"], {})
        title = r["title"]
        lines.append(f"## {r['uid']} · {title}")
        lines.append("")
        if r.get("doi"):
            lines.append(f"- DOI：https://doi.org/{r['doi']}")
        lines.append(f"- 分类：**{r['pdf_class']}** — {why.get(r['pdf_class'], '')}")
        if e.get("oa_status"):
            lines.append(f"- OpenAlex OA 状态：`{e['oa_status']}`")
        if r["detail"].strip(" |"):
            lines.append(f"- 已试过：{r['detail'].strip(' |')[:180]}")
        pt = e.get("pointers") or []
        if pt:
            lines.append("- 合法入口（浏览器直接打开即可）：")
            for p in pt[:6]:
                lines.append(f"  - {p['kind']}{'（' + p['note'] + '）' if p.get('note') else ''}：{p['url']}")
        lines.append("- 其余可选：Zotero 右键 *Find Full Text*；校园网/VPN 下的机构订阅；"
                    "馆际互借；向通讯作者索取（邮箱见论文首页，不要臆测）。")
        lines.append("")
    with open(run.path("pdf_unresolved.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def retag(run):
    """Fold PDF outcome back into the tag schema so Zotero reflects what is actually readable."""
    status = json.load(open(run.path("07_pdf_status.json"), encoding="utf-8")) if run.has("07_pdf_status.json") else {}
    recs = read_jsonl(run.path("06_selected.jsonl"))
    pref = run.profile["tags"]["status"]["prefix"]
    changed = 0
    for r in recs:
        s = status.get(r["uid"], {}).get("status")
        tag = f"{pref}/待读" if s in ("downloaded", "cached") else f"{pref}/{run.profile['pdf']['on_fail_tag'].split('/')[-1]}"
        tags = [t for t in (r.get("zotero_tags") or []) if not t.startswith(pref + "/")]
        if tag not in tags:
            tags.append(tag)
        if set(tags) != set(r.get("zotero_tags") or []):
            changed += 1
        r["zotero_tags"] = tags
        r["pdf_local_path"] = status.get(r["uid"], {}).get("path", "")
    write_jsonl(run.path("06_selected.jsonl"), recs)
    ok(f"标签已按 PDF 结果更新（{changed} 条变动），重新执行 zotero build/import 生效")
    return recs
