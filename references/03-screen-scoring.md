# Stage 3–4 — 判定契约、去重、白名单、六维评分

分工原则：**模型只做语义判断，脚本做所有算术**。判断结果一律落到 `enrichment.csv`，
不许把结论写在对话里就算数——那样不可复现。

## enrichment.csv（模型填写，screen/score 消费）

`lit.py enrich-template --run <id>` 生成两文件：
- `enrichment.csv` — 空骨架，你填
- `enrichment_context.csv` — uid / title / year / venue / abstract_head，**判方向只看这个**

| 列 | 取值 | 规则 |
|---|---|---|
| `direction_ok` | `yes` / `no` / 留空 | 只有摘要足以判断"不属于目标方向"才写 `no`；拿不准**留空**（留空=不因此剔除）。`no` → R01 剔除 |
| `relevance` | 0–1 | 语义相关度，与词汇匹配按 0.45/0.55 合成。留空则退化为纯词汇，脚本会标 `score_uncertainty` |
| `subtopic_label` | 短语 | 用于 `主题/` 标签 + 子方向多样性统计，控制在 5–8 个类目，别一篇一个 |
| `method_label` | 短语 | 用于 `方法/` 标签 |
| `role` | `survey`/`method`/`benchmark`/`application`/`critique` | 驱动配额，留空则脚本按标题猜（不可靠，会进报告） |
| `venue_hint` | 期刊名 | 一般照抄，仅在源数据明显错时改 |
| `one_line` | 一句 | 为什么入选，会渲染到候选清单 |

## 去重

`dedupe` 按 **规范化 DOI → arXiv 号 → 标题模糊（词二元 Jaccard ≥0.86）** 三级合并，
条目 ID 由 DOI/arXiv/标题哈希生成，所以**重跑和二次导入都指向同一条记录**。
预印本与正式版**配对而非互删**（`version_of`），只有正式版进了筛选池才把预印本记为 R06。

已知边界：纯词形变化（`Interface` ↔ `Interfacial`）相似度约 0.60，不会被模糊合并。
这类同一篇通常 DOI 相同、已在第一级合并掉；确实遇到标题漂移时用
`dedupe --threshold 0.78` 放宽，并回看 `02_dedup_log.json` 的 `events` 确认没误并。

## Reason codes（03_drop_log.csv 每条剔除必有）

| 码 | 含义 | 触发条件 |
|---|---|---|
| R01 | 非目标方向 | `direction_ok=no` |
| R02 | 期刊层次不达标 | 分级表命中且 tier ∉ `accept_tiers`，且 `known_low_tier_action="drop"` |
| R03 | 刊名未收录 | 仅当 `unknown_action="drop"` |
| R04 | 预警/排除名单 | `is_predatory=1` 或在 `reject_venues` |
| R05 | 超时间窗 | 早于窗口且未达经典豁免（引用 ≥1500 或 `must_keep`） |
| R06 | 影子版本 | 预印本的正式版已在池中 → 只留正式版 |
| R07 | 无标识符 | 既无 DOI 也无 arXiv 号，不可核验 |
| R08 | 用户点名剔除 | `queries.json.exclude` |

`03_needs_judgement.jsonl` **不是垃圾桶**：那是"目录里没有这个刊"的条目。脚本会把它们按刊名
聚合到 `03_unresolved_venues.csv`。正确闭环是**批量判定刊名 → 追加期刊目录 → 重跑 screen**，
而不是逐条放行或逐条删。

## 期刊权威性分级（venue 维度的唯一数据来源）

`config/journal_tiers.csv` 已内置 91 刊 / 155 个刊名与别名。匹配优先 ISSN-L，其次规范化刊名
（含 `alt_names`，`|` 分隔）。字段：

```csv
name,issn_l,alt_names,tier,partition,if_band,domain,note
Nature,0028-0836,,T1,1,10+,综合,
某水刊,2977-0000,,T5,,,,is_predatory 等价写法
```

| tier | 含义 | venue 计分 | 默认是否认可 |
|---|---|---|---|
| T1 | 顶刊/领域标杆 | 1.00（IF 区间最高再 +0.10） | 是 |
| T2 | 权威主流 | 0.78 | 是 |
| T3 | 良好 | 0.52 | 是 |
| T4 | 一般 | 0.26 | 是（默认认可但基本不加分；从 `accept_tiers` 删掉 T4 后按 R02 剔除） |
| T5 | 预警/灌水 | 0.00 | 否 → R04 剔除 |
| PRE | 预印本 | 0.48 | 是（不降格，也不等同 T1） |
| 未收录 | 表里没有 | 0.00（默认 `unknown_score=zero`） | **保留**，进 `03_unresolved_venues.csv` 待批量补表 |

`accept_tiers` 改认可范围；`known_low_tier_action` / `unknown_action` 分别控制
"明确判低的刊"与"查不到的刊"是剔除还是仅降分。查不到的刊的层次分由 `unknown_score` 定：
`zero`（默认，0 分——否则"来源没告诉我刊名"会比一张诚实标为 T4 的量刊还占便宜）／
`t4`（0.26）／`neutral`（0.40）。

