---
name: literature-pipeline
description: 从研究主题一路做到可执行的阅读计划：生成中英文分块检索式、多源检索近五年文献、按 DOI/标题去重、按期刊权威性分级表与研究方向筛选、基础分+成果加分（封顶30）双层打分、输出候选清单等人工确认、生成 RIS/BibTeX 并导入 Zotero、打优先级/主题/方法/状态标签、抓取开放获取全文、产出逐篇阅读导航与三遍法总顺序；最后把候选文献升级成可直接进稿的论断支撑表（每条论断标注证据来源与页节，脚本审计）和目标期刊体例档案。当用户给出一个研究主题并要求查文献、找论文、做文献综述、写开题/相关工作、"把文献整理进 Zotero"、或问"有哪些相关文献我该读"时使用，也可用于"帮我准备能站得住的引用论断""这篇期刊的论文是怎么写的"。也可只跑到某一阶段。写用户文献库前必须先取得明确同意。
argument-hint: <研究主题> [--years 5] [--candidates 20]
---

# Literature Pipeline

## Overview

把"主题 → 检索式 → 多源召回 → 去重 → 筛选 → 评分 → 候选清单 → 人工确认 → Zotero → 全文 → 阅读导航"
做成一条**可复现、可断点续跑**的流水线。确定性算术全部在脚本里，语义判断全部落在文件里，
两边通过 CSV/JSONL 契约交接。因此任何一次排名都能回溯到"哪条检索式、哪个源、哪个维度贡献了多少分"。

## 铁律

1. **不编造文献、期刊分区、影响因子、引用数。** 一切字段必须来自 API 返回或用户提供的目录文件；
   缺就标缺。分区只能来自 `venue_screen.journal_directory`。
2. **写 Zotero 是不可逆操作。** 先 `zotero build` 给用户看产物 → 取得明确同意 → 才允许
   `zotero import --yes`。绝不绕付费墙。
3. **每个剔除都要有 reason code**（R01–R09，见 `references/03-screen-scoring.md`）。
   拿不准的进 `needs_judgement`，不静默删、也不静默留。
4. **召回不足时先修检索式，不在下游打补丁。** 出现 `WARN 总召回 < 阈值` 或某源 0 命中，
   回改 `queries.json` 重跑 `harvest`，不要降低配额硬凑 20 篇。
5. **候选清单不可声称做过某项排除**，如果对应数据源没配（例如没期刊目录却说"已剔除水刊"）。
6. 分项得分先于结论。用户质疑排名时给 `score_breakdown`，不要为排序结果辩护。
7. **引用数不跨源比较。** OpenAlex / Crossref / Semantic Scholar 的口径与覆盖期不同。
   合并后每条只留 `cited_by_counts`（分源）和一个 `cited_by_source` 权威值，
   `dim_impact` 只在 **同一 subfield × 同一计数来源** 内做百分位。跨源比大小 = 假信号。
8. **预算是上限，不是目标。** 5–15 篇强候选胜过 40 篇凑数。追加轮次按 `selection.difficulty`
   （1-3 不追加 / 4-7 一轮 / 8-10 两轮），**每轮必须绑定一个具体缺口**（反复被引的方法、
   点名未覆盖的基准、摘要指向的奠基作、缺失的缩写），换措辞重跑同一意图不算一轮。够就停。
9. **读到才能引。** 归属到某篇的结论至少读过摘要；要引其中**数字**必须读到结果章节，
   并写清出自第几页/哪一节。只有摘要可看时标 `仅摘要`，不要装作读过全文。
   `notes audit` 是这条的**自动检查**（有数字没页码 / 页引越界 / 页引对不上原文），
   判据、±1 页序容差与降级条件见 `references/05-pdf-reading.md`。
10. **撤稿不可逆于信任。** `zotero build` 前对全部 DOI 做一次 OpenAlex 撤稿核查，
    检出即拒绝写库；无 DOI 的条目记"未核查"，绝不当作"已核查干净"。
11. **产物不存在 = 没做过。** "我核查过引用了""我学过目标刊的体例"这类声称必须能指到一个
    填好的产物（`evidence/claims_audit.csv`、`evidence/exemplar_dossier.md`），脚本判 blocked
    的条目不得出现在交付里。同时记住两条最容易混的：**官方作者指南 ≠ 从已发表论文观察到的体例**；
    **能开放获取/能拿到 PDF ≠ 质量高**（本技能的 tier 表同样是未官方核实的映射，别当质量证据用）。
