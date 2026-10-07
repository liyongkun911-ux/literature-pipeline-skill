"""literature-pipeline CLI — one entry point, one artifact per stage.

Every subcommand is idempotent and resumes from existing artifacts; nothing re-harvests
or re-imports what is already on disk unless you pass --force.
"""
import argparse
import json
import os
import shutil
import urllib.parse
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litlib import common, harvest, dedupe, screen, score, review, zotero, pdfs, notes, evidence  # noqa: E402
from litlib.common import Run, DEFAULT_PROFILE, die, ok, read_jsonl, slugify, now  # noqa: E402


def _run(a):
    return Run(a.run, base=a.dir)


def cmd_init(a):
    rid = a.run or f"{now()[:10]}-{slugify(a.topic, 32)}"
    d = os.path.join(a.dir, "runs", rid)
    os.makedirs(d, exist_ok=True)
    prof = json.load(open(a.profile or DEFAULT_PROFILE, encoding="utf-8"))
    prof["topic"] = a.topic
    if a.mailto:
        prof["http"]["polite_mailto"] = a.mailto
    if a.journal_dir:
        prof["venue_screen"]["journal_directory"] = a.journal_dir.replace(os.sep, "/")
    prof["window"]["years"] = a.years
    prof["selection"]["candidates"] = a.candidates
    pf = os.path.join(d, "profile.json")
    if not os.path.isfile(pf):
        json.dump(prof, open(pf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    for sub in ("bib", "pdfs"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    print(d)
    ok(f"run 初始化于 {d}（topic={a.topic!r} 时间窗={a.years}年 候选={a.candidates}）")
    print("  下一步：按 references/01-query-recipe.md 写 runs/%s/queries.json，再 `lit.py harvest --run %s`" % (rid, rid))
    return rid


def cmd_probe(a):
    run = _run(a)
    prof = run.profile
    print("== 检索源可达性 ==")
    oa_auth = "mailto=%s" % urllib.parse.quote(prof["http"]["polite_mailto"])
    oa_key = harvest.openalex_key()
    if oa_key:
        oa_auth += "&api_key=" + urllib.parse.quote(oa_key)
    tests = [("openalex", "https://api.openalex.org/works?per-page=1&search=test&" + oa_auth),
             ("crossref", "https://api.crossref.org/works?rows=1&query=test"),
             ("arxiv", "https://export.arxiv.org/api/query?search_query=all:test&max_results=1"),
             ("semanticscholar", "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1"),
             ("unpaywall", "https://api.unpaywall.org/v2/10.1038/nature12579?email=" + prof["http"]["polite_mailto"])]
    for name, url in tests:
        if name == "giiisp" and not os.environ.get("GIIISP_AUTH_TOKEN"):
            continue
        st, body, err = common.http_get(url, profile=prof, tries=2, timeout=25)
        n = 0
        try:
            n = len(body)
        except Exception:
            pass
        print(f"  {name:<16} {'OK' if st == 200 else 'DEGRADED'} {st} ({n}B) {err or ''}")
        if name == "openalex" and st == 429 and b"budget" in (body or b"").lower():
            print("      ↑ 共享出口 IP 的每日免费额度已用尽。mailto 解不了，需设 OPENALEX_API_KEY 或等次日重置")
    print(f"  giiisp           {'OK token set' if os.environ.get('GIIISP_AUTH_TOKEN') else 'NO GIIISP_AUTH_TOKEN'}")
    print(f"  openalex key     {'OK（env 或用户变量已读到）' if harvest.openalex_key() else 'NO OPENALEX_API_KEY（匿名池易撞每日额度）'}")
    if prof["http"]["polite_mailto"].startswith("REPLACE"):
        print("  WARN http.polite_mailto 仍是占位值：OpenAlex/Unpaywall 的礼貌池会更快限流，请在 profile.json 填真实邮箱")
    print("== 期刊权威性分级 ==")
    tbl, jerr = screen.load_journal_dir(run)
    counts = tbl.get("counts", {})
    print(f"  内置 journal_tiers.csv = {counts.get('builtin', 0)} 条 · 可匹配刊名/别名 {len(tbl['by_name'])} 个"
          f" · 认可层次 {prof['venue_screen'].get('accept_tiers')}")
    if counts.get("school") is not None:
        print(f"  校内目录覆盖 {counts['school']} 条")
    if jerr:
        print(f"  WARN {jerr}")
    print("== Zotero 通道 ==")
    zotero.probe(run)


def cmd_status(a):
    run = _run(a)
    st = run.state()
    print(f"run {run.run_id}")
    for k in sorted(st.get("stages", {})):
        v = {x: y for x, y in st["stages"][k].items() if x != "at"}
        print(f"  {k:<16} {st['stages'][k].get('at','')}  {json.dumps(v, ensure_ascii=False)[:150]}")
    missing = [f for f in ("queries.json", "01_raw.jsonl", "02_deduped.jsonl", "enrichment.csv",
                           "03_screened.jsonl", "04_ranked.jsonl", "05_candidates.csv", "06_selected.jsonl",
                           "bib/recs.ris", "pdf_manifest.csv", "reading-nav.md") if not run.has(f)]
    print("  待产出：" + (", ".join(missing) if missing else "全部完成"))


def cmd_report(a):
    run = _run(a)
    prov = {}
    if run.has("00_provenance.json"):
        prov = json.load(open(run.path("00_provenance.json"), encoding="utf-8"))
    lines = [f"# 检索与筛选报告 — {run.profile.get('topic')} ({run.run_id})", ""]
    if run.has("queries.json"):
        q = json.load(open(run.path("queries.json"), encoding="utf-8"))
        lines += ["## 检索式（可复现）", "", "| id | 源 | 语言 | 目的 | 表达式 |", "|--|--|--|--|--|"]
        for s in q.get("strings", []):
            lines.append(f"| {s.get('id')} | {s.get('api')} | {s.get('lang')} | {s.get('purpose','')} | `{str(s.get('expr'))[:160]}` |")
        lines.append("")
    for call in prov.get("source_calls", []):
        lines.append(f"## {call['source']} @ {call['at']}")
        for l in call.get("log", []):
            lines.append(f"- {l.get('source')}: {l.get('status')} returned={l.get('returned', 0)} {l.get('reason') or ''}")
        lines.append("")
    if run.has("02_dedup_log.json"):
        s = json.load(open(run.path("02_dedup_log.json"), encoding="utf-8"))["stats"]
        lines += ["## 去重", *[f"- {k}: {v}" for k, v in s.items()], ""]
    if run.has("03_drop_log.csv"):
        rows = list(__import__("csv").DictReader(open(run.path("03_drop_log.csv"), encoding="utf-8-sig")))
        agg = {}
        for r in rows:
            agg[r["reason"]] = agg.get(r["reason"], 0) + 1
        lines += ["## 剔除统计（reason code 见 references/03-screen-scoring.md）",
                  *[f"- {k}: {v}" for k, v in sorted(agg.items())], ""]
    if run.has("06_review_summary.json"):
        lines += ["## 人工确认", json.dumps(json.load(open(run.path("06_review_summary.json"), encoding="utf-8")), ensure_ascii=False)[:400], ""]
    if run.has("pdf_manifest.csv"):
        import collections
        rows = list(__import__("csv").DictReader(open(run.path("pdf_manifest.csv"), encoding="utf-8-sig")))
        have = [r for r in rows if r["status"] in ("downloaded", "cached")]
        miss = [r for r in rows if r["status"] == "manual_required"]
        cls = collections.Counter(r["pdf_class"] for r in miss)
        lines += ["## 全文可得性（只统计合法 OA 通道，不绕付费墙）",
                  f"- 已有全文：{len(have)}/{len(rows)}；缺：{len(miss)}",
                  f"- 缺的原因分布：{dict(cls) or '—'}",
                  "- 逐篇合法获取入口见 `pdf_unresolved.md`；`anti_bot_blocked`/`landing_only` "
                  "两类用自己的浏览器打开链接即可，不需要任何绕过。",
                  "- 来自 arXiv 的是**预印本**，与正式刊版本内容可能不同，引用时注明读的是哪个版本。", ""]
    open(run.path("report.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    ok(f"report.md 写出（{run.path('report.md')}）")


def main():
    p = argparse.ArgumentParser(prog="lit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    gp = argparse.ArgumentParser(add_help=False)
    gp.add_argument("--run", required=True, help="run id under <dir>/runs/")
    gp.add_argument("--dir", default=os.environ.get("LITPIPE_DIR", os.getcwd()),
                    help="workspace containing runs/ (default: $LITPIPE_DIR or cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    gpi = argparse.ArgumentParser(add_help=False)
    gpi.add_argument("--run", help="omit to auto-generate from topic + date")
    gpi.add_argument("--dir", default=os.environ.get("LITPIPE_DIR", os.getcwd()))
    s = sub.add_parser("init", parents=[gpi]); s.add_argument("--topic", required=True)
    s.add_argument("--years", type=int, default=5); s.add_argument("--candidates", type=int, default=20)
    s.add_argument("--profile"); s.add_argument("--mailto"); s.add_argument("--journal-dir")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("probe", parents=[gp], help="read-only environment + Zotero channel check"); s.set_defaults(fn=cmd_probe)
    s = sub.add_parser("status", parents=[gp]); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("report", parents=[gp]); s.set_defaults(fn=cmd_report)

    s = sub.add_parser("harvest", parents=[gp]); s.add_argument("--source", action="append"); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=lambda a: harvest.harvest(_run(a), only=a.source, dry=a.dry_run))

    s = sub.add_parser("dedupe", parents=[gp]); s.add_argument("--threshold", type=float, default=0.86); s.add_argument("--no-fuzzy", action="store_true")
    s.set_defaults(fn=lambda a: dedupe.dedupe(_run(a), threshold=a.threshold, fuzzy=not a.no_fuzzy))

    s = sub.add_parser("enrich-template", parents=[gp]); s.add_argument("--limit", type=int, default=400)
    s.set_defaults(fn=lambda a: screen.emit_template(_run(a), limit=a.limit))

    s = sub.add_parser("screen", parents=[gp]); s.set_defaults(fn=lambda a: screen.screen(_run(a)))
    s = sub.add_parser("score", parents=[gp]); s.set_defaults(fn=lambda a: score.score(_run(a)))
    s = sub.add_parser("select", parents=[gp]); s.set_defaults(fn=lambda a: score.select(_run(a)))

    s = sub.add_parser("review-apply", parents=[gp]); s.add_argument("--keep-all", action="store_true")
    s.set_defaults(fn=lambda a: review.apply(_run(a), keep_all=a.keep_all))

    s = sub.add_parser("zotero", parents=[gp])
    s.add_argument("action", choices=["probe", "build", "import", "verify", "find-pdf", "attach"])
    s.add_argument("--channel", choices=["auto", "local_api", "connector", "file_handoff"])
    s.add_argument("--collection"); s.add_argument("--yes", action="store_true")
    s.add_argument("--with-tags", action="store_true", help="attach 时把 06_selected.jsonl 的标签一并 PATCH 进库")
    s.set_defaults(fn=lambda a: {"probe": zotero.probe, "build": zotero.build,
                                 "import": lambda r: zotero.import_items(r, channel=a.channel, do_write=a.yes, collection=a.collection),
                                 "verify": zotero.verify, "find-pdf": zotero.find_pdf_list,
                                 "attach": lambda r: zotero.attach(r, do_write=a.yes, with_tags=a.with_tags)}[a.action](_run(a)))

    s = sub.add_parser("pdfs", parents=[gp]); s.add_argument("action", choices=["fetch", "retag", "browser-queue", "browser-serve", "browser-commit"], nargs="?", default="fetch")
    s.add_argument("--limit", type=int); s.add_argument("--overwrite", action="store_true")
    s.add_argument("--timeout", type=int, default=900)
    s.set_defaults(fn=lambda a: {"fetch": lambda r: pdfs.fetch(r, limit=a.limit, overwrite=a.overwrite),
                                 "retag": pdfs.retag,
                                 "browser-queue": lambda r: pdfs.browser_queue(r, limit=a.limit),
                                 "browser-serve": lambda r: pdfs.browser_serve(r, timeout=a.timeout),
                                 "browser-commit": pdfs.browser_commit}[a.action](_run(a)))

    s = sub.add_parser("evidence", parents=[gp])
    s.add_argument("action", choices=["claims-template", "claims-check", "exemplar-pick", "exemplar-check"])
    s.add_argument("--venue"); s.add_argument("--n-field", type=int); s.add_argument("--n-venue", type=int)
    s.set_defaults(fn=lambda a: (
        evidence.claims_template(_run(a)) if a.action == "claims-template" else
        evidence.claims_check(_run(a)) if a.action == "claims-check" else
        evidence.exemplar_check(_run(a)) if a.action == "exemplar-check" else
        evidence.exemplar_pick(_run(a), venue=a.venue, n_field=a.n_field, n_venue=a.n_venue)))

    s = sub.add_parser("notes", parents=[gp]); s.add_argument("action", choices=["template", "build"]); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=lambda a: (notes.template(_run(a), force=a.force) if a.action == "template"
                                 else notes.build(_run(a), force=a.force)))

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    main()
