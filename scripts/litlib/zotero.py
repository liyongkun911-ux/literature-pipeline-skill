"""Zotero bridge: channel probe, idempotent import, tags, RIS/BibTeX writers.

Four channels, tried in order of capability. `probe` only reads; `import` is the only
mutating command and refuses to run without --yes.
"""
import csv
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .common import (Run, get_json, http_get, norm_arxiv, norm_doi, norm_title, ok, read_jsonl, die, now)

RIS_TYPE = {"journal-article": "JOUR", "preprint": "CPAPER", "book": "BOOK", "chapter": "CHAP",
            "conference-paper": "CPAPER", "report": "RPRT", "thesis": "THES", "": "GEN"}


# ------------------------------------------------------------------ items
def split_name(n):
    if "," in n:
        last, first = [x.strip() for x in n.split(",", 1)]
        return first, last
    parts = n.split()
    return " ".join(parts[:-1]), parts[-1] if parts else n


def to_zotero_item(run, r, with_pdf=True):
    rv = r.get("venue_resolved") or {}
    preprint = rv.get("kind") == "preprint"
    creators = [{"creatorType": "author", "firstName": f or "", "lastName": l or f}
                for f, l in (split_name(a) for a in (r.get("authors") or [])) if l or f]
    item = {
        "itemType": "preprint" if preprint else "journalArticle",
        "title": r.get("title") or "",
        "creators": creators,
        "date": str(r.get("publication_date") or r.get("year") or ""),
        "DOI": r.get("doi") or "",
        "url": r.get("url") or "",
        "abstractNote": (r.get("abstract") or "")[:2000],
        "libraryCatalog": "+".join(r.get("sources") or []),
        "extra": f"LitPipe uid: {r['uid']}  score: {r.get('score')}  rank: {r.get('candidate_no')}",
        "tags": [{"tag": t, "type": 1} for t in (r.get("zotero_tags") or [])],
    }
    if preprint:
        item["repository"] = rv.get("venue") or "arXiv"
        if r.get("arxiv_id"):
            item["archiveID"] = f"arXiv:{r['arxiv_id']}"
    else:
        item["publicationTitle"] = rv.get("venue") or r.get("venue") or ""
        if r.get("venue_issn_l"):
            item["ISSN"] = r["venue_issn_l"]
    pdf = r.get("pdf_url") or r.get("oa_url")
    if with_pdf and pdf:
        item["attachments"] = [{"type": "url", "mimeType": "application/pdf",
                                "title": "Full Text PDF (OA)", "url": pdf}]
    return item


