# literature-pipeline

**一个把「研究主题」变成「可复现检索链 + 可执行阅读计划」的技能（Skill）——12 个阶段、1 个 CLI、全 JSONL/CSV 产物。**

> **EN TL;DR** — A script-driven, resumable literature-review pipeline for research topics.
> Query design → multi-source recall (OpenAlex / Crossref / arXiv / Semantic Scholar / GIIISP) →
> DOI+title dedup → venue-tier gate & retraction check → 0–100 two-layer scoring →
> human-confirmed shortlist → Zotero import → legal open-access full text → per-paper reading
> notes with page-level citations → three-pass reading order → claim-evidence audit and target-venue
> style dossier. Stdlib-only Python, every stage idempotent, every artifact on disk.
> **It never bypasses paywalls and never fabricates metrics.**

---

## 这是什么

从「我有个题目」到「我能写开题 / 相关工作 / 引用论断」之间，有一段最费时又最难复现的活：
检索式怎么拆、召回够不够、哪些该读哪些该丢、为什么这篇排在前面、哪句话出自第几页。

这个技能把这段全部铺成一条**可断点续跑**的流水线，并且守住一条分界：

> 确定性算术全部在脚本里，语义判断全部落在文件里，两边用 CSV / JSONL 契约交接。

因此任何一次排名都能回溯到「哪条检索式、哪个源、哪个维度贡献了多少分」，
而不是一个不可解释的模型印象。

## 什么时候用

给一个研究主题，要求查文献 / 找论文 / 做文献综述 / 写开题与相关工作 / 整理进 Zotero /
「有哪些相关文献我该读」/「帮我准备能站得住的引用论断」/「这篇期刊的论文是怎么写的」。

**可以只跑其中一段**：想摸方向就跑 `scout` 档拿 5 篇；只想入库就跑 1–9 阶段；
已经有全文只想要阅读导航就跑 10 阶段。

## 边界（它不做什么）

- **不绕付费墙。** 全文只走出版商 / 作者 / 机构自己公开的副本；付费墙与 CDN 反爬墙一律不绕过、
  不伪装、不找镜像站。用户本人有权的入口（机构订阅）走浏览器通道逐篇确认，属合法路径。
- **不编造数据。** 期刊层次、影响因子、引用数都必须来自 API 或用户给的目录文件，缺就标缺。
- **不静默删文献。** 每条剔除都带 reason code（R01–R09），拿不准的进 `needs_judgement`。
- **不代读。** 只有摘要可看就标 `仅摘要`；要引其中的数字必须读到结果章节并写明页 / 节。
- **写库需同意。** `zotero import` 先产出 RIS 给用户过目，拿到明确同意才允许 `--yes` 落库。

---

## 流水线一览

| # | 命令 | 产物 | 谁做判断 |
|---|---|---|---|
| 0 | 定档 `selection.mode`：scout(5 篇摸方向) / related_work(默认) / systematic | 规模 | 模型 + 用户 |
| 1 | 手写 `runs/<id>/queries.json` | 检索式 | 模型 |
| 2 | `harvest` | `01_raw.jsonl`, `00_provenance.json` | 脚本 |
| 3 | `dedupe` | `02_deduped.jsonl`, `02_dedup_log.json` | 脚本 |
| 4 | `enrich-template` → 填 `enrichment.csv` | 方向 / 相关性 / 角色 / 标签 | 模型 |
| 5 | `screen` | `03_screened.jsonl`, `03_drop_log.csv`, `03_unresolved_venues.csv` | 脚本 |
| 6 | `score` → `select` | `04_ranked.jsonl`, `05_candidates.csv/.md` | 脚本 |
| 7 | 用户改 `verdict` → `review-apply` | `06_selected.jsonl`（含标签） | **人** |
| 8 | `pdfs fetch` → `browser-queue` → 浏览器代取 `browser-serve` → `retag` | `pdfs/`, `pdf_manifest.csv`, `zotero-find-pdf.md` | 脚本 + 浏览器（用户登录态） |
| 9 | `zotero build` → 确认 → `import --yes` → `verify` → `attach --with-tags --yes` | `bib/recs.ris`, `recs.bib`, `zotero_state.json` | 人 + 脚本 |
| 10 | `notes template` → 填 → `notes build` | `reading-nav.md`, `reading-matrix.csv`, `reading-order.json` | 模型 + 脚本 |
| 11 | `evidence claims-template` → 填 `claims.csv` → `claims-check` | `evidence/claims_audit.csv` | 模型写 + **脚本审计** |
| 12 | `evidence exemplar-pick` → 填档案 → `exemplar-check` | `evidence/exemplars.csv`, `exemplar_dossier.md` | 脚本选 + 模型填 |
| — | `report` | `report.md`（检索式、各源命中、去重统计、reason code 分布） | 脚本 |

