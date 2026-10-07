"""Six-dimension weighted scoring with per-dimension breakdown, quota + diversity selection.

Design rule: the model supplies only judgement fields (relevance/direction/role/labels);
arithmetic, normalisation and selection happen here so the ranking is reproducible.
"""
import csv
import json
import math
import os
from collections import Counter, defaultdict
from datetime import date

from .common import (Run, ok, pct_rank, read_jsonl, tokens, write_jsonl, die, norm_title)

TIER_SCORE = {"T1": 1.00, "T2": 0.78, "T3": 0.52, "T4": 0.26, "T5": 0.00, "PRE": 0.48, "": 0.40}
# if_band 是区间而非精确影响因子，只作微调；技能不臆造 IF 数值
REVIEW_WORDS = ("review", "survey", "overview", "综述", "progress in", "advances in", "a review", "systematic review")


def lexical_relevance(rec, query_terms):
    """Explainable stand-in for a ranker: coverage of query terms, title weighted 2x."""
    if not query_terms:
        return 0.0
    tt = set(tokens(rec.get("title") or "")) | {w for w in norm_title(rec.get("title") or "").split()}
    ab = set(tokens(((rec.get("abstract") or "")[:2500]).lower()))
    hay = tt | ab
    hit_title = sum(1 for t in query_terms if t in tt)
    hit_all = sum(1 for t in query_terms if t in hay)
    n = len(query_terms)
    return min(1.0, (0.6 * hit_title / n) + (0.4 * hit_all / n)) if n else 0.0


def dim_relevance(run, recs, qterms):
    lx = {r["uid"]: lexical_relevance(r, qterms) for r in recs}
    vals = []
    for r in recs:
        e = r.get("enrich") or {}
        sem = e.get("relevance")
        try:
            sem = float(sem)
            if sem > 1:
                sem = sem / 100.0
        except (TypeError, ValueError):
            sem = None
        w = run.profile["scoring"]["relevance"]
        if sem is None:
            v = lx[r["uid"]]
            r["relevance_basis"] = "lexical-only"
        else:
            v = float(w.get("lexical_weight", .45)) * lx[r["uid"]] + float(w.get("semantic_weight", .55)) * sem
            r["relevance_basis"] = "lexical+semantic"
        r["relevance_lexical"] = round(lx[r["uid"]], 3)
        r["relevance_semantic"] = sem
        vals.append(v)
    return _norm(run, recs, vals, "relevance")


UNKNOWN_SCORE = {"zero": 0.0, "neutral": 0.40, "t4": 0.26}


def dim_venue(run, recs):
    """期刊层次分。载体现名查不到时默认**不作数**（0 分）而不是给 0.40 中性分——
    否则'来源没告诉我刊名'会比一张诚实标为 T4 的量刊还占便宜，等于奖励元数据缺失。
    健康度风险（撤稿密集/未进任何索引）按配置从层次分里显式扣，扣多少进 breakdown。"""
    pol = str(run.profile["venue_screen"].get("unknown_score", "zero")).lower()
    unk = UNKNOWN_SCORE.get(pol, 0.0)
    penalties = ((run.profile["venue_screen"].get("health") or {}).get("penalties")
                 or {"retraction_heavy": 0.20, "not_indexed": 0.10})
    band_adj = {"15+": 0.10, "10+": 0.07, "8+": 0.05, "6+": 0.03, "5+": 0.03, "4+": 0.02, "3+": 0.01}
    vals = []
    for r in recs:
        rv = r.get("venue_resolved") or {}
        name = (rv.get("venue") or "").strip()
        tier = str(rv.get("tier", "") or ("PRE" if rv.get("kind") == "preprint" else "")).upper()
        if not tier:
            v = unk
            r["venue_flag"] = "no-venue" if not name else "unresolved"
            r["venue_tier"] = "未标明载体" if not name else "未收录"
        else:
            v = TIER_SCORE.get(tier, unk) + band_adj.get(str(rv.get("if_band", "")), 0.0)
            r["venue_tier"] = tier
        if r.get("classic"):
            v = max(v, 0.9)
        flags = ((r.get("venue_health") or {}).get("flags")) or []
        pen = sum(float(penalties.get(f, 0.0)) for f in flags)
        if pen and v > 0:
            v = max(0.0, v - pen)
            r["venue_risk_penalty"] = round(pen, 2)
            r["venue_risk_reasons"] = [f for f in flags if f in penalties]
        vals.append(min(1.0, v))
    return _norm(run, recs, vals, "venue", raw=True)


