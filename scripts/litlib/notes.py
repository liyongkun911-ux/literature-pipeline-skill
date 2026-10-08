"""Reading navigation: per-paper guide, three-pass ordering, paper x dimension matrix.

The judgement text is authored by the model into reading_notes.jsonl; this module only
assembles, validates and orders it, so no fabricated field ever comes from the script.
"""
import csv
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict

from .common import (Run, die, ok, read_jsonl, write_jsonl)

REQUIRED = ["研究问题", "方法与技术路线", "主要结论", "可复用点", "局限与疑点", "与本主题的关联", "精读章节指引"]
ALL_FIELDS = ["研究问题", "核心假设", "方法与技术路线", "数据或实验设置", "主要结论", "可复用点",
              "局限与疑点", "与本主题的关联", "精读章节指引", "预计用时"]
PASS_ORDER = {"survey": 0, "method": 1, "benchmark": 2, "application": 3, "critique": 4}
PASS_NAME = {0: "第 1 遍 建立全景（综述/奠基）", 1: "第 2 遍 吃透方法主线", 2: "第 2 遍 基准与可对比性",
             3: "第 2 遍 应用与场景证据", 4: "第 3 遍 争议与缺口"}

SKIP_FIELDS = {"预计用时"}
SUMMARY_MARK = "仅摘要"
# 英文式页引：p/pp./p12/p8-11（前面不许接字母数字，避免命中 "第 700 episode" 里的 p 或单词内部）
PAGE_CITE_EN = re.compile(r"(?<![A-Za-z0-9])pp?\.?\s*(\d+)(?:\s*[-–~]\s*(\d+))?(?![0-9])")
# 中文式页引：必须带「页」，否则 "第 700 episode" 会被误读成 p700
PAGE_CITE_ZH = re.compile(r"第\s*(\d+)(?:\s*[-–~至]\s*(\d+))?\s*页")
MEASURE = re.compile(r"\d+\.\d+|\d+(?:\.\d+)?\s*%|±")
YEAR_LIKE = re.compile(r"^(?:19|20)\d{2}$")
PAGE_TOLERANCE = 1   # 版权页/卷首页会让稿内页码与 pdfinfo 页序差 1
THIN_CHARS = 1500    # 全篇可抽字符低于此值视为文字层不可用（扫描版/图片型）


def template(run, force=False):
    recs = read_jsonl(run.path("06_selected.jsonl"))
    if not recs:
        die("06_selected.jsonl missing")
    path = run.path("reading_notes.jsonl")
    if os.path.isfile(path) and not force:
        ok(f"reading_notes.jsonl 已存在（{len(read_jsonl(path))} 条），不覆盖；需要重建加 --force")
        return path
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in recs:
            row = {"uid": r["uid"], "notes": {k: "" for k in ALL_FIELDS}, "pass_hint": r.get("role") or "method",
                   "depends_on": []}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  每篇必须填写的字段：{REQUIRED}")
    print("  内容来源只能是摘要/全文，不得凭标题推断；填完再跑 notes build")
    ok(f"reading_notes.jsonl 模板已生成（{len(recs)} 行）")
    return path


def _check(rec):
    n = (rec.get("notes") or {})
    missing = [f for f in REQUIRED if not str(n.get(f, "")).strip()]
    return missing


