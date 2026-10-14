"""Stage 10 — 长期库增量维护（李終碩流程）：PDF→MD 转换 + 选题字段提取表。

前 9 阶段是"从主题找文献"的批量流水线；本模块是"定题之后持续加文献"的长期循环：
新 PDF 丢进课题 pdf 文件夹 → `intake tomd` 转 MD（Obsidian/AI 直接读 md，比读 PDF 快）
→ `intake table` 把表里还没有的 PDF 追加成空行 → 模型逐篇读 md 填字段（字段按选题设计）
→ 看表判断哪篇值得进 Obsidian 精读。

幂等：重跑不重复已有 MD / 不重复已有行（按 pdf 绝对路径匹配）。
表结构在首次建表时定死（--fields 只在建表时生效）；要换字段就归档旧表重建。
"""
import csv
import json
import os
import shutil
import subprocess

from .common import Run, die, now, ok

# 选题无关的通用核心字段；建表时用 --fields 换成按选题设计的字段
DEFAULT_FIELDS = ["标题", "年份", "研究问题", "核心方法", "主要结论", "可复用点",
                  "局限与疑点", "与本主题关联", "阅读状态"]


def _pdfs_in(pdf_dir, recursive=False):
    if recursive:
        out = []
        for root, _dirs, files in os.walk(pdf_dir):
            out += [os.path.join(root, f) for f in files if f.lower().endswith(".pdf")]
        return sorted(out)
    return sorted(os.path.join(pdf_dir, f) for f in os.listdir(pdf_dir)
                  if f.lower().endswith(".pdf"))


def tomd(run, pdf_dir, out_dir=None, force=False):
    """PDF 文件夹 → MD 文件夹（李終碩：一个 pdf 文件夹一个 md 文件夹）。"""
    pdf_dir = os.path.abspath(pdf_dir)
    if not os.path.isdir(pdf_dir):
        die(f"PDF 目录不存在：{pdf_dir}")
    out_dir = os.path.abspath(out_dir or os.path.join(os.path.dirname(pdf_dir), "md"))
    os.makedirs(out_dir, exist_ok=True)
    pt = shutil.which("pdftotext")
    if not pt:
        die("找不到 pdftotext（poppler）。Windows：winget install oschwartz10612.poppler "
            "或 scoop install xpdf。不做纯 Python 兜底——转不出正文就如实失败，不产空壳 MD。")
    done = skipped = failed = 0
    rows = []
    for src in _pdfs_in(pdf_dir, recursive=True):
        f = os.path.basename(src)
        dst = os.path.join(out_dir, os.path.splitext(f)[0] + ".md")
        if os.path.isfile(dst) and not force:
            skipped += 1
            rows.append({"pdf": f, "status": "cached"})
            continue
        try:
            proc = subprocess.run([pt, "-layout", "-enc", "UTF-8", src, "-"],
                                  capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            failed += 1
            rows.append({"pdf": f, "status": "failed", "err": "timeout 180s"})
            continue
        text = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode != 0 or len(text.strip()) < 40:
            failed += 1
            rows.append({"pdf": f, "status": "failed",
                         "err": (proc.stderr.decode("utf-8", "replace") or "输出过短，疑扫描件/图片PDF")[:150]})
            continue
        head = (f"# {os.path.splitext(f)[0]}\n\n"
                f"> 来源 PDF：`{src}`\n> 转换时间：{now()}\n\n---\n\n")
        open(dst, "w", encoding="utf-8").write(head + text)
        done += 1
        rows.append({"pdf": f, "status": "ok", "md": dst, "chars": len(text)})
    json.dump({"at": now(), "pdf_dir": pdf_dir, "out_dir": out_dir,
               "done": done, "skipped": skipped, "failed": failed, "rows": rows},
              open(run.path("09_tomd_status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    ok(f"MD 转换：新转 {done}，已存在 {skipped}，失败 {failed} → {out_dir}")
    if failed:
        print(f"  失败明细见 09_tomd_status.json（扫描件需 OCR，不是转换 bug，别当网络问题重跑）")
    return rows


def table(run, pdf_dir, out=None, fields_file=None):
    """扫描 pdf 文件夹，把表里没有的 PDF 追加成空行；字段列由选题决定。"""
    pdf_dir = os.path.abspath(pdf_dir)
    if not os.path.isdir(pdf_dir):
        die(f"PDF 目录不存在：{pdf_dir}")
    out = os.path.abspath(out or os.path.join(os.path.dirname(pdf_dir), "literature-table.csv"))
    md_dir = os.path.join(os.path.dirname(pdf_dir), "md")

    rows, fieldnames = [], None
    if os.path.isfile(out):
        with open(out, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            rows = list(rd)
            fieldnames = rd.fieldnames
        if not fieldnames:
            die(f"表存在但读不到表头：{out}")
        if fields_file:
            print("  提示：表已存在，--fields 只在首次建表时生效（换字段请归档旧表重建）")
    else:
        fields = DEFAULT_FIELDS
        if fields_file:
            fields = json.load(open(fields_file, encoding="utf-8"))
            if not isinstance(fields, list) or not all(isinstance(x, str) for x in fields):
                die("--fields 文件必须是 JSON 字符串数组，如 [\"研究问题\",\"方法\",...]")
        fieldnames = ["文件名", "pdf路径", "md路径"] + fields

    seen = {r.get("pdf路径", "") for r in rows}
    added = 0
    for p in _pdfs_in(pdf_dir, recursive=True):
        if p in seen:
            continue
        f = os.path.basename(p)
        mdp = os.path.join(md_dir, os.path.splitext(f)[0] + ".md")
        row = {c: "" for c in fieldnames}
        row["文件名"] = f
        row["pdf路径"] = p
        row["md路径"] = mdp if os.path.isfile(mdp) else ""
        rows.append(row)
        added += 1

    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    blank = sum(1 for r in rows if not any((r.get(c) or "").strip() for c in fieldnames[3:]))
    json.dump({"at": now(), "pdf_dir": pdf_dir, "table": out,
               "total": len(rows), "added": added, "blank_rows": blank},
              open(run.path("09_table_status.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    ok(f"提取表：共 {len(rows)} 行，新增 {added}，未填 {blank} → {out}")
    if added:
        print("  下一步：模型逐篇读 md 填字段（铁律9：读到才能填；扫描件/无 md 的篇目标注缺口，不臆填）")
    return rows