**校内目录覆盖**：`venue_screen.journal_directory` 指向同格式 CSV，同名条目优先于内置表。
把学校科研目录/中科院分区表导成 `name,issn_l,tier` 三列即可（partition 可留空）。

**这张表的边界**：它是按国内头部高校公开分级习惯整理的**参考映射**，不是官方依据，
收录范围与层次判断都可能偏颇或过时；正式场合以本校当年目录为准。
`if_band` 只有区间——影响因子没有合法免密来源，技能不编造精确数值。

## 综合分：基础分 + 成果加分（封顶 30）

仿照综合测评的两层结构，总分 0–100：

```
综合分 = 基础分 + 成果加分
基础分 = 70 × 相关性(0–1)                  ← 唯一的基础项
成果加分 = min( Σ 各子项, 30 )              ← 封顶，溢出直接丢弃
```

加分子项上限（`scoring.bonus.subcaps`）：
期刊权威性 14 ｜ 影响力 7 ｜ 时效 5 ｜ 可获取 3 ｜ 权威性代理 3   → 合计 32

**上限之和刻意大于封顶 32 > 30**：只有这样 `min(Σ, 30)` 才会真的咬住，
"每项都强"的论文会被削平 6 分，抑制单项通吃。若上限之和恰好等于 30，封顶永远不触发、形同虚设。

| 子项 | 打分依据 | 上限 |
|---|---|---|
| 期刊层次 | 分级表 tier：T1=1.0 T2=0.78 T3=0.52 T4=0.26 PRE=0.48，未收录按 `unknown_score` 取（默认 0）；IF 区间微调最高 +0.10 | 14 |
| 影响力 | 引用数在**同 domain 分组内**取百分位（组内 <8 篇退化为 log10/4） | 7 |
| 时效 | `0.5^(age/2.5)` 半衰减 | 5 |
| 可获取 | 直链 PDF .5 + OA 状态 .2 + 有摘要 .15 + 有 DOI .1 + 多作者 .1 | 3 |
| 权威性代理 | 多源独立命中 + 滚雪球发现 + 作者在本池频次 + 用户点名 | 3 |

相关性 = `0.45×词汇覆盖 + 0.55×语义分(enrichment.relevance)`，再在本次池内取百分位。

**这套结构有两个必然后果，用之前要认：**
1. 相关性权重从 35 抬到 70，**主题对口程度变成压倒性因素**；期刊、引用数的区分度被压到原来的
   约一半，一篇顶刊但只对上一半的文献会明显下滑。
2. 加分封顶会把"好上加好"抹平：加分撞到 30 之后**再强也不加分**，排名差距全部由基础分决定。溢出值记在 `bonus_capped`，`05_candidates.md` 的分项表会显示
   "加分(原始/封顶)"。这是加分制的本意（抑制单项通吃），但意味着**候选表顶部更容易并列**。

优先级分档按新尺度定为 P1 ≥82、P2 ≥62；跑完第一个真实课题后照分数分布再校。
想换成字面上的 `×30%` 算法：`scoring.bonus.apply_bonus_multiplier: true`（总分上限会掉到 79）。

## 选 20 篇：配额 + 多样性

- `role_quota`：综述 2–4、方法 6–10、基准 2–5、应用 3–8、争议 0–3
- `diversity`：同一第一作者 ≤3、同一期刊 ≤4、子方向 ≥4
- 缺口**显式写 WARN 和 report**，不允许用低分文献静默填满名额
- `reserve` 备选 8 篇一起进候选表，用户改判时可就地替换


## 撤稿与原因码 R09

`screen` 见 `is_retracted=true` 直接按 **R09 retracted** 剔除。字段来自 OpenAlex（实测
`select=is_retracted` 与 `filter=is_retracted:true,doi:A|B|...` 均可用，全库约 13.5 万条被标）。
**只有从 OpenAlex 命中的记录带这个标记，"没有标记"不等于"没被撤稿"**——未核查条目状态记为
"未核查"，写库前 `zotero build` 会按 DOI 批量补查一次并落 `bib/retraction_check.json`，
检出即拒绝写库。筛查阶段还应在纳入排除标准里过一遍：案例报告(n<5)、社论、只有摘要无全文、重复条目。

## 规模分档与质量下限

- `selection.mode`：
  - `scout`：只交 5 篇，用于"先看看方向对不对"，不取全文、不写库；
  - `related_work`：默认，交 `candidates` 篇；
  - `systematic`：**先要求 `runs/<id>/prisma_protocol.md` 存在**（研究问题/PEOS、拟检索的库、
    每条检索式全文、纳入排除标准、筛选日志字段），否则 `select` 直接拒绝——没有协议就不做"系统性"声称。
- `selection.min_score`：低于它的条目不填进候选。**交得少不是失败**，凑数才是；
  要更多候选去放宽 `queries.json` 重跑 `harvest`（铁律4），不是调低这里。
- `selection.difficulty` 决定追加轮次上限（1-3→0，4-7→1，8-10→2），见 01-query-recipe.md 第 6 节。

## 引用数怎么用（铁律7 的落地）