12. **拿不到全文只允许"分类 + 交付"，不允许"绕"。** 全文通道只走出版商/作者/机构自己公开的副本
    （arXiv、OpenAlex 全部 `locations`、Unpaywall 全部 `oa_locations`、Semantic Scholar per-DOI、
    Crossref PDF link、Europe PMC 渲染直链）。付费墙、CDN 反爬墙一律不绕过、不伪装、不找镜像站。
    **用户自己有权访问的入口除外**：机构订阅（本校实测走 CARSI：`ds.carsi.edu.cn` → 选校 → 用户本人
    输入统一身份认证）经 `pdfs browser-queue` → 浏览器逐篇代取（`browser-serve` 本地回传）是合法路径，
    逐篇确认后再做。剩余部分写进 `pdf_unresolved.md`，
    按 `anti_bot_blocked / landing_only / closed_paywall / no_open_copy / too_large / transient_error`
    分类，每篇给可点的合法入口，绝不含糊成"网络问题"。

## 前置

```bash
python <skill>/scripts/lit.py init  --topic "<主题>" --run <id> --years 5 --candidates 20 --mailto <邮箱>
python <skill>/scripts/lit.py probe --run <id>
```

`probe` 只读，会报：五个检索源是否可达、`GIIISP_AUTH_TOKEN` / `OPENALEX_API_KEY` 是否设置、
期刊分级表是否加载、Zotero 四条通道哪条可用。`http.polite_mailto` 已配好真实邮箱，
对 Crossref/Unpaywall 的礼貌池有效；但 OpenAlex 若报 `Insufficient budget`，那是共享 IP
的每日配额，只能靠 `OPENALEX_API_KEY` 或等重置，换邮箱无用。Zotero 需保持运行（本机实测：Connector 与 Better BibTeX 可用，
本地 API 默认未开——要自动建 collection 和回读校验，请让用户在设置里勾选允许本机应用通信）。

所有命令都带 `--run <id>`；`--dir` 指定工作区（默认当前目录，产物在 `runs/<id>/`）。

## 阶段与产物

| # | 命令 | 产物 | 谁做判断 |
|---|---|---|---|
| 0 | 定档 `selection.mode`：scout(5 篇摸方向) / related_work(默认) / systematic(须先写 `prisma_protocol.md`) | 规模 | **模型 + 用户** |
| 1 | 手写 `runs/<id>/queries.json` | 检索式 | **模型**（`references/01-query-recipe.md`） |
| 2 | `harvest` | `01_raw.jsonl`, `00_provenance.json` | 脚本 |
| 3 | `dedupe` | `02_deduped.jsonl`, `02_dedup_log.json` | 脚本 |
| 4 | `enrich-template` → 填 `enrichment.csv` | 方向/相关性/角色/标签 | **模型** |
| 5 | `screen` | `03_screened.jsonl`, `03_drop_log.csv`, `03_unresolved_venues.csv` | 脚本 |
| 6 | `score` → `select` | `04_ranked.jsonl`, `05_candidates.csv/.md` | 脚本 |
| 7 | 用户改 `verdict` → `review-apply` | `06_selected.jsonl`（含标签） | **人** |
| 8 | `pdfs fetch` → `pdfs browser-queue` → 浏览器代取 `browser-serve` → `pdfs retag` | `pdfs/`, `pdf_manifest.csv`, `zotero-find-pdf.md` | 脚本 + 浏览器（用户登录） |
| 9 | `zotero build` → 确认 → `import --yes` → `verify` → `attach --with-tags --yes`（挂 linked_file 附件 + 同步标签 + 回读校验） | `bib/recs.ris`, `recs.bib`, `zotero_state.json`, `zotero_attach_state.json` | 人 + 脚本 |
| 10 | `notes template` → 填 → `notes audit`（页码/凭据自检，见 references/05）→ `notes build`（7 个必填字段没填满就拒绝出稿；中途要总表加 `--force`，出的稿顶部标未完成） | `reading-nav.md`, `reading-matrix.csv`, `reading-order.json`, `08_notes_incomplete.json`, `08_notes_audit.csv` | 模型 + 脚本 |
| 11 | `evidence claims-template` → 填 `claims.csv` → `evidence claims-check` | `evidence/claims_audit.csv`（verified / self-attested / blocked） | 模型写 + **脚本审计** |
| 12 | `evidence exemplar-pick --venue "<目标刊>"` → 填档案 → `evidence exemplar-check` | `evidence/exemplars.csv`, `evidence/exemplar_dossier.md` | 脚本选 + 模型填 |
| — | `report` | `report.md`（检索式、各源命中、去重统计、reason code 分布） | 脚本 |

顺序要点：**先取全文再写库**（步骤 8 早于 9），否则"状态/"标签要改第二遍。

## 三个需要停下来找用户的地方

1. **步骤 1 之后**：把 `queries.json` 的概念组念给用户听（尤其中英术语和上下位词），
   方向偏了后面全白做。用户说"漏了 X"就加组重跑。