def dim_recency(run, recs):
    """Time-decay bonus, as an explicit step gradient so the reason a paper lost points is
    readable ("3-4年档"), with the exponential half-life kept as the fallback curve."""
    cfg = run.profile["scoring"]["recency"]
    hl = float(cfg.get("half_life_years", 2.5))
    steps = sorted(cfg.get("gradient") or [], key=lambda s: float(s["max_age"]))
    this = date.today().year
    vals = []
    for r in recs:
        y = r.get("year")
        if not y:
            r["recency_step"] = "无年份"
            vals.append(float(cfg.get("no_year_score", 0.3)))
            continue
        age = max(0, this - int(y))
        if steps:
            hit = next((s for s in steps if age <= float(s["max_age"])), steps[-1])
            v = float(hit["score"])
            r["recency_step"] = f"≤{hit['max_age']}年"
        else:
            v = 0.5 ** (age / hl)
            r["recency_step"] = f"半衰期{hl:g}年"
        if r.get("classic"):
            v = min(v, float(cfg.get("classic_cap", 0.25)))
            r["recency_step"] += "·经典封顶"
        vals.append(v)
    return _norm(run, recs, vals, "recency", raw=True)


def dim_impact(run, recs):
    """Citations normalised within (field, counting source): a young CS paper must not be
    beaten by an old med paper, and an OpenAlex count must not share an axis with a Crossref
    one — the two sources cover different windows and date back different amounts."""
    groups = defaultdict(list)
    for r in recs:
        topic = (r.get("subfields") or r.get("domains") or ["unknown"])[0] or "unknown"
        grp = (topic, r.get("cited_by_source") or "无计数")
        groups[grp].append(r)
    out = [0.0] * len(recs)
    index = {r["uid"]: i for i, r in enumerate(recs)}
    for grp, members in groups.items():
        vals = [float(m.get("cited_by_count") or 0) for m in members]
        svals = sorted(vals)
        all_zero = not any(vals)
        for m in members:
            c = float(m.get("cited_by_count") or 0)
            if all_zero:
                # 该组根本没有引用数（纯 arXiv 或未取到）：无信号就是无信号，
                # 不能让"全 0"在百分位里等价于"全满"。
                p = 0.0
                m["impact_basis"] = "no-citation-signal"
            else:
                p = pct_rank(c, svals)
                if len(members) < 8:
                    p = min(1.0, math.log10(1 + c) / 4.0)
                m["impact_basis"] = "percentile" if len(members) >= 8 else "log-fallback"
            m["impact_group"] = f"{grp[0]}|{grp[1]}"
            out[index[m["uid"]]] = p
    return _norm(run, recs, out, "impact")


def dim_accessibility(run, recs):
    vals = []
    for r in recs:
        v = 0.0
        if r.get("pdf_url"):
            v += 0.5
        elif r.get("oa_url"):
            v += 0.35
        if r.get("oa_status") in ("OA", "GOLD", "GREEN", "HYBRID"):
            v += 0.2
        if r.get("abstract"):
            v += 0.15
        if r.get("doi"):
            v += 0.1
        if len(r.get("authors") or []) >= 2:
            v += 0.1
        if r.get("language") == "zh":
            v -= 0.1
        vals.append(max(0.0, min(1.0, v)))
    return _norm(run, recs, vals, "accessibility", raw=True)


