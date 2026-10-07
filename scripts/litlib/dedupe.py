"""Deduplication: DOI -> arXiv -> fuzzy title, with stable IDs and preprint/published pairing.

Stable ids matter: they are what makes a re-run idempotent against Zotero.
"""
import hashlib
import json
import os
from collections import defaultdict

from .common import (Run, norm_arxiv, norm_doi, norm_title, ok, read_jsonl,
                     title_sim, write_jsonl)

TITLE_SIM = 0.86
FIELD_PRIORITY = ("title", "title_norm", "doi", "arxiv_id", "openalex_id", "s2_id", "pmid",
                  "venue", "venue_issn_l", "venue_source_id", "year", "publication_date",
                  "abstract", "pdf_url", "oa_url", "oa_status", "url", "type", "language",
                  "authors", "domains", "subfields", "cited_by_counts", "cited_by_source")
# OpenAlex / Crossref / Semantic Scholar count different things (covering and cut-off dates
# diverge by an order of magnitude), so a merged record keeps one number per source and
# exposes a single value only through this authority order. Never average or max across them.
CITED_AUTHORITY = ("openalex", "semanticscholar", "crossref", "giiisp", "arxiv")


def stable_id(rec):
    key = norm_doi(rec.get("doi")) or ("arxiv:" + norm_arxiv(rec.get("arxiv_id"))) if (
        norm_doi(rec.get("doi")) or norm_arxiv(rec.get("arxiv_id"))) else "t:" + norm_title(rec.get("title"))[:80]
    return "d" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _richness(rec):
    """Prefer the record that carries the most usable metadata."""
    s = 0
    if rec.get("abstract"):
        s += 3
    if rec.get("pdf_url") or rec.get("oa_url"):
        s += 2
    if rec.get("venue"):
        s += 2
    if rec.get("venue_issn_l"):
        s += 1
    if rec.get("doi"):
        s += 2
    if rec.get("cited_by_count"):
        s += 1
    if rec.get("publication_date"):
        s += 1
    if rec.get("type") not in ("preprint", "", None):
        s += 2
    return s


def merge(group):
    group = sorted(group, key=_richness, reverse=True)
    base = dict(group[0])
    srcs, qs, via = set(), set(), set()
    counts = {}
    for r in group:
        s = (r.get("sources") or ["unknown"])[0]
        c = int(r.get("cited_by_count") or 0)
        if s not in counts or c > counts[s]:
            counts[s] = c
    for r in group[1:]:
        for k in FIELD_PRIORITY:
            if not base.get(k) and r.get(k):
                base[k] = r[k]
        srcs.update(r.get("sources") or [])
        qs.update(r.get("query_ids") or [])
        via.add(r.get("discovered_via") or "search")
        if len(r.get("authors") or []) > len(base.get("authors") or []):
            base["authors"] = r["authors"]
    srcs.update(base.get("sources") or [])
    qs.update(base.get("query_ids") or [])
    via.add(base.get("discovered_via") or "search")
    base["sources"] = sorted(srcs)
    base["query_ids"] = sorted(qs)
    base["discovered_via"] = "+".join(sorted(via))
    base["merged_from"] = len(group)
    base["cited_by_counts"] = counts
    for s in CITED_AUTHORITY:
        if s in counts:
            base["cited_by_count"], base["cited_by_source"] = counts[s], s
            break
    else:
        base["cited_by_count"], base["cited_by_source"] = 0, ""
    return base


def pair_versions(recs):
    """Link preprint <-> published-version siblings without deleting either.

    Same normalized title but different id-space (one has DOI, one only arXiv) means
    the same work at two stages; deleting one loses the OA route or the citation record.
    """
    by_title = defaultdict(list)
    for r in recs:
        by_title[r["title_norm"]].append(r)
    pairs = []
    for tn, grp in by_title.items():
        if len(grp) < 2:
            continue
        published = [g for g in grp if g.get("doi") and g.get("type") != "preprint"]
        preprints = [g for g in grp if not g.get("doi") or g.get("type") == "preprint"]
        for p in preprints:
            if published and p != published[0]:
                p["version_of"] = published[0]["uid"]
                published[0].setdefault("versions", []).append(p["uid"])
                pairs.append({"canonical": published[0]["uid"], "preprint": p["uid"], "title": tn[:70]})
    return pairs