2. **步骤 6 之后**：交付 `05_candidates.md`。明确告知可改 `verdict`（keep/drop/maybe）、
   `priority`、`subtopic`、`method`、`notes`，改完跑 `review-apply`。
   有 WARN 时（配额缺口、语义相关性未填、召回降级）必须原样转述，不能只报好消息。
3. **步骤 9 之前**：展示 `bib/recs.ris` 摘要 + `zotero probe` 的通道结论，问是否写入、
   写进哪个 collection。走 connector 时提醒用户先在 Zotero 里选中目标分类。
   用户不想自动写库，就交付 RIS 并说明拖进去即可——这是合格结果，不是失败。

## 期刊权威性（内置分级表 + 校内目录覆盖）

期刊层次是**硬闸门**，这是本校验收口径的要求、不是学术共识：同类工具（OpenScience）
只按研究类型与撤稿筛查，不排除 venue，理由正是 T4/T5 里常藏着最相关的 niche 与早期工作。
所以留了逃生口：`known_low_tier_action: "flag"` 只标记不剔除，或把误判的刊补进分级表。

内置 `config/journal_tiers.csv`：194 条记录，字段
`name, issn_l, alt_names, tier, partition, if_band, domain, note, tier_basis`。
tier 含义 **T1 顶刊 / T2 权威 / T3 良好 / T4 一般 / T5 预警**。默认认可 T1–T4
（T4 只给 0.26 层次分，等于"留个位置但不加分"），明确判为 T5 的按 **R04** 剔除，
预印本单列 PRE 不降格处理。想恢复严格口径就把 T4 从 `venue_screen.accept_tiers` 里删掉，
届时低于认可线的才按 R02 剔除（`known_low_tier_action: "drop"`）。
`tier_basis` 记判定依据（cas_partition_ref = 停运前中科院分区参考 / proceedings_or_series /
high_volume_journal / editorial_judgement），让每条分级可回溯到"凭什么这么判"。

⚠️ **中科院分区表平台及 API 已于 2026-09-30 停运、不再编纂公开排名**（官方通知）。分区列
一律视为停运前最后公开版的参考映射；过渡期可交叉参考新锐分区（xr-scholar.com，免费、
非官方机构），正式口径以本校科研目录为准。同一刊在不同体系会"同刊异区"，冲突时不挑一个写死。

**期刊健康度（screen 时自动体检，缓存在 `03_venue_health.json`）**：按 ISSN/刊名实查 OpenAlex
Sources——`non_journal_carrier`（book series/conference 载体）自动封顶 T4（判据1的自动化）；
`retraction_heavy`（撤稿占比 ≥ `health.retraction_ratio_threshold` 且发文量足够）与 `not_indexed`
（发文量 ≥100 却不在任何索引清单且不在 DOAJ）按 `health.penalties` 从层次分里显式扣减，
扣分与原因写进 `venue_risk_penalty` 并在候选清单披露。OpenAlex 查不到的载体不造信号。
撤稿信号独立于任何分区体系（实测 JIFS 12.8% 撤稿占比与 Retraction Watch 报道吻合）。

- 表里查不到的刊 = 未知，不判死刑：保留、venue 按未知计分、并汇总进 `03_unresolved_venues.csv`。
  正确闭环是**按刊名批量补表再重跑 `screen`**，不是逐条放行或逐条删。
- 换成学校口径：`venue_screen.journal_directory` 指向校内目录 CSV，同名条目**覆盖**内置表。
- 收紧/放宽：`venue_screen.accept_tiers`；想让未收录刊直接剔除则 `unknown_action: "drop"`。
- **这张表是参考映射，不是官方依据。** 收录的刊及其层次可能有偏差或过时；
  正式场合（开题、毕业、报奖）以本校科研目录/当年有效的官方分区文件为准。
  `if_band` 只是区间，**技能不臆造精确影响因子**（无合法免密来源）。

## 评分与选择

仿综合测评的两层结构，总分 0–100：**综合分 = 基础分(相关性×70) + 成果加分(≤30 封顶)**。
加分子项上限：期刊权威性 14 / 影响力 7 / 时效 5 / 可获取 3 / 权威代理 3（合计 32 > 封顶 30，所以封顶真会咬住），溢出丢弃并记 `bonus_capped`。
后果要心里有数：相关性权重从 35 抬到 70 成为压倒性因素，期刊与引用数区分度减半，
且加分撞顶后各强项不再拉开差距（顶部更易并列）。选 20 篇时叠加角色配额（综述 2–4、方法 6–10、基准 2–5、
应用 3–8、争议 0–3）与多样性约束（同第一作者 ≤3、同期刊 ≤4、子方向 ≥4）。
时效用分档梯度（≤1年 1.0 → >10年 0.08，经典封顶 0.25），档位写进 `recency_step` 可解释。
配额不满足就报缺口，**不用低分文献静默填满**（`min_score` 同时约束补位与配额两条路径）。细则与每维算法见 `references/03-screen-scoring.md`。