def dim_authority(run, recs):
    """Grounded proxy for 'who vouches for this': multi-source hits, snowball discovery,
    author persistence in the pool, explicit user nomination. Not a prestige claim."""
    src = Counter()
    for r in recs:
        for s in r.get("sources") or []:
            src[s] += 1
    author_freq = Counter()
    for r in recs:
        for a in {x.lower() for x in (r.get("authors") or [])}:
            author_freq[a] += 1
    vals = []
    for r in recs:
        v = 0.0
        n_src = len(r.get("sources") or [])
        v += 0.35 * min(1.0, (n_src - 1) / 2) if n_src > 1 else 0.0
        if "snowball" in (r.get("discovered_via") or ""):
            v += 0.3
        top = max([author_freq.get(a.lower(), 0) for a in (r.get("authors") or [])] or [0])
        v += 0.25 * min(1.0, max(0, top - 1) / 3)
        if r.get("must_keep"):
            v += 0.4
        if r.get("cited_by_count", 0) and r["cited_by_count"] > 500:
            v += 0.1
        vals.append(min(1.0, v))
    return _norm(run, recs, vals, "authority", raw=True)


def _norm(run, recs, vals, key, raw=False):
    mode = run.profile["scoring"]["relevance"].get("normalize", "percentile_within_run")
    if key == "relevance" and mode == "percentile_within_run":
        s = sorted(vals)
        vals = [pct_rank(v, s) for v in vals]
    for r, v in zip(recs, vals):
        r[f"dim_{key}"] = round(v, 4)
    return vals


def score(run):
    prof = run.profile
    recs = read_jsonl(run.path("03_screened.jsonl"))
    if not recs:
        die("03_screened.jsonl empty — run screen first")
    if os.path.isfile(run.path("queries.json")):
        plan = json.load(open(run.path("queries.json"), encoding="utf-8"))
    else:
        plan = {}
    qterms = sorted({t for b in plan.get("blocks", [])
                     for lst in (b.get("en"), b.get("zh"))
                     for t in tokens(" ".join(lst or []))})
    dim_relevance(run, recs, qterms)
    dim_venue(run, recs)
    dim_recency(run, recs)
    dim_impact(run, recs)
    dim_accessibility(run, recs)
    dim_authority(run, recs)
    base_w = float(prof["scoring"]["base"]["weight_pct"])
    bonus_cap = float(prof["scoring"]["bonus"]["cap"])
    subcaps = prof["scoring"]["bonus"]["subcaps"]
    if prof["scoring"]["bonus"].get("apply_bonus_multiplier"):
        mult = float(prof["scoring"]["bonus"].get("bonus_multiplier", 0.3))
        bonus_cap *= mult
        subcaps = {k: v * mult for k, v in subcaps.items()}
    capped = 0
    for r in recs:
        base_pts = base_w * float(r.get("dim_relevance", 0.0))
        parts = {k: float(subcaps.get(k, 0)) * float(r.get(f"dim_{k}", 0.0)) for k in subcaps}
        raw_bonus = sum(parts.values())
        bonus_pts = min(raw_bonus, bonus_cap)
        if raw_bonus > bonus_cap:
            r["bonus_capped"] = round(raw_bonus - bonus_cap, 2)
            capped += 1
        total = base_pts + bonus_pts
        if r.get("relevance_basis") == "lexical-only":
            r["score_uncertainty"] = "语义相关性未提供，基础分仅词汇匹配，排序稳定性较低"
        if r.get("classic"):
            total *= 0.97
        r["score"] = round(total, 2)
        r["score_breakdown"] = {"基础分": round(base_pts, 2),
                                **{k: round(v, 2) for k, v in parts.items()}}
        r["bonus_raw"] = round(raw_bonus, 2)
        r["bonus_cap"] = bonus_cap
    if capped:
        print(f"  {capped} 篇加分超过封顶 {bonus_cap:g} 分，溢出部分已丢弃（这正是加分制想要的抑制效果）")

    tie = prof["scoring"].get("tie_break", ["impact", "recency"])
    # A tie-break name with no dim_* column silently contributes 0 for everyone.
    known = [t for t in tie if any(f"dim_{t}" in r for r in recs)]
    dropped = [t for t in tie if t not in known]
    if dropped:
        print(f"  WARN tie_break 中这些名字没有对应分项，已忽略：{dropped}（应为 venue/impact/recency/accessibility/authority/relevance 之一）")
    recs.sort(key=lambda r: (-r["score"], *[-float(r.get(f"dim_{t}", 0)) for t in known]))
    for i, r in enumerate(recs, 1):
        r["rank_all"] = i
        r.setdefault("role", (r.get("enrich") or {}).get("role") or _guess_role(r))
    write_jsonl(run.path("04_ranked.jsonl"), recs)
    ok(f"04_ranked.jsonl = {len(recs)} 篇打分（基础 0-{base_w:g} + 加分 0-{bonus_cap:g}）")
    return recs


