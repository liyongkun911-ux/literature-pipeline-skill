"""Screening: venue whitelist/partition, time window, direction judgement.

Nothing here is silently deleted — every drop carries a reason code, and unresolved
venues go to a `needs_judgement` bucket for the model + user instead of a guess.
"""
import csv
import json
import os
from datetime import date

from .common import (Run, norm_doi, norm_title, ok, read_jsonl, write_jsonl, die)
from .venue_health import probe_venues

REASONS = {
    "R01": "off-topic: 模型判定不属于目标方向",
    "R02": "venue-tier: 期刊权威性 tier 低于 accept_tiers（分级表判为 T4/T5）",
    "R03": "venue-unresolved: 刊名未被分级表收录且 unknown_action=drop",
    "R04": "flagged-venue: 命中排除/预警名单",
    "R05": "out-of-window: 超出时间窗且未获经典豁免",
    "R06": "shadow-version: 已有正式版本，预印本仅作为全文入口保留",
    "R07": "no-identifier: 无 DOI 也无 arXiv 号，无法核验",
    "R08": "user-excluded: 用户在 queries.json 的 exclude 列表中点名剔除",
    "R09": "retracted: OpenAlex 标记 is_retracted，撤稿论文不得进入文献库",
}

ENRICH_FIELDS = ["direction_ok", "relevance", "subtopic_label", "method_label", "role",
                 "venue_hint", "one_line", "flags"]
# uid 是内容哈希：后一轮召回把 DOI 合并进来后 uid 会变。只按 uid 索引会让已经做过的判读
# 静默失效，所以模板里带上身份列，查找时按 uid -> DOI -> arXiv -> 标题 逐级兜底。
ENRICH_ID_COLS = ["doi", "arxiv_id", "title_norm"]
ENRICH_HEADER = ["uid"] + ENRICH_ID_COLS + ENRICH_FIELDS


BUILTIN_TIERS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "journal_tiers.csv")


def _read_tiers(path, table, origin):
    """Later reads win: built-in table first, the school's own directory on top."""
    n = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not (row.get("name") or row.get("issn_l")):
                continue
            tier = (row.get("tier") or "").upper()
            entry = {"tier": tier,
                     "partition": row.get("partition") or "",
                     "if_band": row.get("if_band") or row.get("impact_factor") or "",
                     "domain": row.get("domain") or "",
                     "tier_basis": row.get("tier_basis") or "",
                     "predatory": tier == "T5" or row.get("is_predatory", "") in ("1", "true", "y", "yes", "预警"),
                     "origin": origin}
            if row.get("issn_l"):
                table["by_issn"][row["issn_l"].split(";")[0].strip()] = entry
            if row.get("name"):
                table["by_name"][norm_title(row["name"])] = entry
            for alt in (row.get("alt_names") or "").split("|"):
                if alt.strip():
                    table["by_name"][norm_title(alt)] = entry
            n += 1
    return n


def load_journal_dir(run):
    cfg = run.profile["venue_screen"]
    table = {"by_issn": {}, "by_name": {}, "counts": {}}
    err = None
    if cfg.get("builtin_tiers", True) and os.path.isfile(BUILTIN_TIERS):
        table["counts"]["builtin"] = _read_tiers(BUILTIN_TIERS, table, "builtin")
    path = cfg.get("journal_directory")
    if path:
        path = path if os.path.isabs(path) else os.path.join(run.dir, path)
        if os.path.isfile(path):
            table["counts"]["school"] = _read_tiers(path, table, "school")
        else:
            err = f"journal_directory not found: {path}"
    return table, err


def resolve_venue(run, rec, table):
    cfg = run.profile["venue_screen"]
    venue = (rec.get("venue") or "").strip()
    issn = (rec.get("venue_issn_l") or "").strip()
    is_preprint = (rec.get("type") == "preprint") or venue.lower().startswith("arxiv")
    if is_preprint and cfg.get("preprint_venues_allowed", True):
        return {"kind": "preprint", "tier": "PRE", "partition": "", "if_band": "",
                "predatory": False, "venue": venue or "arXiv", "resolved": "preprint"}
    hit = table["by_issn"].get(issn) or table["by_name"].get(norm_title(venue))
    if hit:
        return {"kind": "journal", "tier": hit["tier"], "partition": hit["partition"],
                "if_band": hit.get("if_band", ""), "domain": hit.get("domain", ""),
                "predatory": hit["predatory"], "venue": venue, "resolved": hit["origin"]}
    for rej in cfg.get("reject_venues") or []:
        if norm_title(rej) and norm_title(rej) == norm_title(venue):
            return {"kind": "journal", "tier": "T5", "partition": "", "if_band": "",
                    "predatory": True, "venue": venue, "resolved": "reject_list"}
    # Absent from every table = unknown, not guilty: keep it, score it low, flag for bulk triage.
    return {"kind": "journal", "tier": "", "partition": "", "if_band": "",
            "predatory": False, "venue": venue, "resolved": "unmatched"}