def build(run, force=False):
    sel = read_jsonl(run.path("06_selected.jsonl"))
    if not sel:
        die("06_selected.jsonl missing")
    if not run.has("reading_notes.jsonl"):
        die("先跑 `notes template` 生成 reading_notes.jsonl 并填写，再 build")
    notes = {r["uid"]: r for r in read_jsonl(run.path("reading_notes.jsonl"))}
    pdfst = json.load(open(run.path("07_pdf_status.json"), encoding="utf-8")) if run.has("07_pdf_status.json") else {}
    by_uid = {r["uid"]: r for r in sel}

    incomplete, rows = [], []
    for r in sel:
        nt = notes.get(r["uid"]) or {"notes": {}, "depends_on": [], "pass_hint": r.get("role")}
        miss = _check(nt)
        if miss:
            incomplete.append({"uid": r["uid"], "title": (r.get("title") or "")[:70], "missing": miss})
        p = pdfst.get(r["uid"], {})
        rows.append({"rec": r, "nt": nt, "pdf": p})

    rows.sort(key=lambda x: (PASS_ORDER.get((x["nt"].get("pass_hint") or x["rec"].get("role") or "method"), 2),
                             -(x["rec"].get("score") or 0)))
    total_min = 0
    order_json, md = [], [f"# {run.profile.get('topic') or run.run_id} — 阅读导航", ""]
    md.append("| 顺序 | 篇 | 遍次 | 优先 | 角色 | 全文 | 预计 | 一句话 |")
    md.append("|--|--|--|--|--|--|--|--|")
    for i, x in enumerate(rows, 1):
        r, nt, p = x["rec"], x["nt"], x["pdf"]
        mins = _mins(nt["notes"].get("预计用时", ""))
        total_min += mins
        pw = PASS_ORDER.get((nt.get("pass_hint") or r.get("role") or "method"), 2)
        md.append(f"| {i} | `{r['uid']}` | {i_pass(pw)} | {r.get('priority','P?')} | "
                  f"{r.get('role','')} | {'本地' if p.get('status') in ('downloaded','cached') else ('需手动' if p else '待查')} | "
                  f"{mins}min | {str(nt['notes'].get('研究问题',''))[:40]} |")
        order_json.append({"order": i, "uid": r["uid"], "pass": i_pass(pw), "minutes": mins,
                           "pdf": p.get("path", ""), "depends_on": nt.get("depends_on") or []})

    md += ["", f"**总预计用时：{total_min//60}h{total_min%60:02d}m**（按 {len(rows)} 篇累加）", "",
           "## 建议分遍阅读", ""]
    groups = defaultdict(list)
    for o in order_json:
        groups[o["pass"]].append(o)
    for pw in sorted(groups):
        md.append(f"### {pw}")
        md.append("")
        for o in groups[pw]:
            r = by_uid[o["uid"]]
            nt = (notes.get(o["uid"]) or {}).get("notes", {})
            md.append(f"#### {o['order']}. {r.get('title','')[:110]}  ")
            md.append(f"`{o['uid']}` ｜ {r.get('venue') or (r.get('venue_resolved') or {}).get('venue','')} ｜ "
                      f"{r.get('year')} ｜ 分 {r.get('score')} ｜ DOI `{r.get('doi') or '-'}` ｜ "
                      f"标签 `{'`, `'.join(r.get('zotero_tags') or [])}`")
            pdf = o.get("pdf") or "（无本地 PDF，先看 需手动获取 清单）"
            md.append(f"- 本地全文：`{pdf}`")
            for f in ALL_FIELDS:
                v = str(nt.get(f, "")).strip()
                md.append(f"- **{f}**：{v or '（未填）'}")
            deps = [d for d in (o.get("depends_on") or []) if d in by_uid]
            if deps:
                md.append(f"- 建议先读：{', '.join(deps)}")
            md.append("")

    if incomplete:
        json.dump(incomplete, open(run.path("08_notes_incomplete.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"  {len(incomplete)}/{len(rows)} 篇判读未填满：")
        for x in incomplete[:15]:
            print(f"    {x['uid']} 缺 {','.join(x['missing'])} ｜ {x['title'][:50]}")
        if len(incomplete) > 15:
            print(f"    …其余 {len(incomplete) - 15} 篇见 08_notes_incomplete.json")
        if not force:
            die("判读没填满就不出导航稿：空壳 reading-nav.md 会被当成「读过的结论」引用，比不写更危险。"
                "补齐 reading_notes.jsonl 后重跑，或加 --force 出带未完成标记的草稿")
        md.insert(1, f"> ⚠️ **未完成草稿**：{len(incomplete)}/{len(rows)} 篇判读字段空缺，"
                     f"这些篇的结论不可引用（清单见 `08_notes_incomplete.json`）。")

    matrix_cols = ["顺序", "uid", "优先", "角色", "年份", "子方向", "方法", "全文"] + ALL_FIELDS
    with open(run.path("reading-matrix.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(matrix_cols)
        for o in order_json:
            r = by_uid[o["uid"]]
            nt = (notes.get(o["uid"]) or {}).get("notes", {})
            e = r.get("enrich") or {}
            p = pdfst.get(o["uid"], {})
            w.writerow([o["order"], o["uid"], r.get("priority"), r.get("role"), r.get("year"),
                        e.get("subtopic_label", ""), e.get("method_label", ""),
                        "本地" if p.get("status") in ("downloaded", "cached") else "需手动"]
                       + [re_sub(nt.get(f, "")) for f in ALL_FIELDS])

    open(run.path("reading-nav.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    json.dump(order_json, open(run.path("reading-order.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    run.save_state("notes", papers=len(rows), incomplete=len(incomplete), est_minutes=total_min)
    print(f"  reading-nav.md 生成：{len(rows)} 篇，预计 {total_min} 分钟"
          + (f"（含 {len(incomplete)} 篇未完成草稿，见顶部标记）" if incomplete else ""))
    ok("reading-nav.md / reading-matrix.csv / reading-order.json 写出")
    return order_json, incomplete


def re_sub(v):
    return str(v).replace("\n", " ").strip()[:120]


# ---------------------------------------------------------------- audit
def _cites(text):
    """返回 [(起始页, 结束页 or None)]；中英两种页引写法都收。"""
    out = []
    for m in PAGE_CITE_EN.finditer(text):
        out.append((int(m.group(1)), int(m.group(2)) if m.group(2) else None))
    for m in PAGE_CITE_ZH.finditer(text):
        out.append((int(m.group(1)), int(m.group(2)) if m.group(2) else None))
    return sorted(set(out), key=lambda x: (x[0], x[1] or x[0]))


def _measures(text):
    """字段里可跨语言核验的凭据：小数与百分数。
    表/图编号中英写法不同（"表9" vs "Table 9"）不参与核验；
    arXiv 号（2511.01083）、DOI 前缀（10.71443）这类标识符一律排除，它们不是测量值。
    """
    toks = []
    for m in re.findall(r"\d+\.\d+|\d+(?:\.\d+)?\s*%", text):
        t = m.strip()
        if YEAR_LIKE.match(t):
            continue
        if re.fullmatch(r"\d{4}\.\d{4,5}", t):      # arXiv 号
            continue
        if re.fullmatch(r"10\.\d{3,}", t):          # DOI 前缀
            continue
        toks.append(t)
    return toks


def _pdf_pages(path):
    exe = shutil.which("pdfinfo")
    if not (exe and os.path.isfile(path)):
        return 0
    try:
        r = subprocess.run([exe, path], capture_output=True, timeout=60,
                           encoding="utf-8", errors="replace")
    except Exception:
        return 0
    m = re.search(r"^Pages:\s*(\d+)", r.stdout or "", re.M)
    return int(m.group(1)) if m else 0


def _page_texts(path):
    """一次抽全文再按 \\f 切页，避免逐页调 pdftotext；抽不出文字层则返回 {}。"""
    exe = shutil.which("pdftotext")
    if not (exe and os.path.isfile(path)):
        return {}
    try:
        r = subprocess.run([exe, "-layout", path, "-"], capture_output=True, timeout=180)
    except Exception:
        return {}
    if r.returncode != 0 or not r.stdout:
        return {}
    txt = r.stdout.decode("utf-8", "replace")
    pages = txt.split("\f")
    return {i: re.sub(r"\s+", "", p) for i, p in enumerate(pages, 1)}


def _norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def _found(tok, cites, ptext, tol=PAGE_TOLERANCE):
    """在引用页 ±tol 范围内找凭据：容忍「抽文首页是版权页」这类整体偏移。"""
    t = _norm(tok)
    for a, b in cites:
        for pg in range(max(1, a - tol), (b or a) + tol + 1):
            if t in ptext.get(pg, ""):
                return pg
    return 0


def audit(run, use_text=True):
    """判读页码审计：抓「有数字没页码」「页码越界」「该标仅摘要没标」「页引与页内容不符」。

    只报可复核的差异，每条都带命中/未命中的原始凭据，不做黑箱判定。
    内容级核验依赖 pdftotext 抽出的文字层，扫描版/图片型 PDF 抽不出时会如实降级。
    页序容差 ±1：实测有些稿的抽文首页是版权页/卷首页，稿内 "Page k of N" 与 pdfinfo 页序差 1。
    """
    sel = read_jsonl(run.path("06_selected.jsonl"))
    if not sel:
        die("06_selected.jsonl missing")
    if not run.has("reading_notes.jsonl"):
        die("先跑 `notes template` 并填好 reading_notes.jsonl，再 audit")
    notes = {r["uid"]: r for r in read_jsonl(run.path("reading_notes.jsonl"))}
    pdfst = json.load(open(run.path("07_pdf_status.json"), encoding="utf-8")) if run.has("07_pdf_status.json") else {}

    rows, thin, degraded, soft_range = [], [], 0, []
    for r in sel:
        uid = r["uid"]
        nt = (notes.get(uid) or {}).get("notes") or {}
        p = pdfst.get(uid) or {}
        path = p.get("path") or ""
        has_pdf = bool(path) and os.path.isfile(path)
        pages = _pdf_pages(path) if has_pdf else 0
        ptext = _page_texts(path) if (has_pdf and use_text) else {}
        chars = sum(len(v) for v in ptext.values())
        thin_layer = bool(ptext) and chars < THIN_CHARS
        if has_pdf and use_text and not ptext:
            thin.append(uid)
        # 本字段之外的页引：用于区分「数字根本不存在」与「数字存在但页引放错了字段」
        paper_cites = sorted({c for f2, v2 in nt.items() if f2 not in SKIP_FIELDS
                              for c in _cites(str(v2 or ""))}, key=lambda x: (x[0], x[1] or x[0]))
        for f, v in nt.items():
            if f in SKIP_FIELDS:
                continue
            v = str(v or "").strip()
            if not v:
                continue
            cites = _cites(v)
            cited = "/".join(f"p{a}" + (f"-{b}" if b else "") for a, b in cites) or "-"
            for a, b in cites:
                hi = max(a, b or a)
                if pages and hi > pages + PAGE_TOLERANCE:
                    rows.append((uid, f, "page_out_of_range",
                                 f"引用 p{hi}，该 PDF 只有 {pages} 页（已放宽 ±{PAGE_TOLERANCE}）", cited, ""))
                elif pages and hi > pages:
                    soft_range.append((uid, f, hi, pages))
            if not has_pdf and SUMMARY_MARK not in v:
                rows.append((uid, f, "summary_marker_missing",
                             "无本地全文的篇目，此字段未标「（仅摘要）」", "-", ""))
            toks = _measures(v)
            # 无本地全文的篇目没法核页码，只查「有没有标仅摘要」，不报「缺页码」
            # 与本主题的关联 按设计会掺入课题自己的方案数字（如"1% 预算"），不是文献事实，不查
            if has_pdf and not cites and toks and f != "与本主题的关联":
                rows.append((uid, f, "measure_without_page",
                             "含具体数字但没写页码/节", "-", "/".join(toks[:4])))
            if ptext and cites and toks and not thin_layer:
                miss = [t for t in toks if not _found(t, cites, ptext)]
                elsewhere = [t for t in miss if _found(t, paper_cites, ptext)]
                nowhere = [t for t in miss if t not in elsewhere]
                if nowhere:
                    rows.append((uid, f, "page_text_mismatch",
                                 f"页引 {cited} ±{PAGE_TOLERANCE} 页内没有，本文其他被引页也找不到",
                                 cited, "/".join(nowhere[:6])))
                if elsewhere:
                    rows.append((uid, f, "citation_misplaced",
                                 "数字在本篇其他被引页上有，但不在本字段的页引上", cited, "/".join(elsewhere[:6])))
        if thin_layer:
            rows.append((uid, "-", "text_layer_thin",
                         f"文字层可抽字符仅 {chars}（扫描版/图片型），内容级核验已跳过", "-", ""))

    out = run.path("08_notes_audit.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["uid", "field", "kind", "detail", "cited_pages", "miss"])
        w.writerows(rows)

    kinds = defaultdict(int)
    for x in rows:
        kinds[x[2]] += 1
    LABEL = {"page_out_of_range": "页码越界", "measure_without_page": "有数字没页码",
             "summary_marker_missing": "未标仅摘要", "page_text_mismatch": "页引对不上（本文他处也无）",
             "citation_misplaced": "页引放错字段（他处页有）",
             "text_layer_thin": "文字层不可用（降级）"}
    print(f"  审计 {len(sel)} 篇 × {len(ALL_FIELDS) - 1} 字段，发现问题 {len(rows)} 处：")
    if not rows:
        print("    无")
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"    {LABEL.get(k, k):<18} {n}")
    for x in rows[:25]:
        print(f"    · [{LABEL.get(x[2], x[2])}] {x[0]} / {x[1]}：{x[3]}" + (f" ｜ 缺 {x[5]}" if x[5] else ""))
    if len(rows) > 25:
        print(f"    …其余 {len(rows) - 25} 处见 08_notes_audit.csv")
    if soft_range:
        print(f"  另有 {len(soft_range)} 处越界仅超 ±{PAGE_TOLERANCE}（未计入，多为卷首页偏移，供你复核）："
              + ", ".join(f"{u[:8]}/{f} p{hi}>{p}页" for u, f, hi, p in soft_range[:6]))
    if thin:
        print(f"  降级：{len(thin)} 篇抽不出文字层（扫描版/图片型），已跳过内容级核验："
              + ", ".join(thin[:5]) + ("…" if len(thin) > 5 else ""))
        degraded += len(thin)
    if not shutil.which("pdftotext"):
        print("  提示：本机没有 pdftotext，内容级核验已跳过（只做了结构级检查）")
    elif not use_text:
        print("  提示：--no-text 已指定，跳过内容级核验")
    ok(f"08_notes_audit.csv 写出（{len(rows)} 处，其中降级 {degraded}）")
    run.save_state("notes_audit", findings=len(rows), papers=len(sel), thin=len(thin))
    return rows


def _mins(s):
    import re
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else 30


def i_pass(pw):
    return PASS_NAME.get(pw, "第 2 遍")
