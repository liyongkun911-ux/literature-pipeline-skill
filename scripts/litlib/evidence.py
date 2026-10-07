"""Claim-level evidence bank and target-venue exemplar dossier.

Distilled from PaperSpine's citation_support_bank + journal-learning, with one change:
every claim is checked against files this pipeline actually produced. A number may only
be cited from a paper whose PDF is on disk with a page/section named, and an exemplar
observation must name the uid it came from — so "we learned the venue's style" cannot be
claimed from an empty file.
"""
import csv
import json
import math
import os
import re
from collections import Counter
from datetime import date

from .common import die, norm_title, ok, read_jsonl

CLAIM_COLS = ["claim_id", "uid", "section", "claim_sentence", "evidence_basis",
              "page_or_section", "source_channel", "verified", "verification_note", "status"]
SECTIONS = ["Introduction", "Related Work", "Method", "Results", "Discussion", "Limitations", "Background"]
BASES = ("abstract", "fulltext", "metadata")
# A claim that carries a measured quantity cannot come from a title or an abstract skim.
NUMERIC = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|×|倍|eV|keV|mAh|Ah|mAh/g|Wh/kg|mV|V\b|nm|μm|µm|Å|℃|K\b|MPa|GPa|kDa|ppm|fold|个月|小时|天)"
                     r"|\b(?:R2|Rs|p\s*[<>=]|PVC|IE|CE|efficiency|容量|电导率|循环次数)\s*[=<>]?\s*\d", re.I)
DOSSIER_SECTIONS = ["## Corpus", "## Official author guidance", "## Observed style in published papers",
                    "## Move table", "## Skeletons", "## Anti-patterns", "## Transfer plan"]


def _selected(run):
    recs = read_jsonl(run.path("06_selected.jsonl")) or read_jsonl(run.path("05_candidates.jsonl"))
    if not recs:
        die("没有选中集 — 先跑 review-apply（或至少 select）")
    return {r["uid"]: r for r in recs}, recs


def _pdf_status(run):
    f = run.path("07_pdf_status.json")
    return json.load(open(f, encoding="utf-8")) if os.path.isfile(f) else {}


def _recent(rec, years):
    return int(rec.get("year") or 0) > date.today().year - int(years)