顺序要点：**先取全文再写库**（第 8 步早于第 9 步），否则 `状态/` 标签要改第二遍。

### 三个会主动停下来的地方

1. **步骤 1 之后**——把 `queries.json` 的概念组念给用户听（尤其中英术语与上下位词），方向偏了后面全白做。
2. **步骤 6 之后**——交付 `05_candidates.md`，用户可改 `verdict` / `priority` / `subtopic` / `method` / `notes`；有 WARN 时原样转述，不只报好消息。
3. **步骤 9 之前**——展示 `bib/recs.ris` 摘要 + Zotero 通道结论，问是否写入、写进哪个 collection。

---

## 快速开始

```bash
# 0) 安装：仓库根目录就是技能本体，目录名必须是 literature-pipeline
git clone https://github.com/liyongkun911-ux/literature-pipeline-skill.git \
  ~/.qoder-cn/skills/literature-pipeline

# 1) 建 run（产物落在 <工作区>/runs/<id>/；工作区默认 = $LITPIPE_DIR 或当前目录）
python <skill>/scripts/lit.py init --topic "你的研究主题" --run 2026-10-08-demo \
  --years 5 --candidates 20 --mailto you@example.com

# 2) 只读体检：五源可达性 / key 是否配 / 分级表是否加载 / Zotero 四条通道哪条可用
python <skill>/scripts/lit.py probe --run 2026-10-08-demo

# 3) 按 references/01-query-recipe.md 手写 runs/2026-10-08-demo/queries.json，然后：
python <skill>/scripts/lit.py harvest --run 2026-10-08-demo
python <skill>/scripts/lit.py dedupe  --run 2026-10-08-demo
# ... 每一阶段一个命令，产物落在 run 目录，随时 `status` 看进度
```

所有命令都带 `--run <id>`，`--dir` 指定工作区；每个子命令幂等，已存在的产物不会重跑（要覆盖加 `--force`）。
`--mailto` 建议填真实邮箱（Crossref / Unpaywall 礼貌池）；**它对 OpenAlex 的「共享 IP 每日额度耗尽」无效**，
那不是礼貌问题而是配额问题——解药是 `OPENALEX_API_KEY`。

## 命令总览

| 命令 | 作用 |
|---|---|
| `init` | 建 run：写 `profile.json`（主题 / 时间窗 / 候选数 / 邮箱 / 校内目录），建 `runs/<id>/{bib,pdfs}` |
| `probe` | 只读体检：五源可达性、`OPENALEX_API_KEY` / `GIIISP_AUTH_TOKEN`、分级表、Zotero 四通道 |
| `status` | 该 run 各阶段状态 + 「待产出」清单 |
| `report` | `report.md`：检索式表、各源命中与降级、去重统计、reason code 分布、全文可得性 |
| `harvest` | 多源召回 → `01_raw.jsonl` + `00_provenance.json`（`--source` 单跑、`--dry-run` 只看不落） |
| `dedupe` | DOI + 标题模糊去重 → `02_deduped.jsonl`、`02_dedup_log.json`（`--threshold`、`--no-fuzzy`） |
| `enrich-template` | 生成 `enrichment.csv` 模板（方向 / 相关性 / 角色 / 标签由模型填） |
| `screen` | 期刊层次闸门 + 撤稿/预警核查 → `03_screened.jsonl`、`03_drop_log.csv`、`03_venue_health.json` |
| `score` | 双层算分（相关性×70 + 成果加分 ≤30 封顶）→ `04_ranked.jsonl` |
| `select` | 角色配额 + 多样性约束选候选 → `05_candidates.csv/.md`（附 `score_breakdown`） |
| `review-apply` | 应用人改的 verdict → `06_selected.jsonl`（`--keep-all` 全收） |
| `zotero` | 子动作 `probe / build / import / verify / find-pdf / attach`：四通道入库、查重、回读校验、挂 linked_file 附件、推标签 |
| `pdfs` | 子动作 `fetch / retag / browser-queue / browser-serve / browser-commit`：合法 OA 抓取 + 浏览器代取通道 |
| `evidence` | 子动作 `claims-template / claims-check / exemplar-pick / exemplar-check`：论断支撑表与目标刊体例档案（脚本审计） |
| `notes` | 子动作 `template / build`：逐篇判读 → 阅读导航（7 个必填字段没填满就拒绝出稿，`--force` 出带未完成标记的草稿） |