改口径只改 `runs/<id>/profile.json`，不要在 SKILL.md 或脚本里硬编码。

## 排障

| 现象 | 处理 |
|---|---|
| 某源 429 / 超时 | 正常降级。单跑 `harvest --source openalex`，等 40–60s；`probe` 看现状。arXiv 挂了会自动改走 OpenAlex 的 arXiv 源过滤（产物标 `discovered_via=arxiv_via_openalex`），**不要 fan-out 重试** |
| 候选数少于 target | 先看 `min_score` 是否卡住：够格的就那么多，宁少不凑数，回去放宽 `queries.json` 而不是调低下限 |
| 去重数几乎没变 | DOI 覆盖本来就高；看 `02_dedup_log.json` 的 `title_fuzzy_merges`，可调 `--threshold` |
| `needs_judgement` 爆量 | 期刊目录不全，按上面闭环补目录后重跑 `screen` |
| 候选不足 20 篇 | 池子太小或配额/多样性卡住。放宽 `queries.json` 或补 `seed_works` 滚雪球 |
| Zotero 本地 API 404 | 用 connector 通道（写条目与标签够用），或让用户开启本地 API 再 `zotero probe` |
| 本地 API 写入 401 / authorize 428·429 | 写请求要带 `POST /local/authorize` 换来的 key，authorize 本身要带 `Zotero-Server-ID` 头；一个 key 复用到底、401 才重授权、429 退避 10s（每条都新授权会在批量写时把 authorize 自己打进 429，实测 19 条中 3 条失败）。详见 references/04 |
| 附件在库但点不开（`zotero attach` 回读报 `路径失效`） | 课题目录被移动过，不是挂载失败。按 references/04 的 PATCH 修法重指 `path`，并同步 `07_pdf_status.json` / `pdf_manifest.csv` / `06_selected.jsonl` / `profile.pdf.storage_root`，再跑 `zotero attach` 回读确认 |
| 导入怕重复 | `import` 自带 DOI/标题查重；再跑一次 `verify` 回读比对 |
| PDF 大面积 manual_required | 正常（订阅制出版商）。交付 `zotero-find-pdf.md`，标 `状态/需手动获取`；有机构订阅时走 `pdfs browser-queue` + 浏览器代取（见 references/05） |
| `anti_bot_blocked` 条目 | 索引认定有开放副本、脚本被 CDN 拒。浏览器打开 `pdf_unresolved.md` 里的入口即得；Europe PMC 渲染直链属此类（脚本 403、浏览器可下） |
| `notes build` 报"判读没填满" | 这是拦空壳的闸门，不是故障。按 `08_notes_incomplete.json` 补齐 `reading_notes.jsonl` 的 7 个必填字段再跑；只想中途看总表就加 `--force`（稿顶会标未完成） |
| `notes audit` 报「页引对不上（本文他处也无）」 | 该数字在本篇任何被引页上都找不到，回原文核；若该篇是扫描版/图片型，会同时给 `text_layer_thin`，此时内容级核验已跳过，结论按"仅摘要"对待。仅超 ±1 页的越界不计入问题数（卷首页偏移），只单独列出供复核 |

## 交付什么

跑到哪步就交哪步的产物，并说清"下一步等什么"：
`report.md`（过程可核验）、`05_candidates.md`（清单）、`bib/recs.ris`（可导入）、
`pdf_manifest.csv` + `zotero-find-pdf.md`（全文情况）、`reading-nav.md`（阅读计划）。
降级、缺数据、未填字段都要如实写出——这份清单会被拿去写论文，含糊即害人已见。

## Resources

- `references/01-query-recipe.md` — 概念组拆解、中英双语、各源语法差异、滚雪球、自检清单
- `references/02-sources.md` — 各检索源**实测**限流行为、降级与礼貌池要求
- `references/03-screen-scoring.md` — `enrichment.csv` 契约、reason codes、期刊目录格式、双层算分与封顶、配额
- `references/04-zotero-bridge.md` — 四条通道能力矩阵、本地 API 写授权与附件挂载的实测写法（405/428/401/429 怎么绕）、目录移动后的路径修复、幂等查重、标签 schema、写入操作顺序
- `references/05-pdf-reading.md` — 合法全文路由、Zotero 找 PDF、逐篇导航字段、三遍法顺序
- `references/06-evidence-exemplars.md` — 论断支撑表契约与判定规则、范本档案七节、目标刊体例与官方指南的分界
- `config/journal_tiers.csv` — 内置期刊权威性分级表（参考用，可被校内目录覆盖）
- `config/profile.default.json` — 全部口径的默认值（复制进 run 目录后按课题改）
- `scripts/lit.py` — 唯一入口，子命令：`init probe status report harvest dedupe enrich-template screen score select review-apply zotero pdfs evidence notes`