def claims_template(run):
    prof = run.profile["evidence"]
    _, recs = _selected(run)
    mult = int(prof.get("claims_pool_multiplier", 3))
    os.makedirs(run.path("evidence"), exist_ok=True)
    rows = []
    for i in range(len(recs) * mult):
        r = recs[i % len(recs)]
        rows.append({"claim_id": f"C{i+1:03d}", "uid": r["uid"], "section": "", "claim_sentence": "",
                     "evidence_basis": "", "page_or_section": "", "source_channel": "",
                     "verified": "", "verification_note": "", "status": "draft"})
    p = run.path("evidence", "claims_template.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CLAIM_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"  为 {len(recs)} 篇各铺 {mult} 行 = {len(rows)} 行候选论断（PaperSpine 的比例是 3×，够挑不够凑）")
    print("  复制为 evidence/claims.csv 后填写：section / claim_sentence / evidence_basis"
          f"({'|'.join(BASES)}) / 出处 / source_channel / verified")
    ok(f"{p}")


def _audit(run, rows, by_uid, pdf):
    out, problems = [], []
    seen = set()
    for r in rows:
        cid = (r.get("claim_id") or "").strip()
        uid = (r.get("uid") or "").strip()
        rec = by_uid.get(uid) or {}
        sent = (r.get("claim_sentence") or "").strip()
        basis = (r.get("evidence_basis") or "").strip().lower()
        note = (r.get("verification_note") or "").strip()
        hard, soft = [], []
        if cid in seen:
            hard.append("claim_id 重复")
        seen.add(cid)
        if not rec:
            hard.append("uid 不在选中集")
        if len(sent) < 25:
            hard.append("论断句过短或为空（要写成能直接进稿子的句子）")
        elif norm_title(sent) == norm_title(rec.get("title")):
            hard.append("论断句就是标题复制，不算支撑句")
        if basis not in BASES:
            hard.append(f"evidence_basis 必须是 {'|'.join(BASES)}")
        sec = (r.get("section") or "").strip()
        if sec and sec not in SECTIONS:
            soft.append(f"section 写的是 `{sec}`，不在标准章节 {SECTIONS} 之内")
        if rec.get("is_retracted"):
            hard.append("该文已被标记撤稿，不得作为支撑")
        if basis == "fulltext":
            if (pdf.get(uid) or {}).get("status") not in ("downloaded", "cached"):
                hard.append("声称读完全文，但 pdfs/ 里没有这篇全文（先跑 pdfs fetch，或把 basis 改回 abstract）")
            if not (r.get("page_or_section") or "").strip():
                hard.append("全文类论断必须写明第几页/哪一节")
        elif NUMERIC.search(sent):
            hard.append("含测量数值的论断只能来自读过的全文（铁律9），摘要与标题不够")
        if (r.get("verified") or "").strip().lower() in ("yes", "true", "pass", "verified"):
            idents = [x for x in (rec.get("doi"), rec.get("arxiv_id"), rec.get("url")) if x]
            if not any(i in note for i in idents):
                soft.append("verified=yes 但核查记录里没有该篇的稳定标识（DOI/arXiv/URL）——自证不算已核")
        r["status"] = "blocked" if hard else ("self-attested" if soft else "verified")
        r["_why"] = "; ".join(hard + soft)
        out.append(r)
        if hard or soft:
            problems.append(f"{cid} [{r['status']}]: {r['_why']}")
    return out, problems


def claims_check(run):
    path = run.path("evidence", "claims.csv")
    if not os.path.isfile(path):
        die("缺 evidence/claims.csv — 先 `lit.py evidence claims-template`，复制成 claims.csv 填写")
    by_uid, recs = _selected(run)
    pdf = _pdf_status(run)
    with open(path, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh)]
    if not rows:
        die("evidence/claims.csv 是空的")
    out, problems = _audit(run, rows, by_uid, pdf)
    os.makedirs(run.path("evidence"), exist_ok=True)
    with open(run.path("evidence", "claims_audit.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CLAIM_COLS + ["_why"])
        w.writeheader()
        w.writerows(out)
    ev = run.profile["evidence"]
    used = [r for r in out if r["status"] == "verified" and (r.get("claim_sentence") or "").strip()]
    blocked = [r for r in out if r["status"] == "blocked"]
    selfatt = [r for r in out if r["status"] == "self-attested"]
    by_sec = Counter((r.get("section") or "(未填)") for r in used)
    recent = sum(1 for r in used if _recent(by_uid.get(r["uid"], {}), ev.get("recent_years", 3)))
    print(f"  论断 {len(out)} 行：已核验 {len(used)} · 自证待补 {len(selfatt)} · 拦下 {len(blocked)}")
    print(f"  分布：{json.dumps(dict(by_sec), ensure_ascii=False)}")
    if used:
        share = recent / len(used)
        floor = float(ev.get("min_recent_share", 0.6))
        if share < floor:
            print(f"  WARN 近 {ev.get('recent_years',3)} 年的论断只占 {share:.0%}（建议 ≥{floor:.0%}）"
                  f"—— 除非你的论断本就是奠基工作，否则说明候选清单太旧，回去调检索式")
        if max(by_sec.values()) > 0.6 * len(used) and len(by_sec) > 1:
            print(f"  WARN 论断集中在 {by_sec.most_common(1)[0][0]}，其他章节仍然没有支撑句")
    for p in problems[:20]:
        print("   ✗", p)
    if len(problems) > 20:
        print(f"   … 另有 {len(problems)-20} 条见 evidence/claims_audit.csv")
    run.save_state("claims", rows=len(out), verified=len(used), self_attested=len(selfatt),
                   blocked=len(blocked), by_section=dict(by_sec))
    ok("evidence/claims_audit.csv")
    return out


def _dossier_skeleton(run, shortlist):
    lines = ["# 范本档案（exemplar dossier）", "",
             "> 用途：从**你已抓到全文的**同领域/目标刊论文里提取可复用的写作决定，不是抄句子。",
             "> 规则：官方作者指南与从已发表论文观察到的体例分开记（两者常被混为\"期刊要求\"）；",
             "> 摘要不能当作\"研究过它的图和全文结构\"的证据；引文数、体例结论必须挂 uid。", ""]
    for s in DOSSIER_SECTIONS:
        lines.append(s)
        if s.startswith("## Official"):
            lines += ["", "| 项 | 官方规定 | 出处（期刊作者指南链接/检索日期） |", "|---|---|---|", "| | | |", ""]
        elif s.startswith("## Corpus"):
            lines += ["", "| uid | 论文 | 期刊/年 | 角色 | 读了哪些部分 | 为什么值得学 |", "|---|---|---|---|---|---|"]
            for r in shortlist:
                lines.append(f"| {r['uid']} | {(r.get('title') or '')[:60]} | {r.get('venue') or ''}/{r.get('year') or ''} "
                             f"| {r.get('_ex_role','')} |  |  |")
            lines.append("")
        elif s.startswith("## Observed"):
            lines += ["", "| uid | 维度(动机/缺口/贡献表述/方法解释/结果解读/讨论闭合) | 观察到的写法 | 为什么有效 |",
                      "|---|---|---|---|", "| | | | |", ""]
        elif s.startswith("## Move"):
            lines += ["", "| uid | 段落 | move | 证据类型 | 段首功能 | 段尾功能 |", "|---|---:|---|---|---|---|", "| | | | | | |", ""]
        elif s.startswith("## Skeletons"):
            lines += ["", "| 功能 | 句型骨架（抽象结构，不照抄措辞） | 来自哪篇 uid | 用在你哪一节 |", "|---|---|---|---|", "| | | | |", ""]
        elif s.startswith("## Anti-patterns"):
            lines += ["", "| 反例写法 | 为什么会失败 | 改成什么 |", "|---|---|---|", "| | | |", ""]
        else:
            lines += ["", "| 你的章节 | 学到的写法 | 需要的操作(REWRITE/SPLIT/MERGE/DELETE/MOVE/ADD/KEEP) |",
                      "|---|---|---|", "| | | |", "", "> KEEP 应当很少，ADD 是次要手段；"
                      "闭卷重写：把事实/论断/数字摘进笔记后**合上原文**，照蓝图重写，最后再开原文核对没漏。", ""]
    return "\n".join(lines) + "\n"


def exemplar_pick(run, venue=None, n_field=None, n_venue=None):
    ev = run.profile["evidence"]
    cfg = ev.get("exemplars", {})
    n_field = int(n_field or cfg.get("same_field", 3))
    n_venue = int(n_venue or cfg.get("target_venue", 3))
    venue = venue or ev.get("target_venue")
    by_uid, recs = _selected(run)
    pdf = _pdf_status(run)
    have_text = [r for r in recs if not r.get("is_retracted")
                 and (pdf.get(r["uid"]) or {}).get("status") in ("downloaded", "cached")]
    if len(have_text) < n_field:
        print(f"  手里有全文的只有 {len(have_text)} 篇，凑不出 {n_field} 篇范本 —— 先跑 `lit.py pdfs fetch --run ...`")
    vt = norm_title(venue or "")
    vpool = [r for r in have_text if norm_title(r.get("venue")) == vt] if venue else []
    vpick = sorted(vpool, key=lambda r: (-float(r.get("score") or 0)))[:n_venue]
    taken = {r["uid"] for r in vpick}
    fpool = [r for r in sorted(have_text, key=lambda r: -float(r.get("score") or 0))
             if r["uid"] not in taken]
    fpick = []
    for r in fpool:
        if len(fpick) >= n_field:
            break
        if any(norm_title((x.get("enrich") or {}).get("subtopic_label", "")) ==
               norm_title((r.get("enrich") or {}).get("subtopic_label", "")) for x in fpick):
            continue
        fpick.append(r)
    for r in fpool:
        if len(fpick) >= n_field:
            break
        if r not in fpick:
            fpick.append(r)
    for r in vpick:
        r["_ex_role"] = "target-venue"
    for r in fpick:
        r["_ex_role"] = "same-field"
    short = vpick + fpick
    if venue and len(vpick) < n_venue:
        print(f"  WARN 目标刊 `{venue}` 只有 {len(vpick)} 篇且已有全文（要求 {n_venue}）——"
              f"样本不足时不得声称\"符合该刊体例\"，只能说\"参考了 {len(vpick)} 篇\"")
    if not venue:
        print("  未指定目标刊：本次不做\"符合目标刊格式\"的任何声称。agent 自选的对照刊不能当 target。")
    os.makedirs(run.path("evidence"), exist_ok=True)
    with open(run.path("evidence", "exemplars.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["uid", "role", "venue", "year", "tier", "has_fulltext", "referenced_works_count", "title"])
        for r in short:
            w.writerow([r["uid"], r["_ex_role"], r.get("venue"), r.get("year"),
                        (r.get("venue_resolved") or {}).get("tier", ""), "yes",
                        r.get("referenced_works_count", ""), (r.get("title") or "")[:90]])
    with open(run.path("evidence", "exemplar_dossier.md"), "w", encoding="utf-8") as fh:
        fh.write(_dossier_skeleton(run, short))
    counts = [int(r.get("referenced_works_count") or 0) for r in short if r.get("referenced_works_count")]
    if counts:
        mean = sum(counts) / len(counts)
        print(f"  这批范本的参考文献数：均值 {mean:.0f}（n={len(counts)}，来自 OpenAlex referenced_works_count）"
              f" → 目标引文数下限建议 {int(math.ceil(mean))}；这是观察值，不是期刊规定")
    else:
        print("  范本缺少 OpenAlex referenced_works_count，无法给出目标引文数的观察值（不要凭印象编一个数）")
    run.save_state("exemplars", picked=len(short), target_venue=venue or "", with_fulltext=len(have_text),
                   venue_side=min(len(vpick), n_venue))
    ok(f"evidence/exemplar_dossier.md 骨架已生成（{len(short)} 篇范本，两集合零重复）")
    return short


def exemplar_check(run):
    p = run.path("evidence", "exemplar_dossier.md")
    if not os.path.isfile(p):
        die("缺 evidence/exemplar_dossier.md — 先跑 evidence exemplar-pick")
    txt = open(p, encoding="utf-8").read()
    missing = [s for s in DOSSIER_SECTIONS if s not in txt]
    if missing:
        die(f"档案缺章节：{missing}")
    with open(run.path("evidence", "exemplars.csv"), encoding="utf-8-sig") as fh:
        allow = {r["uid"] for r in csv.DictReader(fh)}
    # Corpus 表是骨架自己预填的，只能数正文各表里的引用，否则永远"全部引用"
    after_corpus = txt.split("## Official author guidance", 1)[-1]
    cited = {u for u in allow if u in after_corpus}
    body = txt.split("## Official author guidance", 1)[-1].split("## Observed style", 1)[0]
    blank_rows = len(re.findall(r"^\|\s*\|\s*\|\s*\|", txt, re.M))
    print(f"  范本 {len(allow)} 篇，档案里真正引用了 {len(cited)} 篇 uid")
    if not body.strip() or body.count("|") < 12:
        print("  WARN 官方作者指南一节还是空的 —— 没读官方指南就别说\"按期刊要求\"")
    problems = []
    if len(cited) < max(2, int(len(allow) * 0.6)):
        problems.append(f"至少 60% 的范本要在档案里出现（现在 {len(cited)}/{len(allow)}）")
    if blank_rows:
        problems.append(f"仍有 {blank_rows} 行空表格未填")
    for c in problems:
        print("   ✗", c)
    run.save_state("exemplars_check", cited=len(cited), total=len(allow), blank_rows=blank_rows,
                   passed=not problems)
    if problems:
        die("范本案档未完成")
    ok("范本案档通过")
