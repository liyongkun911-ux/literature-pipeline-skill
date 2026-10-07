# Stage 1 — 检索式配方（queries.json）

脚本不做主题理解，检索式由模型生成后写进 `runs/<id>/queries.json`。**这一步决定召回上限**，
后面所有去重、评分都救不回一个窄掉的检索式。

## 1. 先拆概念组（BLOCK），不要直接写一长串 AND

把主题拆成 2–4 个概念组，每组内是**同义词/近义词/上下位词/缩写/常见拼写变体**，组间才 AND。
组内词数决定召回，组数决定精度。经验：材料/生物类 3 组，方法/算法类 2 组。

```json
{"blocks": [
  {"id":"A","label":"核心对象","en":["solid electrolyte","solid-state electrolyte","LLZO","garnet electrolyte","sulfide electrolyte","Li7La3Zr2O12"],"zh":["固态电解质","石榴石电解质","硫化物电解质"]},
  {"id":"B","label":"界面","en":["interfacial stability","interface resistance","solid electrolyte interphase","SEI","space charge layer"],"zh":["界面稳定性","界面阻抗","空间电荷层"]},
  {"id":"C","label":"改善手段","en":["interface modification","buffer layer","interlayer","artificial SEI","doping","annealing"],"zh":["界面修饰","缓冲层","人工界面层","掺杂"]}
]}
```

要求：
- 每个 en 组 ≥5 个成员，必须含**缩写与全称两种写法**（LLZO / Li7La3Zr2O12 / garnet）。
- 上下位词分开：`solid electrolyte`（上位）会带回大量无关文献，配 `B`/`C` 组收窄。
- zh 组照样填：GIIISP / 中文库要用，且中英术语能暴露你没想到的英文同义词。

## 2. 每条检索式必须标 purpose，一轮宽 + 一轮窄

```json
{"strings": [
  {"id":"Q1","api":"openalex","lang":"en","purpose":"broad-recall","expr":"(solid electrolyte OR LLZO OR sulfide electrolyte) AND (interfacial stability OR interface resistance OR SEI)"},
  {"id":"Q2","api":"openalex","lang":"en","purpose":"precision","expr":"(solid electrolyte) AND (interface modification OR buffer layer OR interlayer) AND (stability)"},
  {"id":"Q3","api":"crossref","lang":"en","purpose":"broad-recall","expr":"solid electrolyte interfacial stability buffer layer modification"},
  {"id":"Q4","api":"arxiv","lang":"en","purpose":"preprint","expr":"abs:\"solid electrolyte\" AND abs:\"interfacial stability\""},
  {"id":"Q5","api":"semanticscholar","lang":"en","purpose":"broad-recall","expr":"solid state electrolyte interphase stability interface modification"}
]}
```

`api` 可以是 `"all"` 或源名（`openalex`/`crossref`/`arxiv`/`semanticscholar`/`giiisp`）或列表。
宽式只放 2 组，窄式放 3 组——两轮结果并集去重，宽式保召回、窄式提精度。

## 3. 各源语法不一样，别共用一条表达式

| 源 | 语法要点 | 坑 |
|---|---|---|
| OpenAlex | `search` 走全文相关度排序，不接受 `+`/裸 `AND` 组合语法，用自然短语或引号词组 | `select` 字段白名单严格（`related_urls` 不合法）；`search` 匿名池易被限流 |
| Crossref | 用 `query.bibliographic` 的自由文本相关度，不认布尔语法，写成**词袋**最好 | `filter=type:journal-article` 会砍掉预印本（本技能已默认加上，需要就改） |
| arXiv | `search_query` 支持 `abs:"..." AND ti:"..."`、字段前缀、`--` 否定 | 短语必须 `%22` 引号包裹；`all:` 太宽 |
| Semantic Scholar | 纯自由文本 `query`，布尔语法会被当词 | `fields=` 参数名不能再出现在字段列表里 |
| GIIISP | POST JSON，`{"titleAndAbs": "..."}` | 需要 `GIIISP_AUTH_TOKEN` |