def dedupe(run, threshold=TITLE_SIM, fuzzy=True):
    raw = read_jsonl(run.path("01_raw.jsonl"))
    if not raw:
        return [], {"error": "01_raw.jsonl empty — run harvest first"}
    plan = {}
    if os.path.isfile(run.path("queries.json")):
        plan = json.load(open(run.path("queries.json"), encoding="utf-8"))
    must_keep = {norm_doi(x) or norm_title(x) for x in (plan.get("must_keep") or [])}

    for r in raw:
        r["title_norm"] = norm_title(r.get("title"))
        r["doi"] = norm_doi(r.get("doi"))
        r["arxiv_id"] = norm_arxiv(r.get("arxiv_id"))

    dsu = DSU()
    for i, r in enumerate(raw):
        dsu.find(i)

    def link(index, key_fn, label, log):
        buckets = defaultdict(list)
        for i, r in enumerate(raw):
            k = key_fn(r)
            if k:
                buckets[k].append(i)
        for k, idxs in buckets.items():
            for j in idxs[1:]:
                if dsu.find(idxs[0]) != dsu.find(j):
                    log.append({"key": label, "value": str(k)[:70], "merged": [raw[idxs[0]]["title_norm"][:60], raw[j]["title_norm"][:60]]})
                dsu.union(idxs[0], j)
        return {k: len(v) for k, v in buckets.items() if len(v) > 1}

    events = []
    doi_groups = link(dsu, lambda r: r["doi"], "doi", events)
    arx_groups = link(dsu, lambda r: r["arxiv_id"], "arxiv", events)

    fuzzy_merged = 0
    if fuzzy:
        items = [(i, r) for i, r in enumerate(raw) if r["title_norm"]]
        grams = {i: r["title_norm"].split() for i, r in items}
        for a in range(len(items)):
            ia, ra = items[a]
            ba = grams[ia]
            if len(ba) < 3:
                continue
            for b in range(a + 1, len(items)):
                ib, rb = items[b]
                bb = grams[ib]
                if abs(len(ba) - len(bb)) > max(2, 0.5 * len(ba)):
                    continue
                if ra["doi"] and rb["doi"] and ra["doi"] != rb["doi"]:
                    continue
                if title_sim(ra["title_norm"], rb["title_norm"]) >= threshold:
                    if dsu.find(ia) != dsu.find(ib):
                        events.append({"key": "title_fuzzy", "value": f"{ra['title_norm'][:46]} || {rb['title_norm'][:46]}",
                                       "merged": [ra.get("uid"), rb.get("uid")]})
                        fuzzy_merged += 1
                    dsu.union(ia, ib)

    comps = defaultdict(list)
    for i in range(len(raw)):
        comps[dsu.find(i)].append(raw[i])
    merged = []
    for root, grp in comps.items():
        m = merge(grp)
        m["uid"] = stable_id(m)
        if norm_doi(m.get("doi")) in must_keep or m["title_norm"] in must_keep:
            m["must_keep"] = True
        merged.append(m)

    # Order by cross-source corroboration first: the citation number is not comparable
    # across sources, so it must not decide who sits at the top of the pool.
    merged.sort(key=lambda r: (-len(r.get("sources") or []), -(r.get("cited_by_count") or 0),
                               r.get("year") or 0))
    pairs = pair_versions(merged)

    stats = {
        "input": len(raw), "output": len(merged),
        "doi_collisions": sum(v - 1 for v in doi_groups.values()),
        "arxiv_collisions": sum(v - 1 for v in arx_groups.values()),
        "title_fuzzy_merges": fuzzy_merged,
        "version_pairs": len(pairs),
        "multi_source": sum(1 for m in merged if len(m.get("sources") or []) > 1),
    }
    write_jsonl(run.path("02_deduped.jsonl"), merged)
    json.dump({"stats": stats, "events": events[:800], "version_pairs": pairs},
              open(run.path("02_dedup_log.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    run.save_state("dedupe", records=len(merged), **stats)
    print("  " + " | ".join(f"{k}={v}" for k, v in stats.items()))
    ok(f"02_deduped.jsonl = {len(merged)} unique works from {len(raw)} raw")
    return merged, stats