# ------------------------------------------------------------------ RIS / BibTeX
def write_ris(run, recs, path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for r in recs:
            rv = r.get("venue_resolved") or {}
            fh.write(f"TY  - {RIS_TYPE.get(r.get('type') or '', 'GEN')}\n")
            for a in r.get("authors") or []:
                f, l = split_name(a)
                fh.write(f"AU  - {', '.join(x for x in (l, f) if x)}\n")
            fh.write(f"TI  - {r.get('title','')}\n")
            if rv.get("venue"):
                fh.write(f"T2  - {rv['venue']}\n")
            fh.write(f"PY  - {r.get('year') or ''}\n")
            if r.get("doi"):
                fh.write(f"DO  - {r['doi']}\n")
            if r.get("arxiv_id"):
                fh.write(f"ID  - arXiv:{r['arxiv_id']}\n")
            fh.write(f"UR  - {r.get('pdf_url') or r.get('oa_url') or r.get('url') or ''}\n")
            if r.get("abstract"):
                fh.write("AB  - " + re.sub(r"\s+", " ", r["abstract"])[:1500] + "\n")
            for t in r.get("zotero_tags") or []:
                fh.write(f"KW  - {t}\n")
            fh.write(f"N1  - LitPipe uid={r['uid']} score={r.get('score')}\n")
            fh.write("ER  - \n\n")
    return path


def citekey(r):
    f = ((r.get("authors") or [""]) [0].split() or ["anon"])[-1]
    return re.sub(r"[^\w]", "", f"{f}{r.get('year') or ''}{(r.get('title') or '').split()[0] if r.get('title') else 'unt'}").lower()[:40]


def write_bib(run, recs, path):
    seen = set()
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            k = citekey(r)
            while k in seen:
                k += "b"
            seen.add(k)
            rv = r.get("venue_resolved") or {}
            typ = "online" if rv.get("kind") == "preprint" else "article"
            fields = [("author", " and ".join(r.get("authors") or [])), ("title", r.get("title") or ""),
                      ("year", r.get("year") or ""), ("journal", rv.get("venue") or r.get("venue") or ""),
                      ("doi", r.get("doi") or ""), ("url", r.get("url") or ""),
                      ("abstract", re.sub(r"\s+", " ", r.get("abstract") or "")[:900]),
                      ("keywords", " ; ".join(r.get("zotero_tags") or [])),
                      ("note", f"LitPipe uid={r['uid']} score={r.get('score')}")]
            fh.write(f"@{typ}{{{k},\n")
            fh.write(",\n".join(f"  {n} = {{{v}}}" for n, v in fields if str(v).strip()))
            fh.write("\n}\n\n")
    return path


# ------------------------------------------------------------------ channel probe
def _zotero_prefs():
    home = os.path.expanduser("~")
    prof = os.path.join(home, "AppData", "Roaming", "Zotero", "Zotero", "Profiles")
    out = {"dataDir": os.path.join(home, "Zotero")}
    if os.path.isdir(prof):
        for d in os.listdir(prof):
            pj = os.path.join(prof, d, "prefs.js")
            if os.path.isfile(pj):
                txt = open(pj, encoding="utf8", errors="replace").read()
                m = re.search(r'"extensions\.zotero\.dataDir",\s*"([^"]+)"', txt)
                if m:
                    out["dataDir"] = m.group(1).replace("\\\\", "\\")
                m2 = re.search(r'"extensions\.zotero\.baseURL",\s*"([^"]+)"', txt)
                if m2:
                    out["baseURL"] = m2.group(1)
    return out


# ------------------------------------------------------------------ local API write auth
# Measured 2026-10-06 on Zotero 9.0.6 (see references/04-zotero-bridge.md): a write without a
# key from POST /local/authorize gets 401, authorize without Zotero-Server-ID gets 428, and
# authorizing once then continuing to write still 401s — so take a fresh key per write request.
AUTH_APP_NAME = "literature-pipeline"
AUTHORIZE_TIMEOUT_S = 180      # the user answers Zotero's dialog; 30s times out before they click
RATE_LIMIT_BACKOFF_S = 10      # measured 429 on rapid re-authorize
WRITE_RETRIES = 3


def _zapi_base(run):
    return run.profile["zotero"]["base"].rstrip("/") + "/api"


def _zapi_get(run, path, timeout=20):
    """Read-only local API GET → (status, parsed|bytes|str, headers). Never raises."""
    req = urllib.request.Request(_zapi_base(run) + path, headers={"Zotero-API-Version": "3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf8", "replace")), r.headers
            except ValueError:
                return r.status, raw, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except Exception as e:
        return 0, str(e), None


def server_id(run):
    st, _, hdrs = _zapi_get(run, "/users/0/items?limit=1")
    sid = (hdrs or {}).get("Zotero-Server-ID") or ""
    if st != 200 or not sid:
        print(f"  WARN 取不到 Zotero-Server-ID（HTTP {st}）—— 检查 Zotero 是否在运行、设置→高级 是否允许本机应用通信")
    return sid


def authorize(run, sid):
    body = json.dumps({"appName": AUTH_APP_NAME}).encode()
    req = urllib.request.Request(_zapi_base(run) + "/local/authorize", data=body, method="POST",
                                 headers={"Zotero-Server-ID": sid, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=AUTHORIZE_TIMEOUT_S) as r:
            return (json.loads(r.read().decode("utf8", "replace")) or {}).get("key") or ""
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"  429 限流（authorize），退避 {RATE_LIMIT_BACKOFF_S}s")
            time.sleep(RATE_LIMIT_BACKOFF_S)
            return ""
        print(f"  WARN authorize 失败 HTTP {e.code}")
        return ""
    except Exception as e:
        print(f"  WARN authorize 失败：{e}")
        return ""


class LocalWrite:
    """Local API writes with one reused authorize key. A 401 means the key died → re-authorize;
    authorizing on every request instead gets its own 429 once you write more than a few items
    (measured: 3 of 19 tag writes failed that way)."""

    def __init__(self, run, sid):
        self.run, self.sid = run, sid
        self.key = os.environ.get("ZOTERO_LOCAL_API_KEY") or (run.profile["zotero"].get("local_api_key") or "")

    def write(self, method, path, body, extra_headers=None):
        for _ in range(WRITE_RETRIES):
            if not self.key:
                self.key = authorize(self.run, self.sid)
                if not self.key:
                    continue
            hdrs = {"Zotero-API-Version": "3", "Zotero-Server-ID": self.sid,
                    "Content-Type": "application/json", "Zotero-API-Key": self.key}
            hdrs.update(extra_headers or {})
            req = urllib.request.Request(_zapi_base(self.run) + path, data=json.dumps(body).encode(),
                                         method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    try:
                        return r.status, json.loads(raw.decode("utf8", "replace"))
                    except ValueError:
                        return r.status, raw
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self.key = ""      # expired/rejected → authorize on the next pass
                    continue
                if e.code == 429:
                    print(f"  429 限流，退避 {RATE_LIMIT_BACKOFF_S}s")
                    time.sleep(RATE_LIMIT_BACKOFF_S)
                    continue
                return e.code, e.read()
            except Exception as e:
                return 0, str(e)
        return 0, "write failed after retries (authorize/401)"


def probe(run):
    prof = run.profile
    base = prof["zotero"]["base"].rstrip("/")
    ch = {}
    st, body, err = http_get(base + "/connector/ping", profile=prof, method="POST", body={},
                             ct="application/json", tries=1, timeout=5)
    ch["connector"] = {"ok": st == 200, "detail": (body[:120].decode("utf8", "replace") if body else err)}
    if st == 200:
        try:
            ch["connector"]["prefs"] = {k: v for k, v in json.loads(body.decode("utf8", "replace")).get("prefs", {}).items()
                                        if k in ("downloadAssociatedFiles", "automaticSnapshots", "supportsAttachmentUpload")}
        except Exception:
            pass
    st, body, err = http_get(base + "/api/users/0/collections?limit=5", profile=prof, tries=1, timeout=5,
                             headers={"Zotero-API-Version": "3"})
    ch["local_api"] = {"ok": st == 200, "detail": (f"HTTP {st} " + (body[:80].decode("utf8", "replace") if body else "")) if st != 200 else err or "read/write available"}
    st, body, err = http_get(base + "/better-bibtex/json-rpc", profile=prof, method="POST",
                             body={"jsonrpc": "2.0", "method": "ping", "params": [], "id": 1}, ct="application/json", tries=1, timeout=5)
    ch["betterbibtex"] = {"ok": st in (200, 400, 500), "detail": (body[:120].decode("utf8", "replace") if body else err)}
    prefs = _zotero_prefs()
    db = os.path.join(prefs.get("dataDir", ""), "zotero.sqlite")
    ch["sqlite_readonly"] = {"ok": os.path.isfile(db), "detail": db}
    ch["file_handoff"] = {"ok": True, "detail": "always available: drag runs/<id>/bib/recs.ris into Zotero"}
    order = [c for c in prof["zotero"]["channels"] if ch.get(c, {}).get("ok")]
    chosen = next((c for c in ("local_api", "connector") if c in order), "file_handoff")
    res = {"probed_at": now(), "base": base, "channels": ch, "recommended_channel": chosen,
           "available": order}
    json.dump(res, open(run.path("zotero_channels.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("  channels:")
    for k, v in ch.items():
        print(f"    {'OK  ' if v['ok'] else 'NO  '}{k:<16} {str(v['detail'])[:96]}")
    if not ch["local_api"]["ok"]:
        print("  HINT 本地 API 不可读：让 Zotero 处于运行中，并在 设置→高级 勾选允许本机应用通信（路径为 /api/，不是旧文档里的 /bapi/），否则查重退到 sqlite 副本。")
    print("  HINT 走 connector 通道时，条目会落到 Zotero 当前选中的分类；导入前请先在 Zotero 里选中目标 collection。")
    ok(f"recommended channel = {chosen}")
    return res


# ------------------------------------------------------------------ idempotency
def existing(run):
    """DOIs/titles already in the library, so a re-run never duplicates items."""
    prof = run.profile
    base = prof["zotero"]["base"].rstrip("/")
    out = {"dois": set(), "titles": set(), "keys": {}, "source": None}
    st, body, err = http_get(base + "/api/users/0/items?format=json&limit=100", profile=prof,
                             tries=1, timeout=20, headers={"Zotero-API-Version": "3"})
    if st == 200:
        try:
            data = json.loads(body.decode("utf8", "replace"))
        except Exception:
            data = None
        if isinstance(data, list) and data:
            for it in data:
                d = norm_doi((it.get("data") or {}).get("DOI", ""))
                if d:
                    out["dois"].add(d)
                t = norm_title((it.get("data") or {}).get("title", ""))
                if t:
                    out["titles"].add(t)
            out["keys"] = {it.get("key"): (it.get("data") or {}).get("title", "")[:60]
                           for it in data if it.get("key")}
            out["source"] = "local_api"
            out["note"] = "本地 API 首页样本；库很大时改用 sqlite 通道取全量" if len(data) >= 100 else None
            return out
        # A 200 with an empty list means the endpoint answered but gave us nothing usable.
        # Trusting it would silently defeat de-duplication, so fall through to the read-only DB.
        out["error"] = f"local_api returned 200 but 0 items (st={st}); falling back to sqlite"
    prefs = _zotero_prefs()
    db = os.path.join(prefs.get("dataDir", ""), "zotero.sqlite")
    if os.path.isfile(db):
        tmp = os.path.join(tempfile.gettempdir(), f"zot_ro_{uuid.uuid4().hex[:6]}.sqlite")
        try:
            shutil.copy2(db, tmp)
            con = sqlite3.connect(f"file:{tmp.replace(os.sep,'/')}?mode=ro", uri=True)
            for field, bucket in (("DOI", "dois"), ("title", "titles")):
                try:
                    rows = con.execute(
                        "SELECT DISTINCT idv.value FROM itemData id "
                        "JOIN itemDataValues idv ON idv.valueID=id.valueID "
                        "JOIN fieldNames fn ON fn.fieldID=id.fieldID "
                        "JOIN items i ON i.itemID=id.itemID "
                        "WHERE fn.fieldName=? AND i.deleted IS NULL", (field,)).fetchall()
                    for (v,) in rows:
                        bucket_val = norm_doi(v or "") if field == "DOI" else norm_title(v or "")
                        if bucket_val:
                            out[bucket].add(bucket_val)
                except sqlite3.Error:
                    pass
            try:
                n = con.execute("SELECT COUNT(*) FROM items WHERE deleted IS NULL").fetchone()[0]
                out["item_count"] = n
            except sqlite3.Error:
                pass
            con.close()
            out["source"] = "sqlite_readonly(copy)"
        except sqlite3.Error as e:
            out["error"] = f"sqlite: {e}"
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not out["dois"]:
        p = run.path("zotero_state.json")
        if os.path.isfile(p):
            prev = json.load(open(p, encoding="utf-8"))
            out["dois"] |= {norm_doi(x) for x in prev.get("imported_dois", [])}
            out["source"] = out["source"] or "previous_run_state"
    return out


# ------------------------------------------------------------------ import
RETRACT_URL_MAX = 50


def retraction_gate(run, recs):
    """One bulk OpenAlex query before anything reaches the library.

    Importing a retracted paper is the one failure mode a literature tool cannot undo
    politely later; records without a DOI are reported as unchecked, never as clean.
    The filter `is_retracted:true,doi:A|B|C` was verified live on 2026-09-21."""
    from .harvest import _oa_auth
    if not run.profile["zotero"].get("retraction_check", True):
        return {"status": "skipped", "reason": "zotero.retraction_check=false"}
    dois = sorted({r["doi"] for r in recs if r.get("doi")})
    unchecked = [r.get("title", "")[:60] for r in recs if not r.get("doi")]
    flagged, calls = [], 0
    for i in range(0, len(dois), RETRACT_URL_MAX):
        params = _oa_auth(run.profile)
        params["filter"] = "is_retracted:true,doi:" + "|".join(dois[i:i + RETRACT_URL_MAX])
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data, err = get_json(url, profile=run.profile, timeout=30)
        calls += 1
        if err:
            print(f"  WARN 撤稿核查不可用（{err}）—— 这批条目未经撤稿筛查，请手动确认或稍后重试")
            return {"status": "unavailable", "error": err, "checked": len(dois)}
        for w in data.get("results") or []:
            flagged.append(str(w.get("doi") or "").replace("https://doi.org/", ""))
    out = {"status": "ok", "checked": len(dois), "calls": calls, "retracted": flagged,
           "no_doi_unchecked": unchecked}
    json.dump(out, open(run.path("bib", "retraction_check.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    if flagged:
        print(f"  ✗ 检出 {len(flagged)} 篇已被撤稿：{flagged}")
        print("    请在 05_candidates.csv 把这些行的 verdict 改成 drop，重跑 review-apply 后再写库。")
        die("撤稿论文不得导入文献库")
    print(f"  撤稿核查通过：{len(dois)} 个 DOI 已查（OpenAlex），"
          + (f"{len(unchecked)} 条无 DOI 未能核查" if unchecked else "全部可核验"))
    return out


def build(run):
    sel = read_jsonl(run.path("06_selected.jsonl")) or read_jsonl(run.path("05_candidates.jsonl"))
    if not sel:
        die("no selected records — run review apply first")
    os.makedirs(run.path("bib"), exist_ok=True)
    retraction_gate(run, sel)
    write_ris(run, sel, run.path("bib", "recs.ris"))
    write_bib(run, sel, run.path("bib", "recs.bib"))
    items = [to_zotero_item(run, r) for r in sel]
    json.dump(items, open(run.path("bib", "zotero_items.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    ok(f"bib/ 生成 {len(sel)} 条：recs.ris, recs.bib, zotero_items.json")
    return sel, items


def import_items(run, channel=None, do_write=False, collection=None):
    if not do_write:
        die("写库需要显式 --yes（避免误建重复条目）。先看 zotero probe 与 bib/ 产物。")
    sel, items = build(run)
    prof = run.profile
    base = prof["zotero"]["base"].rstrip("/")
    ch = channel or prof["zotero"].get("prefer_channel", "auto")
    if ch in ("auto", None):
        ch = json.load(open(run.path("zotero_channels.json"), encoding="utf-8"))["recommended_channel"] \
            if os.path.isfile(run.path("zotero_channels.json")) else "connector"
    ex = existing(run)
    keep_idx, skipped = [], []
    for i, (r, it) in enumerate(zip(sel, items)):
        d = norm_doi(it.get("DOI", ""))
        t = norm_title(it.get("title", ""))
        if (d and d in ex["dois"]) or (not d and t and t in ex["titles"]):
            skipped.append({"uid": r["uid"], "doi": d, "title": it["title"][:60], "why": "库中已存在"})
            continue
        keep_idx.append(i)
    if skipped:
        print(f"  已跳过 {len(skipped)} 条（查重命中，source={ex['source']}）")
    new_items = [items[i] for i in keep_idx]
    if not new_items:
        ok("没有需要新建的条目（全部命中查重）")
        return {"imported": 0, "skipped": len(skipped)}

    result = {"channel": ch, "requested": len(new_items)}
    if ch == "connector":
        session = "litpipe-" + uuid.uuid4().hex[:8]
        payload = {"sessionID": session, "uri": "http://localhost:23119/", "items": new_items}
        st, body, err = http_get(base + "/connector/saveItems", profile=prof, method="POST", body=payload,
                                ct="application/json", tries=2, timeout=60,
                                headers={"X-Zotero-Connector-API-Version": "3"})
        result.update({"status": st, "error": err, "response_head": (body or b"")[:300].decode("utf8", "replace")})
        keys = re.findall(r'"key"\s*:\s*"([A-Z0-9]{8})"', (body or b"").decode("utf8", "replace"))
        result["keys"] = keys
    elif ch == "local_api":
        sid = server_id(run)
        lib = prof["zotero"].get("library_id", 0)
        lw = LocalWrite(run, sid)
        col = None
        if collection:
            st, resp = lw.write("POST", f"/users/{lib}/collections", {"name": collection})
            if st in (200, 201) and isinstance(resp, dict):
                col = resp.get("key")
        payload = [{"itemType": it["itemType"], "creators": it.get("creators", []), "tags": it.get("tags", []),
                    "collections": [col] if col else [], **{k: v for k, v in it.items()
                                                            if k not in ("itemType", "creators", "tags", "collections", "attachments")},
                    **({"links": {"self": {}}} if False else {})} for it in new_items]
        st, resp = lw.write("POST", f"/users/{lib}/items", payload)
        head = json.dumps(resp, ensure_ascii=False)[:300] if isinstance(resp, dict) else str(resp)[:300]
        result.update({"status": st, "error": None if st in (200, 201) else head,
                       "collection": col, "response_head": head})
        if isinstance(resp, dict):
            result["failed"] = resp.get("failed")
    else:
        result.update({"status": "handoff", "file": run.path("bib", "recs.ris"),
                       "instruction": "把该文件拖进 Zotero 窗口（或 文件→导入），KW 行会变成标签"})

    st_p = run.path("zotero_state.json")
    prev = json.load(open(st_p, encoding="utf-8")) if os.path.isfile(st_p) else {"imported_dois": [], "history": []}
    prev["imported_dois"] = sorted({*prev.get("imported_dois", []), *[norm_doi(items[i].get("DOI", "")) for i in keep_idx if items[i].get("DOI")]})
    prev["history"].append({"at": now(), "channel": ch, "imported": len(new_items), "skipped": len(skipped), "status": result.get("status")})
    json.dump(prev, open(st_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    run.save_state("zotero_import", **{k: result.get(k) for k in ("channel", "status", "requested")})
    print("  " + json.dumps({k: v for k, v in result.items() if k != "response_head"}, ensure_ascii=False)[:400])
    if result.get("status") not in (200, 201, "handoff", None):
        print("  WARN 导入返回非 2xx：改用 file_handoff（拖入 bib/recs.ris）或先跑 zotero probe 确认可用通道")
    return result


def verify(run):
    sel = read_jsonl(run.path("06_selected.jsonl")) or []
    ex = existing(run)
    hit = sum(1 for r in sel if norm_doi(r.get("doi", "")) in ex["dois"] or norm_title(r.get("title", "")) in ex["titles"])
    print(f"  selected={len(sel)}  库中可匹配={hit}  缺失={len(sel)-hit}  (读取方式 {ex['source']})")
    for r in sel:
        if norm_doi(r.get("doi", "")) not in ex["dois"] and norm_title(r.get("title", "")) not in ex["titles"]:
            print(f"    MISSING {r['uid']} {(r.get('title') or '')[:60]}")
    ok("回读校验完成" if hit == len(sel) else "存在未落库条目，检查上面 MISSING 清单")
    return hit, len(sel)


# ------------------------------------------------------------------ attachment mounting
ITEM_TYPES_WITH_DOI = "journalArticle || preprint || conferencePaper || bookSection || thesis || report || book"


def _top_items(run):
    """Page through top-level items. `?q=` is useless here — the local index carries no DOI/path
    field (measured 0 hits), so candidates are pulled whole and matched client-side."""
    out, start, limit = [], 0, 100
    while True:
        qs = urllib.parse.urlencode({"itemType": ITEM_TYPES_WITH_DOI, "format": "json",
                                     "limit": limit, "start": start})
        st, data, _ = _zapi_get(run, f"/users/0/items?{qs}", timeout=30)
        if st != 200:
            die(f"条目拉取失败 HTTP {st}：{data if isinstance(data, str) else ''}")
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < limit:
            break
        start += limit
    return out


def _match_maps(items):
    """Build the parent-item lookup. `?q=` can't do this job (no DOI/path in the local index), so
    everything is matched client-side. Order of trust: the `LitPipe uid:` marker that
    to_zotero_item writes into extra at import time, then DOI, then arXiv id, then title.
    Anything ambiguous is treated as no match — mounting onto the wrong paper is worse than not
    mounting."""
    by_uid, by_doi, by_arxiv, by_title = {}, {}, {}, {}
    dup_titles = set()
    for it in items:
        d = it.get("data") or {}
        kt = (it.get("key"), (d.get("title") or "")[:50])
        m = re.search(r"LitPipe uid:\s*(\w{6,})", d.get("extra") or "")
        if m:
            by_uid.setdefault(m.group(1), kt)
        nd = norm_doi(d.get("DOI", ""))
        if nd:
            by_doi.setdefault(nd, []).append(kt)
        na = norm_arxiv(d.get("archiveID", ""))
        if na:
            by_arxiv.setdefault(na, []).append(kt)
        nt = norm_title(d.get("title", ""))
        if nt:
            if nt in by_title:
                dup_titles.add(nt)
            by_title[nt] = kt
    return by_uid, by_doi, by_arxiv, by_title, dup_titles


def _find_parent(r, maps):
    by_uid, by_doi, by_arxiv, by_title, dup_titles = maps
    if r.get("uid") in by_uid:
        return by_uid[r["uid"]], "uid"
    hits = by_doi.get(norm_doi(r.get("doi", ""))) or []
    if len(hits) == 1:
        return hits[0], "doi"
    hits = by_arxiv.get(norm_arxiv(r.get("arxiv_id", ""))) or []
    if len(hits) == 1:
        return hits[0], "arxiv"
    nt = norm_title(r.get("title", ""))
    if nt and nt not in dup_titles:
        return by_title[nt], "title"
    return None, ""


def _child_data(run, lib, parent_key):
    st, kids, _ = _zapi_get(run, f"/users/{lib}/items/{parent_key}/children")
    return [k.get("data") or {} for k in kids if isinstance(k, dict)] if st == 200 and isinstance(kids, list) else []


def attach(run, do_write=False, with_tags=False):
    """Mount local PDFs as linked_file child attachments, optionally pushing the tag schema.

    POST /items/<key>/children answers 405 on the local API, so children go through the
    array-style POST /users/<lib>/items whose body carries parentItem (measured 200)."""
    sel = read_jsonl(run.path("06_selected.jsonl")) or []
    if not sel:
        die("no selected records — run review apply first")
    if not run.has("07_pdf_status.json"):
        die("07_pdf_status.json missing — run `pdfs fetch` first")
    status = json.load(open(run.path("07_pdf_status.json"), encoding="utf-8"))
    lib = run.profile["zotero"].get("library_id", 0)
    sid = server_id(run)
    if not sid:
        die("取不到 Zotero-Server-ID，无法写库")
    lw = LocalWrite(run, sid)
    maps = _match_maps(_top_items(run))
    print(f"  库内可匹配条目：{len(maps[0])} 条带 LitPipe uid")

    rows, skipped = [], []
    for r in sel:
        s = status.get(r["uid"]) or {}
        if s.get("status") not in ("downloaded", "cached"):
            continue
        p = (s.get("path") or r.get("pdf_local_path") or "").strip()
        parent, how = _find_parent(r, maps)
        if not parent:
            skipped.append({"uid": r["uid"],
                            "why": f"库内无唯一可确认的父条目（uid/DOI/arXiv/标题均未唯一命中，doi={r.get('doi') or '缺'}）"})
            continue
        pkey, ptitle = parent
        # one linked_file child per paper is the idempotency rule; the recorded status path can be
        # stale (a browser-fetched file keeps the publisher's name), so trust what the library holds
        kids = [c for c in _child_data(run, lib, pkey) if c.get("linkMode") == "linked_file"]
        if kids:
            rows.append({"uid": r["uid"], "parent": pkey, "parent_title": ptitle, "matched_by": how,
                         "path": kids[0].get("path") or p, "already": True,
                         "tags": r.get("zotero_tags") or []})
            continue
        if not p or not os.path.isfile(p):
            skipped.append({"uid": r["uid"], "why": f"待挂 PDF 文件不存在：{p or '未记录路径'}（该条目库里也没有 linked_file 附件）"})
            continue
        rows.append({"uid": r["uid"], "parent": pkey, "parent_title": ptitle, "matched_by": how,
                     "path": os.path.abspath(p), "already": False,
                     "tags": r.get("zotero_tags") or []})
    for s in skipped:
        print(f"  ⚠️ 跳过 {s['uid']}：{s['why']}")
    if not rows:
        ok("没有可挂载的条目（PDF 缺失或 DOI 未命中）")
        return {"planned": 0, "skipped": len(skipped)}

    to_mount = [x for x in rows if not x["already"]]
    print(f"  计划：挂载 {len(to_mount)} 个附件，已存在 {len(rows) - len(to_mount)} 个"
          + (f"，同步标签 {len(rows)} 条" if with_tags else ""))
    for x in rows:
        print(f"    {'[已挂]' if x['already'] else '[待挂]'} {x['uid']} → {x['parent']}（按{x['matched_by']}命中）｜ {x['path']}")
    if not do_write:
        ok("dry-run：以上是待写内容。加 --yes 才真正写库（首次会弹 Zotero 授权框，请点始终允许）")
        return {"planned": len(to_mount), "skipped": len(skipped), "already": len(rows) - len(to_mount)}

    mounted = failed = tag_ok = tag_fail = 0
    for x in rows:
        if not x["already"]:
            body = [{"itemType": "attachment", "parentItem": x["parent"], "linkMode": "linked_file",
                     "path": x["path"], "title": os.path.basename(x["path"]),
                     "contentType": "application/pdf", "charset": ""}]
            st, resp = lw.write("POST", f"/users/{lib}/items", body)
            key = ((resp or {}).get("successful") or {}).get("0", {}).get("key") if isinstance(resp, dict) else None
            if st == 200 and key:
                mounted += 1
                x["attachment_key"] = key
                print(f"  ✅ {x['uid']} → 附件 {key}")
            else:
                failed += 1
                print(f"  ✗ {x['uid']} 挂载失败 HTTP {st}：{str(resp)[:160]}")
        if with_tags and x["tags"]:
            st, cur, _ = _zapi_get(run, f"/users/{lib}/items/{x['parent']}")
            data = (cur or {}).get("data") or {} if isinstance(cur, dict) else {}
            ver = (cur or {}).get("version") if isinstance(cur, dict) else None
            # Same-prefix tags are replaced (状态/待读 → 状态/到手 must not coexist), tags from
            # other prefixes stay — that's where the user's own notes live.
            ours = {t.split("/", 1)[0] for t in x["tags"] if "/" in t}
            kept = [t for t in (data.get("tags") or [])
                    if t.get("tag", "").split("/", 1)[0] not in ours]
            known = {t.get("tag") for t in kept}
            newtags = kept + [{"tag": t, "type": 1} for t in x["tags"] if t not in known]
            if {t.get("tag") for t in newtags} != {t.get("tag") for t in (data.get("tags") or [])}:
                st, resp = lw.write("PATCH", f"/users/{lib}/items/{x['parent']}",
                                    {"tags": newtags},
                                    extra_headers={"If-Unmodified-Since-Version": str(ver)} if ver else None)
                if st in (200, 204):
                    tag_ok += 1
                    print(f"  ✅ {x['uid']} 标签已同步（{len(newtags)} 条）")
                else:
                    tag_fail += 1
                    print(f"  ✗ {x['uid']} 标签写入失败 HTTP {st}：{str(resp)[:160]}")

    confirmed = dangling = unconfirmed = 0
    for x in rows:
        paths = [c.get("path", "") for c in _child_data(run, lib, x["parent"])]
        hit = [p for p in paths if os.path.normcase(p) == os.path.normcase(x["path"])]
        if not hit:
            unconfirmed += 1
            print(f"  🔴 回读未确认：{x['parent']} 下没有该附件（{x['path']}）")
        elif not os.path.isfile(hit[0]):
            dangling += 1
            print(f"  ⚠️ 附件在库、文件不在磁盘：{x['parent']} ｜ {hit[0]}")
        else:
            confirmed += 1
    out = {"at": now(), "mounted": mounted, "mount_failed": failed,
           "already": len(rows) - len(to_mount), "tags_updated": tag_ok, "tags_failed": tag_fail,
           "readback_confirmed": confirmed, "readback_dangling": dangling,
           "readback_unconfirmed": unconfirmed, "skipped": skipped, "rows": rows}
    json.dump(out, open(run.path("zotero_attach_state.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    if failed or unconfirmed:
        die(f"挂载未全部成功（写入失败 {failed} / 回读未确认 {unconfirmed}），明细见 zotero_attach_state.json")
    if dangling:
        print(f"  WARN {dangling} 个附件的 linked_file 路径打不开——通常是课题目录被移动过，"
              f"需先修 07_pdf_status.json 与库内路径再重跑")
    ok(f"完成：新增挂载 {mounted}，已存在 {out['already']}，标签同步 {tag_ok}，"
       f"回读 {confirmed} 可开 / {dangling} 路径失效 / {unconfirmed} 未确认")
    return out


def find_pdf_list(run):
    sel = read_jsonl(run.path("06_selected.jsonl")) or []
    need = [r for r in sel if not (r.get("pdf_url") or r.get("oa_url"))]
    with open(run.path("zotero-find-pdf.md"), "w", encoding="utf-8") as fh:
        fh.write("# 交给 Zotero 查找 PDF 的清单\n\n"
                 "在 Zotero 中选中下面的条目 → 右键 → 附件 → “Find Available PDF”。\n"
                 "（走本地 API 未开启时无法远程触发；也可在 Connector 通道导入时自带 URL 附件。）\n\n")
        for r in need:
            fh.write(f"- [ ] {r['uid']} ｜ {r.get('doi') or r.get('arxiv_id') or '无标识符'} ｜ {(r.get('title') or '')[:80]}\n")
    ok(f"zotero-find-pdf.md：{len(need)} 条无 OA 直链，需在 Zotero 内查找")
    return need