## 产物契约

一次完整 run 在 `runs/<id>/` 下留下的东西：

```
00_provenance.json          每源每次调用：状态、返回条数、query id
01_raw.jsonl                原始召回（未去重）
02_deduped.jsonl / _log     去重结果 + 合并日志（含 title_fuzzy_merges）
enrichment.csv              模型填的语义维度
03_screened.jsonl           过闸门后的池子
03_drop_log.csv             每条剔除 + reason code（R01–R09）
03_unresolved_venues.csv    分级表查不到的刊，汇总待补
03_venue_health.json        期刊体检缓存（撤稿/载体类型/索引状态）
03_needs_judgement.jsonl    拿不准的，不静默删也不静默留
04_ranked.jsonl             逐维得分与 score_breakdown
05_candidates.csv / .md     候选清单（人工确认的主界面）
06_selected.jsonl           人确认后的入选集（含标签）
06_review_summary.json      本轮确认摘要
07_pdf_status.json          逐篇全文状态
08_browser_queue.md         浏览器代取的清单
08_notes_incomplete.json    判读缺哪些字段
bib/recs.ris / recs.bib     可导入 Zotero / 引用的条目
zotero_state.json           写库回读校验
pdfs/                       全文（按 run 归档）
pdf_manifest.csv            全文台账
reading-nav.md / -matrix.csv / -order.json   阅读计划（三遍法）
report.md                   全过程可核验报告
```

## 期刊权威性：硬闸门，但只是参考映射

`config/journal_tiers.csv` 内置 194 条记录（`tier_basis` 记判定依据，可回溯到「凭什么这么判」）：

| tier | 含义 | 层次分 |
|---|---|---|
| T1 / T2 / T3 | 顶刊 / 权威 / 良好 | 1.0 / 0.78 / 0.52 |
| T4 | 一般（含 book series / conference 载体自动封顶） | 0.26，默认仍认可但不加分 |
| T5 | 预警/灌水 | 0.00，按 R04 剔除 |
| 未收录 | 未知，不判死刑 | `unknown_score=zero`，并汇总进 `03_unresolved_venues.csv` |
| PRE | 预印本，单列不降格 | 0.48 |

- `screen` 时自动对 ISSN/刊名实查 OpenAlex Sources：`non_journal_carrier` 自动封顶 T4；
  `retraction_heavy`（撤稿占比 ≥ 阈值）与 `not_indexed` 按 `health.penalties` 显式扣分并披露。
- 换成学校口径：`venue_screen.journal_directory` 指向校内目录 CSV，同名条目**覆盖**内置表。
- 收紧 / 放宽：`venue_screen.accept_tiers`、`known_low_tier_action`、`unknown_action`。

**这张表是未官方核实的参考映射，不是质量证据。** 中科院分区表平台及 API 已于 **2026-09-30 停运**、
不再编纂公开排名；`tier_basis=cas_partition_ref` 指的是停运前最后公开版。正式场合（开题 / 毕业 / 报奖）
以本校科研目录或当年有效的官方文件为准。技能不臆造精确影响因子（无合法免密来源）。

## 全文获取：分层，不硬碰

`pdfs fetch` 依次尝试出版商 / 作者 / 机构自行公开的副本：
arXiv、OpenAlex 全部 `locations`、Unpaywall 全部 `oa_locations`、Semantic Scholar per-DOI、
Crossref PDF link（只取 `content-type=application/pdf`）、Europe PMC 渲染直链（只取 OA）。

拿不到就**分类 + 交付**，绝不硬凑：

| 分类 | 含义 | 处置 |
|---|---|---|
| `anti_bot_blocked` | 内容本身开放，只是本机出口被 CDN 拒 | 浏览器点开清单里的入口即得 |
| `landing_only` | 只登记到 HTML 落地页而非真 PDF 直链 | 同上 |
| `closed_paywall` | 订阅制 | 不找镜像、不绕墙；有机构订阅时走浏览器代取 |
| `no_open_copy` | 索引里没有合法开放副本 | 机构订阅 / 馆际互借 |
| `too_large` / `transient_error` | 超上限 / 临时故障 | 可重试或手动 |

有机构订阅的用户走 `pdfs browser-queue` → 浏览器逐篇代取 → `browser-serve` 本机回传 →
`browser-commit`（支持把真实浏览器下载到本地的文件按 `path` 回写收录，带魔数校验）。逐篇确认，不批量。

