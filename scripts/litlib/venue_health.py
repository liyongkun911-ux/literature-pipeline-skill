"""Journal health signals from OpenAlex Sources, probed at screen time and cached per run.

Three flags, each mapped to a decision (not a vibe):
- non_journal_carrier   OpenAlex says the venue is a book series / conference proceedings
                        -> caps the tier at T4 (user 判据1: 丛书会议集一律封顶 T4), automated.
- not_indexed           real journal with real volume but absent from every major index listing
                        (listed_in empty AND not in DOAJ) -> high-risk signal for unmatched venues.
- retraction_heavy      retracted works / total works >= threshold -> integrity signal independent
                        of any partition table.

All fields come from API responses; anything the API can't answer stays absent, never guessed.
"""
import json
import os
import urllib.parse

from .common import get_json, norm_title, ok

SOURCE_SELECT = "id,issn_l,display_name,type,is_in_doaj,listed_in,works_count"


def _mailto(prof):
    return urllib.parse.quote((prof.get("http") or {}).get("polite_mailto") or "")


def _find_source(run, prof, issn, name):
    if issn:
        d, err = get_json(
            f"https://api.openalex.org/sources?filter=issn:{issn}&select={SOURCE_SELECT}"
            f"&mailto={_mailto(prof)}", profile=prof, tries=2, timeout=30)
        hits = ((d or {}).get("results") or [])
        if hits:
            return hits[0]
        if err:
            return {"__error__": f"issn:{err}"}
    if name:
        d, err = get_json(
            f"https://api.openalex.org/sources?filter=display_name.search:{urllib.parse.quote(name)}"
            f"&select={SOURCE_SELECT}&mailto={_mailto(prof)}", profile=prof, tries=2, timeout=30)
        hits = [s for s in ((d or {}).get("results") or [])
                if norm_title(s.get("display_name") or "") == norm_title(name)]
        if hits:
            return hits[0]
        if err:
            return {"__error__": f"name:{err}"}
    return None


def _retracted_count(run, prof, source_id):
    if not source_id:
        return None
    # OpenAlex has no "count-only" param (metaonly=400, measured); per-page=1 + meta.count is the cheap form.
    d, err = get_json(
        f"https://api.openalex.org/works?filter=primary_location.source.id:{source_id},"
        f"is_retracted:true&per-page=1&mailto={_mailto(prof)}", profile=prof, tries=2, timeout=30)
    if err:
        return None
    return ((d or {}).get("meta") or {}).get("count")


def probe_venues(run, recs, refresh=False):
    prof = run.profile
    cfg = (prof["venue_screen"].get("health") or {})
    cache = run.path("03_venue_health.json")
    if os.path.isfile(cache) and not refresh:
        return json.load(open(cache, encoding="utf-8"))

    items = {}
    for r in recs:
        if (r.get("type") == "preprint") or (r.get("venue") or "").lower().startswith("arxiv"):
            continue
        issn = (r.get("venue_issn_l") or "").strip()
        name = (r.get("venue") or "").strip()
        if not issn and not name:
            continue
        items.setdefault(issn or norm_title(name), {"venue": name, "issn": issn})

    thr = float(cfg.get("retraction_ratio_threshold", 0.02))
    min_works = int(cfg.get("min_works_for_retraction_signal", 50))
    out = {}
    for key, it in items.items():
        h = {"venue": it["venue"], "issn": it["issn"], "flags": []}
        s = _find_source(run, prof, it["issn"], it["venue"])
        if s and s.get("__error__"):
            h["error"] = s["__error__"]
        elif s:
            wc = int(s.get("works_count") or 0)
            listed = s.get("listed_in") or []
            rc = _retracted_count(run, prof, (s.get("id") or "").rsplit("/", 1)[-1])
            h.update({"source_id": s.get("id", ""), "type": s.get("type", ""),
                      "is_in_doaj": bool(s.get("is_in_doaj")), "listed_in": listed,
                      "works_count": wc, "retracted": rc,
                      "retraction_ratio": round(rc / wc, 4) if (rc is not None and wc) else None})
            if (s.get("type") or "") in ("book series", "conference"):
                h["flags"].append("non_journal_carrier")
            if wc >= 100 and not listed and not h["is_in_doaj"]:
                h["flags"].append("not_indexed")
            if h["retraction_ratio"] is not None and h["retraction_ratio"] >= thr and wc >= min_works:
                h["flags"].append("retraction_heavy")
        else:
            h["error"] = "source-not-found"
        out[key] = h
    json.dump(out, open(cache, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    flagged = sorted({f for v in out.values() for f in v["flags"]})
    errs = sum(1 for v in out.values() if v.get("error"))
    ok(f"03_venue_health.json：{len(out)} 个载体已体检"
       f"{'，命中标记: ' + ','.join(flagged) if flagged else '，无风险标记'}"
       f"{'，' + str(errs) + ' 个未匹配到 source' if errs else ''}")
    return out