def _guess_role(r):
    t = (r.get("title") or "").lower()
    if any(w in t for w in REVIEW_WORDS):
        return "survey"
    if r.get("type") == "preprint":
        return "method"
    return "application"


ROLE_CN = {"survey": "综述", "method": "方法", "benchmark": "基准", "application": "应用", "critique": "争议"}


def select(run, recs=None):
    prof = run.profile
    recs = recs or read_jsonl(run.path("04_ranked.jsonl"))
    sel = prof["selection"]
    quota, div = sel["role_quota"], sel["diversity"]
    chosen, pool = [], list(recs)
    firsts, venues, subs = Counter(), Counter(), set()

    def take(r):
        chosen.append(r)
        firsts[(r.get("authors") or ["?"])[0].lower()] += 1
        venues[norm_title(r.get("venue") or "")] += 1
        subs.add((r.get("enrich") or {}).get("subtopic_label") or r.get("role") or "n/a")

    def allowed(r, role, phase):
        c = sum(1 for x in chosen if x.get("role") == role)
        lim = quota.get(role, {}).get("max", 99)
        if c >= lim:
            return False
        if phase == "fill":
            f = (r.get("authors") or ["?"])[0].lower()
            if firsts[f] >= int(div.get("max_same_first_author", 99)):
                return False
            if venues[norm_title(r.get("venue") or "")] >= int(div.get("max_same_venue", 99)):
                return False
        return True

    mode = sel.get("mode", "related_work")
    # 规模分档：scout 只摸方向，不该交出一整篇综述的量
    target = 5 if mode == "scout" else int(sel["candidates"])
    floor = float(sel.get("min_score", 0))
    if mode == "systematic" and not os.path.isfile(run.path("prisma_protocol.md")):
        die("systematic 模式要先写 runs/<id>/prisma_protocol.md："
            "研究问题(PEOS)、拟检索的库、每条检索式全文、纳入/排除标准、筛选日志字段。"
            "没有协议就不做『系统性』声称——这是铁律5。")
    must = [r for r in pool if r.get("must_keep")]
    for r in must[:target]:
        take(r)

    for role, q in quota.items():
        want = int(q.get("min", 0))
        have = sum(1 for x in chosen if x.get("role") == role)
        for r in pool:
            if have >= want or len(chosen) >= target:
                break
            # 配额下限不得越过质量下限，否则"综述不足 2 篇"会变成塞两篇低分综述的理由。
            if float(r.get("score") or 0) < floor:
                continue
            if r in chosen or r.get("role") != role or not allowed(r, role, "quota"):
                continue
            take(r)
            have += 1

    for r in pool:
        if len(chosen) >= target:
            break
        if float(r.get("score") or 0) < floor:
            continue
        if r in chosen or not allowed(r, r.get("role"), "fill"):
            continue
        take(r)
    above = sum(1 for r in pool if float(r.get("score") or 0) >= floor)
    if len(chosen) < target and above <= target:
        print(f"  按质量下限 min_score={floor:g} 只有 {above} 篇够格，本轮就交 {len(chosen)} 篇。"
              f"宁少不凑数：要更多候选请回去放宽 queries.json 重跑 harvest（铁律4），不要降低 min_score。")

    shortfalls = {}
    for role, q in quota.items():
        have = sum(1 for x in chosen if x.get("role") == role)
        if have < int(q.get("min", 0)):
            shortfalls[role] = {"min": int(q["min"]), "have": have}

    reserve = [r for r in pool if r not in chosen][: int(sel.get("reserve", 8))]
    for i, r in enumerate(chosen, 1):
        r["candidate_no"] = i
    render(run, chosen, reserve, shortfalls, subs)
    run.save_state("select", candidates=len(chosen), reserve=len(reserve), distinct_subtopics=len(subs),
                   quota_shortfall=shortfalls)
    ok(f"05_candidates.csv = {len(chosen)} 篇候选 + {len(reserve)} 备选（覆盖子方向 {len(subs)} 个）")
    if shortfalls:
        print(f"  WARN 角色配额未满足: {json.dumps(shortfalls, ensure_ascii=False)} — 请放宽检索式或调低 min，不要塞低分文献")
    return chosen, reserve