def load_enrichment(run, required=True):
    path = run.path("enrichment.csv")
    if not os.path.isfile(path):
        if required:
            die("enrichment.csv missing — run `lit.py enrich-template`, fill it (model judgement), then re-run screen")
        return {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_uid, by_ident = {}, {}
    for r in rows:
        u = (r.get("uid") or "").strip()
        if u:
            by_uid[u] = r
        for k in ENRICH_ID_COLS:
            v = (r.get(k) or "").strip()
            if v:
                by_ident.setdefault(f"{k}:{v}", r)
    return {"by_uid": by_uid, "by_ident": by_ident, "__exclude__": []}


def enrich_for(idx, rec):
    """Return (judgement_row, matched_by). matched_by != 'uid' means the record's uid drifted
    after a later harvest merged in an identifier, and the judgement was rescued by key."""
    r = idx["by_uid"].get(rec["uid"])
    if r is not None:
        return r, "uid"
    cand = {"doi": norm_doi(rec.get("doi")),
            "arxiv_id": str(rec.get("arxiv_id") or "").strip().lower(),
            "title_norm": rec.get("title_norm") or ""}
    for k in ENRICH_ID_COLS:
        v = cand.get(k) or ""
        hit = idx["by_ident"].get(f"{k}:{v}") if v else None
        if hit is not None:
            return hit, k
    return {}, ""


def emit_template(run, limit=400):
    recs = read_jsonl(run.path("02_deduped.jsonl"))
    if not recs:
        die("02_deduped.jsonl missing — run dedupe first")
    path = run.path("enrichment.csv")
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        ok(f"enrichment.csv 已存在（{len(rows)} 行），不覆盖；需要重建请删除该文件")
        return path, len(rows)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(ENRICH_HEADER)
        for r in recs[:limit]:
            w.writerow([r["uid"], norm_doi(r.get("doi")),
                        str(r.get("arxiv_id") or "").strip().lower(),
                        r.get("title_norm") or ""] + [""] * len(ENRICH_FIELDS))
    with open(run.path("enrichment_context.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["uid", "title", "year", "venue", "abstract_head"])
        for r in recs[:limit]:
            w.writerow([r["uid"], r.get("title", ""), r.get("year", ""), r.get("venue", ""),
                        (r.get("abstract") or "")[:600].replace("\n", " ")])
    ok(f"enrichment.csv 骨架 {min(limit,len(recs))} 行 + enrichment_context.csv（判方向时看这个，别凭标题猜）")
    return path, min(limit, len(recs))


def screen(run):
    prof = run.profile
    cfg = prof["venue_screen"]
    recs = read_jsonl(run.path("02_deduped.jsonl"))
    if not recs:
        die("02_deduped.jsonl missing — run dedupe first")
    enr = load_enrichment(run)
    table, dir_err = load_journal_dir(run)
    health = {}
    if (cfg.get("health") or {}).get("enabled", True):
        health = probe_venues(run, recs)
    keep, needs, drops = [], [], []
    min_year = date.today().year - int(prof["window"]["years"])
    exclude = {norm_doi(x) or norm_title(x) for x in (enr.get("__exclude__") or [])}
    if os.path.isfile(run.path("queries.json")):
        plan = json.load(open(run.path("queries.json"), encoding="utf-8"))
        exclude |= {norm_doi(x) or norm_title(x) for x in (plan.get("exclude") or [])}

    def drop(r, code, detail=""):
        drops.append({"uid": r["uid"], "reason": code, "detail": f"{REASONS[code].split(': ')[0]} {detail}".strip(),
                      "title": (r.get("title") or "")[:90], "venue": r.get("venue") or "", "year": r.get("year")})

    rescued = {}
    for r in recs:
        e, how = enrich_for(enr, r)
        if how and how != "uid":
            rescued[how] = rescued.get(how, 0) + 1
        key = norm_doi(r.get("doi")) or r.get("title_norm")
        if key and key in exclude:
            drop(r, "R08"); continue
        if not r.get("doi") and not r.get("arxiv_id"):
            drop(r, "R07", r.get("url", "")[:60]); continue
        if r.get("is_retracted"):
            drop(r, "R09", (r.get("doi") or r.get("arxiv_id") or "")[:40]); continue
        for k, v in e.items():
            if k not in ("uid",):
                r.setdefault("enrich", {})[k] = v
        r["direction_ok"] = (e.get("direction_ok") or "").strip().lower()
        if r["direction_ok"] in ("no", "n", "off", "0"):
            drop(r, "R01", e.get("one_line", "")[:60]); continue
        rv = resolve_venue(run, r, table)
        # 期刊健康度：非期刊载体封顶 T4（判据1自动化）；风险标记随行带入打分
        hk = (r.get("venue_issn_l") or "").strip() or norm_title(rv.get("venue") or "")
        hh = health.get(hk) or {}
        if hh.get("flags"):
            r["venue_health"] = {k: hh[k] for k in ("type", "listed_in", "retraction_ratio", "flags")
                                 if k in hh}
            if "non_journal_carrier" in hh["flags"] and rv.get("tier") in ("T1", "T2", "T3"):
                rv["tier"] = "T4"
                rv["tier_capped_by"] = "non_journal_carrier"
        r["venue_resolved"] = rv
        if rv["predatory"]:
            drop(r, "R04", rv["venue"][:60]); continue
        if rv["kind"] == "journal":
            if rv["resolved"] == "unmatched":
                if cfg.get("unknown_action", "keep_and_flag") == "drop":
                    drop(r, "R03", rv["venue"][:60]); continue
                # keep_and_flag 必须真的留在打分池里：needs 是复核视图，不是排除。
                # 之前只 append 到 needs，等于把"期刊表没收录"变成了隐性淘汰。
                r["venue_flag"] = "unresolved"
                keep.append(r); needs.append(r)
                continue
            accept = [str(x).upper() for x in cfg.get("accept_tiers", ["T1", "T2", "T3"])]
            if rv["tier"] and rv["tier"] not in accept:
                if cfg.get("known_low_tier_action", "drop") == "drop":
                    drop(r, "R02", f"{rv['venue'][:38]}={rv['tier']}"); continue
                r["venue_flag"] = "low-tier"
                keep.append(r); needs.append(r); continue
        y = r.get("year")
        classic_floor = int(prof["window"].get("classic_exempt_citations_gte", 1500))
        if y and int(y) < min_year and not prof["window"].get("hard_cutoff"):
            if int(r.get("cited_by_count") or 0) >= classic_floor or r.get("must_keep"):
                r["classic"] = True
            else:
                drop(r, "R05", str(y)); continue
        keep.append(r)

    # A preprint only earns its own slot when the journal version did not survive screening.
    kept_uids = {r["uid"] for r in keep}
    survivors = []
    for r in keep:
        canon = r.get("version_of")
        if canon and canon in kept_uids and not r.get("must_keep"):
            drop(r, "R06", f"canonical={canon}")
        else:
            survivors.append(r)
    keep = survivors
    write_jsonl(run.path("03_screened.jsonl"), keep)
    write_jsonl(run.path("03_needs_judgement.jsonl"), needs)
    with open(run.path("03_drop_log.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["uid", "reason", "detail", "title", "venue", "year"])
        w.writeheader()
        w.writerows(drops)
    by_code = {}
    for d in drops:
        by_code[d["reason"]] = by_code.get(d["reason"], 0) + 1
    run.save_state("screen", kept=len(keep), needs_judgement=len(needs), dropped=len(drops), by_reason=by_code)
    if rescued:
        print("  判读兜底沿用 %d 条（uid 因后续合并而变化，按 %s 找回）"
              % (sum(rescued.values()), "/".join(f"{k}×{v}" for k, v in sorted(rescued.items()))))
    blank = sum(1 for r in recs if not enrich_for(enr, r)[0])
    if blank:
        print(f"  WARN {blank} 条没有任何判读记录（direction/相关性全空）—— 它们只拿词汇匹配分，"
              f"清单里若出现 score_uncertainty 就是这一批")
    print("  kept=%d（其中期刊待判定 %d，已留在打分池内）dropped=%d" % (len(keep), len(needs), len(drops)))
    print("  drops by reason: " + (", ".join(f"{k}:{v}({REASONS[k].split(':')[0]})" for k, v in sorted(by_code.items())) or "none"))
    if dir_err:
        print(f"  WARN {dir_err} — 期刊未解析的条目全部进入 needs_judgement")
    if needs:
        agg = {}
        for r in needs:
            v = (r.get("venue") or "(no venue name)")
            agg.setdefault(v, [0, r.get("venue_issn_l") or ""])[0] += 1
        with open(run.path("03_unresolved_venues.csv"), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["venue", "issn_l", "hits_in_needs_judgement"])
            for v, (c, issn) in sorted(agg.items(), key=lambda x: -x[1][0]):
                w.writerow([v, issn, c])
        print(f"  {len(needs)} 条待判定，仅涉及 {len(agg)} 个不同刊名 -> 汇总在 03_unresolved_venues.csv")
        print("  闭环：按刊名批量判定层次 -> 追加进分级表 -> 重跑 screen（比逐条判定便宜得多）")
    counts = table.get("counts") or {}
    print("  分级表：内置 %d 条 · 校内目录 %d 条 · 可匹配刊名 %d 个 · 认可层次 %s" % (
        counts.get("builtin", 0), counts.get("school", 0), len(table["by_name"]),
        cfg.get("accept_tiers")))
    if not table["by_name"]:
        print("  WARN 无任何分级数据：venue 维度全部按未知计分")
    ok("03_screened.jsonl written")
    return keep, needs, drops