`02_deduped.jsonl` 每条带 `cited_by_counts`（分源字典）与 `cited_by_source`（按
openalex > semanticscholar > crossref > giiisp > arxiv 顺序取的那个权威值）。
`dim_impact` 的分组键是 `subfield|计数来源`（见 `04_ranked.jsonl` 的 `impact_group`），
组内 <8 条时退回 `log10(1+c)/4`。整组都没有引用数时该维记 0 并标
`impact_basis=no-citation-signal`——无信号就是无信号，不能让"全 0"在百分位里等于"全满"
（这条是改成按源分组后新引入的坑，已实测 4/82 条走该分支）。
`tie_break` 只准列有 `dim_*` 分项的名字；写错会 WARN 并忽略（曾经默默当 0 参与排序）。

## 与 OpenScience 的两处设计分歧（有意保留）

1. **它反对硬期刊闸门**：它的筛查只看研究类型与是否撤稿，不排除 venue，理由是 T4/T5 里
   常藏着最相关的 niche 与早期工作。本技能仍默认排除，因为用户侧的验收口径是硬约束；
   逃生口是 `known_low_tier_action: "flag"`（只标记不剔除）或把刊补进分级表。
2. **它反对二次施加排序偏好**：认为 API 的默认排序已掺入时效与引用。本技能按用户要求保留
   双层打分（相关性×70 + 成果加分≤30），但吸收了它这条批评里成立的部分——
   引用数不再跨源比较，且召回排序已从"按引用数"改回"按相关度"，避免高被引挤掉小众相关工作。

## 时效分档梯度（现行口径）

`scoring.recency.gradient` 取代了纯半衰期曲线，分档值写进 `04_ranked.jsonl` 的 `recency_step`，
所以"它为什么比邻居少 2 分"能直接回答：≤1年 1.00 ／ ≤2年 0.88 ／ ≤3年 0.72 ／ ≤4年 0.55 ／
≤5年 0.40 ／ ≤7年 0.26 ／ ≤10年 0.15 ／ 更久 0.08；无年份 0.30。
**经典文献（classic）时效分封顶 0.25** —— 否则窗外老论文的词汇匹配会把时效加分吃满。
删掉 `gradient` 键即退回 `half_life_years` 指数曲线。

## enrichment.csv 的身份列（判读不再静默丢失）

`uid` 是内容哈希，后一轮召回把 DOI/arXiv 合并进来后 **uid 会变**。因此模板带
`doi / arxiv_id / title_norm` 三个身份列，查找顺序是 uid → DOI → arXiv → 标题，
screen 会打印"判读兜底沿用 N 条"和"N 条完全没有判读记录"。
迁移旧文件时只补身份列、不动任何判读取值。

## 高风险出版方走 R04，不走 R02

分级表里 `tier=T5` 会被解析成 predatory 标记，按 **R04 flagged-venue** 剔除（区别于
R02 的"层次低于认可线"）。当前表内 T5 包括 Journal of Intelligent & Fuzzy Systems
（累计撤稿 1561 篇）、Innovative Research Thoughts（未入核心）、
Computers & Electrical Engineering（2025 年中科院预警名单）。
**主题对口但刊物在预警名单的论文要单独交代**：可以读，不能当成果依据，
需要保留就走 `must_keep` 并说明理由，不要靠改表格骗过闸门。

## 期刊健康度（2026-09-23 新增，OpenAlex Sources 实查）

背景：中科院分区表平台及 API 2026-09-30 起停运、不再编纂公开排名。分区不再是"每年会刷新的
官方数据"，分级必须叠加**体系无关**的健康度信号。screen 阶段按 ISSN（无 ISSN 回落刊名精确匹配）
实查 OpenAlex Sources，结果缓存 `03_venue_health.json`（重跑 screen 不重复请求，删缓存即强制刷新）。

| flag | 判定 | 后果 |
|---|---|---|
| `non_journal_carrier` | OpenAlex `type` = book series / conference | 载体层次自动封顶 T4（判据1自动化，`tier_capped_by` 留痕）；量刊折扣不叠加 |
| `retraction_heavy` | 撤稿占比 ≥ `health.retraction_ratio_threshold`(0.02) 且发文 ≥ `min_works_for_retraction_signal`(50) | 层次分按 `health.penalties.retraction_heavy`(0.20) 扣减 |
| `not_indexed` | 发文 ≥100、`listed_in` 为空且不在 DOAJ | 层次分按 `health.penalties.not_indexed`(0.10) 扣减（主要影响未收录刊） |

实测校准（2026-09-23）：TPAMI 撤稿 0/12543 → 无标记；IEEE Access 114/112421≈0.1% → 无标记
（量刊折扣另算）；LNCS `type=book series` → 自动封顶 T4；JIFS 1659/12932≈12.8% →
`retraction_heavy`，与 Retraction Watch 报道（累计 1561+）量级吻合。

注意：撤稿计数用 `per-page=1` 取 `meta.count`——OpenAlex **没有** count-only 参数
（`metaonly` 400，实测）。查不到的载体标 `error=source-not-found`，不造信号、不扣分。
