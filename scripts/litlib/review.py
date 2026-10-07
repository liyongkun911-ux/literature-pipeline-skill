"""Apply the user's edits on the candidate table -> 06_selected.jsonl."""
import csv
import json
import os

from .common import Run, die, norm_doi, norm_title, ok, read_jsonl, write_jsonl, now
from .score import tags_for, band, render, ROLE_CN


def apply(run, keep_all=False):
    ranked = read_jsonl(run.path("04_ranked.jsonl"))
    if not ranked:
        die("04_ranked.jsonl missing — run score first")
    by_uid = {r["uid"]: r for r in ranked}
    cand = run.path("05_candidates.csv")
    if not os.path.isfile(cand):
        die("05_candidates.csv missing — run select first")
    selected, maybe, dropped, added = [], [], [], []
    with open(cand, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            uid = (row.get("uid") or "").strip()
            verdict = (row.get("verdict") or "keep").strip().lower()
            rec = by_uid.get(uid)
            if rec is None:
                rec = {"uid": uid, "title": row.get("title", ""), "doi": row.get("doi", ""),
                       "arxiv_id": row.get("arxiv", ""), "authors": [row.get("first_author") or ""],
                       "year": row.get("year"), "venue": row.get("venue"), "score": 0,
                       "sources": ["user"], "user_added": True}
                added.append(uid)
            else:
                rec = dict(rec)
            for k_dst, k_src in (("subtopic_override", "subtopic"), ("method_override", "method"),
                                 ("priority_override", "priority"), ("user_note", "notes")):
                if (row.get(k_src) or "").strip():
                    rec[k_dst] = row[k_src].strip()
            if keep_all or verdict in ("keep", "k", "y", "yes", "保留"):
                selected.append(rec)
            elif verdict in ("maybe", "m", "待查", "?"):
                maybe.append(rec)
            else:
                dropped.append(rec)

    enrich_by_uid = {}
    if os.path.isfile(run.path("enrichment.csv")):
        with open(run.path("enrichment.csv"), encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("uid"):
                    enrich_by_uid[row["uid"].strip()] = row

    for i, r in enumerate(selected, 1):
        r["candidate_no"] = i
        e = enrich_by_uid.get(r["uid"]) or r.get("enrich") or {}
        r["enrich"] = {**{k: v for k, v in e.items() if k != "uid"},
                       **({"subtopic_label": r["subtopic_override"]} if r.get("subtopic_override") else {}),
                       **({"method_label": r["method_override"]} if r.get("method_override") else {})}
        if r.get("priority_override"):
            r["priority"] = r["priority_override"].split("/")[-1]
        else:
            r["priority"] = band(run, r.get("score") or 0)
        r["zotero_tags"] = tags_for(run, r)
        r.setdefault("zotero_status_tag", f"{run.profile['tags']['status']['prefix']}/{run.profile['tags']['status']['default']}")
        if r.get("user_note"):
            r["zotero_tags"] = r["zotero_tags"] + [f"备注/{r['user_note'][:40]}"]

    write_jsonl(run.path("06_selected.jsonl"), selected)
    write_jsonl(run.path("06_maybe.jsonl"), maybe)
    json.dump({"selected": len(selected), "maybe": len(maybe), "dropped": len(dropped),
               "user_added": added, "dropped_titles": [d.get("title", "")[:70] for d in dropped],
               "at": now()},
              open(run.path("06_review_summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  keep={len(selected)} maybe={len(maybe)} drop={len(dropped)} 用户补加={len(added)}")
    if len(selected) < 1:
        die("全部被剔除：确认 05_candidates.csv 的 verdict 列是否被清空")
    ok(f"06_selected.jsonl = {len(selected)} 篇（含 zotero_tags）")
    return selected, maybe, dropped