def band(run, s):
    b = run.profile["tags"]["priority"]["bands"]
    for k in sorted(b, key=lambda x: -b[x]):
        if s >= b[k]:
            return k
    return "P3"


def tags_for(run, r):
    tg = run.profile["tags"]
    prio = r.get("priority") or band(run, r.get("score") or 0)
    out = [f"{tg['priority']['prefix']}/{prio}",
           f"{tg['status']['prefix']}/{tg['status']['default']}"]
    role = ROLE_CN.get(r.get("role"), r.get("role"))
    if role:
        out.append(f"{tg['role']['prefix']}/{role}")
    e = r.get("enrich") or {}
    if e.get("subtopic_label"):
        out.append(f"{tg['topic']['prefix']}/{e['subtopic_label']}")
    if e.get("method_label"):
        out.append(f"{tg['method']['prefix']}/{e['method_label']}")
    return [t for t in out if t.strip()]


COLS = ["candidate_no", "verdict", "uid", "score", "priority", "role", "subtopic", "method",
        "year", "first_author", "n_authors", "venue", "partition", "cited", "oa", "title", "doi",
        "arxiv", "url", "pdf_url", "why", "tags", "notes"]


def render(run, chosen, reserve, shortfalls, subs):
    sel = run.profile["selection"]
    with open(run.path("05_candidates.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for r in chosen + reserve:
            e = r.get("enrich") or {}
            w.writerow([r.get("candidate_no", ""), "keep", r["uid"], r.get("score"),
                        band(run, r.get("score") or 0), ROLE_CN.get(r.get("role"), r.get("role")),
                        e.get("subtopic_label", ""), e.get("method_label", ""), r.get("year"),
                        (r.get("authors") or [""])[0], len(r.get("authors") or []),
                        r.get("venue") or "", (r.get("venue_resolved") or {}).get("tier", ""),
                        r.get("cited_by_count"), r.get("oa_status") or ("OA" if r.get("pdf_url") else ""),
                        r.get("title"), r.get("doi"), r.get("arxiv_id"), r.get("url"),
                        r.get("pdf_url") or r.get("oa_url") or "",
                        e.get("one_line", ""), ";".join(tags_for(run, r)), ""])
    prof = run.profile
    sc = prof["scoring"]
    rule = (f"计分：综合分 = 基础分(相关性 0-{sc['base']['weight_pct']:g}) + 成果加分"
            f"(0-{sc['bonus']['cap']:g} 封顶)；子项上限 {sc['bonus']['subcaps']}")
    lines = [f"# {prof.get('topic') or run.run_id} — 候选清单（{len(chosen)} 篇）", "",
             f"{rule} ｜ 时间窗：近 {prof['window']['years']} 年 ｜ 认可期刊层次：{prof['venue_screen'].get('accept_tiers')}",
             f"打分池 {len(read_jsonl(run.path('04_ranked.jsonl')))} 篇 → 配额+多样性选出 {len(chosen)} 篇，覆盖子方向 {len(subs)} 个。",
             "",
             "**请在 `05_candidates.csv` 的 `verdict` 列改为 keep / drop / maybe 后回复确认；`priority`/`subtopic`/`method`/`notes` 同样可改。**",
             "",
             "| # | 分 | P | 角色 | 年 | 引用 | 期刊/层次 | 标题 | 为什么入选 |", "|---|---|---|---|---|---|---|---|---|"]
    for r in chosen:
        e = r.get("enrich") or {}
        rv = r.get("venue_resolved") or {}
        lines.append(f"| {r.get('candidate_no')} | {r.get('score')} | {band(run, r.get('score') or 0)} | "
                     f"{ROLE_CN.get(r.get('role'), r.get('role'))} | {r.get('year')} | {r.get('cited_by_count')} | "
                     f"{(rv.get('venue') or '')[:32]}{' ' + rv['tier'] if rv.get('tier') else ' 未收录'} | "
                     f"{(r.get('title') or '')[:72]} | {(e.get('one_line') or r.get('relevance_basis') or '')[:48]} |")
    if shortfalls:
        lines += ["", f"> ⚠️ 角色配额缺口：{json.dumps(shortfalls, ensure_ascii=False)}"]
    unk = [r for r in chosen if r.get("venue_flag")]
    if unk:
        lines += ["", f"> ℹ {len(unk)} 条**载体未确定**（来源没给刊名，或分级表未收录），"
                  f"本次期刊层次分按 `{prof['venue_screen'].get('unknown_score', 'zero')}` 处理、未计入加分："
                  + "、".join(f"#{r.get('candidate_no')} {(r.get('title') or '')[:28]}" for r in unk)]
        lines += [">  若其中有你认定该读的一篇，说明是元数据缺了而不是文章不好 —— 可以手动补刊名进 "
                  "`config/journal_tiers.csv` 再重跑 screen，或直接 `must_keep`。"]
    risk = [r for r in chosen if r.get("venue_risk_penalty") or
            (r.get("venue_resolved") or {}).get("tier_capped_by")]
    if risk:
        lines += ["", "> ⚠️ 期刊健康度风险（层次分已按配置显式扣减，依据=OpenAlex Sources 实查）："]
        for r in risk:
            rv = r.get("venue_resolved") or {}
            vh = r.get("venue_health") or {}
            lines.append(f"> - #{r.get('candidate_no')} {(r.get('venue') or '')[:36]}"
                         f"（{'、'.join((vh.get('flags') or []) + ([rv['tier_capped_by']] if rv.get('tier_capped_by') else []))}"
                         f"{'，撤稿占比 ' + str(vh['retraction_ratio']) if vh.get('retraction_ratio') is not None else ''}）"
                         f" —— {(r.get('title') or '')[:40]}")
    if any(r.get("score_uncertainty") for r in chosen):
        lines += ["", "> ⚠️ 部分条目的语义相关性未提供，排序仅基于词汇匹配，建议人工复核 relevance。"]
    lines += ["", "## 分项得分（可核验；加分列为封顶前原值）", "",
              "| # | uid | 基础分 | rel(词汇/语义) | 期刊 | 影响力 | 时效 | 可获取 | 权威 | 加分(原/封顶) | 综合 |",
              "|--|--|--|--|--|--|--|--|--|--|--|"]
    for r in chosen:
        bd = r.get("score_breakdown", {})
        lines.append(f"| {r.get('candidate_no')} | {r['uid']} | {bd.get('基础分')} "
                     f"| {r.get('dim_relevance')}/{r.get('relevance_lexical')} "
                     f"| {bd.get('venue')} | {bd.get('impact')} | {bd.get('recency')} | {bd.get('accessibility')} "
                     f"| {bd.get('authority')} | {r.get('bonus_raw')}/{r.get('bonus_cap')}"
                     f"{'↓'+str(r.get('bonus_capped')) if r.get('bonus_capped') else ''} "
                     f"| **{r.get('score')}** |")
    if reserve:
        lines += ["", "## 备选（未入选，按分数排序）", ""]
        for r in reserve:
            lines.append(f"- {r.get('score')} ｜ {r.get('year')} ｜ {(r.get('title') or '')[:80]}")
    open(run.path("05_candidates.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
