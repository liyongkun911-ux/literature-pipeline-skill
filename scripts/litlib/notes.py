"""Reading navigation: per-paper guide, three-pass ordering, paper x dimension matrix.

The judgement text is authored by the model into reading_notes.jsonl; this module only
assembles, validates and orders it, so no fabricated field ever comes from the script.
"""
import csv
import json
import os
from collections import defaultdict

from .common import (Run, die, ok, read_jsonl, write_jsonl)

REQUIRED = ["研究问题", "方法与技术路线", "主要结论", "可复用点", "局限与疑点", "与本主题的关联", "精读章节指引"]
ALL_FIELDS = ["研究问题", "核心假设", "方法与技术路线", "数据或实验设置", "主要结论", "可复用点",
              "局限与疑点", "与本主题的关联", "精读章节指引", "预计用时"]
PASS_ORDER = {"survey": 0, "method": 1, "benchmark": 2, "application": 3, "critique": 4}
PASS_NAME = {0: "第 1 遍 建立全景（综述/奠基）", 1: "第 2 遍 吃透方法主线", 2: "第 2 遍 基准与可对比性",
             3: "第 2 遍 应用与场景证据", 4: "第 3 遍 争议与缺口"}


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


def _mins(s):
    import re
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else 30


def i_pass(pw):
    return PASS_NAME.get(pw, "第 2 遍")