## 4. 引文滚雪球（补关键词检索的结构性盲区）

纯关键词检索必然漏掉两类：用词不同的奠基作、还没被索引的最新预印本。用种子文献补：

```json
{"seed_works": ["W2741809807", "doi:10.1126/sciadv.1603010"]}
```

给 3–6 个种子（从宽式结果里挑最高被引 + 你最想引的那篇），`harvest` 会自动做一跳
references + cites 扩展，落进 `01_raw.jsonl` 标 `discovered_via=snowball_ref/snowball_cit`。
`must_keep` 列表（DOI 或标题）里的条目会绕过分区/时间窗硬性排除，用于点名经典。

## 5. 自检清单（生成 queries.json 后逐条过）

- [ ] `strings` 覆盖 ≥3 个已启用源，且有 broad + precision 两类
- [ ] 每个概念组 ≥5 成员，含缩写和全称
- [ ] 主题里若存在"方法/材料/场景"三类概念，各自独立成组，不要塞进同一组
- [ ] 时间窗由 profile 控制（脚本注入 `from_publication_date`），**不要在 expr 里写年份**
- [ ] 给了 `seed_works`（或明确说明为何不给）
- [ ] `harvest` 后看 `01_raw.jsonl` 的 `query_ids` 分布：某个源 0 命中或某组完全没贡献，就是检索式写坏了，**回去改 queries.json 再重跑 harvest**，不要在下游打补丁


## 6. 追加轮次：一轮一个具体缺口（不要换措辞重跑）

第一轮跑完先看缺口，再决定要不要第二轮。合法的"一轮"必须说得出补的是哪个洞：

| 缺口类型 | 怎么补 |
|---|---|
| 结果里反复出现、但初始检索没覆盖的方法/材料名 | 单独开一组，只搜它 |
| 某篇摘要点名了却不在池子里的基准/数据集 | 直接搜该名字，走 openalex |
| 缩写只有缩写、没有全称（或反过来） | 缩写单独一条检索式；全称与缩写各跑一次 |
| 引文网络里的奠基作 | 给 `seed_works` 加那篇，跑 citation_chasing（一跳 ref + cit） |
| 某个刊/会反复出现，想要它的专场 | OpenAlex `filter=primary_location.source.id:...` |

难度决定轮次上限：`selection.difficulty` 1–3 只跑初始轮，4–7 允许一轮追加，8–10 允许两轮。
**同一意图换说法重跑不算一轮**，那是噪声，只会把池子灌大。够 5–15 篇强候选就停，
预算是上限不是目标（铁律8）。

## 7. 缩写与术语的硬规则

- **绝不臆造缩写的全称。** 只用用户给的词，和结果里真实出现过的词。
  猜出来的展开会把检索带到隔壁领域，而且看起来很像"查过了"。
- 检索式里既有散文又有缩写时，**同轮里额外跑一条只搜缩写的**。
- 中英术语分开建组（这是本技能比现成科研 agent 多做的一步：OpenScience 全树里
  没有任何双语检索式构造，只有"Language: English (or specify multilingual)"这一条纳入标准）。
  中文组喂 GIIISP/中文库，中英对照本身还会暴露你没想到的英文同义词。

## 8. 各源字段语法速查（写 strings 时按源改，不要共用一条）

| 源 | 精确写法 |
|---|---|
| PubMed | `"CRISPR"[Title] AND 2020:2024[DP]` |
| arXiv | `ti:"..." AND abs:"..." AND cat:cs.LG`（类别前缀提精度最明显） |
| Semantic Scholar | `title:"..." year:2020-2024` |
| OpenAlex | `filter=primary_location.source.id:...`；`from_publication_date` 由脚本注入，别写进 expr |

通配符 `genom*` 可用；`NOT` 少用——各源对否定的处理不一致，容易把想要的也砍掉，
需要排除时优先用「另开一条只搜被排除项、在 enrichment 里标 direction_ok=0」的做法。