## 配置

- `config/profile.default.json` — 全部口径的默认值（时间窗、各源配额、`http` 退避、`venue_screen`、
  `scoring`、`selection`、`zotero`、`tags` 标签 schema、`pdf`、`reading_notes`、`evidence`）。
- `init` 时复制进 `runs/<id>/profile.json`，**之后改口径只改这份**，不要在脚本或 SKILL.md 里硬编码。

改口径的常见入口：

| 想改什么 | 改哪 |
|---|---|
| 时间窗 / 候选数 | `init --years --candidates` 或 `profile.window`、`profile.selection` |
| 各源额度与开关 | `profile.sources.*`（默认 OpenAlex 400 / Crossref 300 / arXiv 300 / S2 300 / GIIISP 200 且默认关） |
| 认可层次、未收录刊处置 | `profile.venue_screen.*` |
| 相关性权重、加分子项上限 | `profile.scoring.*` |
| 角色配额、多样性约束 | `profile.selection.*` |
| 全文存放位置 | `profile.pdf.storage_root`（默认 `pdfs`，落在 `<工作区>/pdfs/<run-id>/`；课题目录用法：指向 `<课题>/references/pdfs`） |
| 标签前缀与取值 | `profile.tags.*` |

## 环境与依赖

- **Python 3，仅标准库**，无第三方依赖，不调用外部命令行程序。
- 检索源：OpenAlex、Crossref、arXiv、Semantic Scholar、Unpaywall 均免密；
  GIIISP 需 token（默认关闭）；引文滚雪球默认开（种子 ≤6、深度 1、最多补 250 条）。

| 环境变量 | 用途 |
|---|---|
| `OPENALEX_API_KEY` | 免除共享出口 IP 的每日额度限制（匿名池易撞 `Insufficient budget`） |
| `GIIISP_AUTH_TOKEN` | 启用中文 / OA 补充源；未设则只跑四源 |
| `ZOTERO_LOCAL_API_KEY` | Zotero 本地 API 写授权的 key（`remember:false`，不入库） |
| `LITPIPE_DIR` | 默认工作区（等价于每次都传 `--dir`） |

Zotero 侧需保持运行。四条通道按能力顺序探测：`local_api`（读写全能力，需在设置里允许本机应用通信）
→ `connector`（免鉴权，但条目会落进 Zotero **当前选中分类**）→ `betterbibtex` → `file_handoff`
（永远可用：把 `bib/recs.ris` 拖进 Zotero 即可，这是合格结果不是失败）。

## 目录结构

```
literature-pipeline/
├── SKILL.md                      技能定义与完整规格（本 README 的权威上游）
├── README.md
├── config/
│   ├── journal_tiers.csv         期刊权威性分级表（194 条，可被校内目录覆盖）
│   └── profile.default.json      全部口径默认值
├── references/
│   ├── 01-query-recipe.md        概念组拆解、中英双语、各源语法差异、滚雪球
│   ├── 02-sources.md             各检索源实测限流行为、降级与礼貌池
│   ├── 03-screen-scoring.md      enrichment 契约、reason codes、双层算分与配额
│   ├── 04-zotero-bridge.md       四通道能力矩阵、写授权与附件挂载、路径修复
│   ├── 05-pdf-reading.md         合法全文路由、逐篇导航字段、三遍法顺序
│   └── 06-evidence-exemplars.md  论断支撑表契约、范本档案七节
└── scripts/
    ├── lit.py                    唯一 CLI 入口（15 个子命令）
    └── litlib/                   common / harvest / dedupe / screen / score / review /
                                  zotero / pdfs / notes / evidence / venue_health
```

## 已知限制

- **中文库不在召回范围**：CNKI / 万方 / WoS / Scopus 需要登录态，`harvest` 不覆盖，属人工扩展路径。
- **订阅制出版商的全文只能手点或少而慢的浏览器代取**（脚本侧会被 webdriver / reCAPTCHA 风控拦住）。
- **判读质量取决于全文能否抽出文字层**：扫描版 / 图片型 PDF 会降级，需 OCR 或人工确认。
- **期刊层次与影响因子区间是参考值**，不构成官方依据；冲突时不挑一个写死，按披露处理。
- 引用数**不跨源比较**：OpenAlex / Crossref / Semantic Scholar 口径与覆盖期不同，
  百分位只在同一 subfield × 同一计数来源内做。

## 许可

未声明许可证。公开可见不等于授权再分发或商用；需要授权请开 issue 联系。
