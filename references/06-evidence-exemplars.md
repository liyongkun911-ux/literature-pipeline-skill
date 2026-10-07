# Stage 11–12 — 论断支撑表与范本案体例

方法骨架来自 PaperSpine（`WUBING2023/PaperSpine`，MIT）的 `citation-support-bank` /
`journal-learning` / `exemplar-learning-dossier` 三份参考，**改成了本流水线的做法**：
它把规则写在文档里靠模型自觉，这里把可判定的部分交给脚本，凡是"我核查过了""我学过这篇的体例"
这类声称，都必须能对上一个真实存在的产物字段。

产物目录：`runs/<id>/evidence/`。前置：跑到 `select` 之后；要用全文相关能力则先跑 `pdfs fetch`。

## 11. 论断支撑表 `evidence/claims.csv`

```
lit.py evidence claims-template --run <id>   # 按选中集铺 3× 行
# 复制成 evidence/claims.csv 后由模型/用户填写
lit.py evidence claims-check --run <id>      # 脚本审计，产出 claims_audit.csv
```

列：`claim_id, uid, section, claim_sentence, evidence_basis, page_or_section,
source_channel, verified, verification_note, status`

- `section` ∈ Introduction / Related Work / Method / Results / Discussion / Limitations / Background
- `evidence_basis` ∈ `abstract` / `fulltext` / `metadata`
- `claim_sentence` 要写成**能直接进稿子的句子**，不是关键词堆叠，也不能是标题复制
- 铺 3× 行是刻意留冗余：先有得挑，再删到 20 条左右，而不是正好凑 20 条

`claims-check` 的判定（三态：`verified` / `self-attested` / `blocked`）：

| 触发条件 | 结果 |
|---|---|
| uid 不在选中集 / claim_id 重复 / basis 非法 / 句子 <25 字 / 句子就是标题 | **blocked** |
| 句子含测量数值（`91.2%`、`3.1 eV`、`500 次`…）但 basis 不是 `fulltext` | **blocked** |
| basis=`fulltext` 但 `pdfs/` 里没有这篇（查 `07_pdf_status.json`） | **blocked** |
| basis=`fulltext` 但没写 `page_or_section` | **blocked** |
| 该文被标 `is_retracted` | **blocked** |
| `verified=yes` 但 `verification_note` 里没有该篇的 DOI/arXiv/URL | **self-attested**（自证不算已核） |
| `section` 写了非标准章节名 | **self-attested** + 提示 |

另两条整体提醒：已核验论断里近 3 年占比 <60% 时 WARN（除非你的论断本就是奠基作）；
已核验论断 60% 以上挤在同一节时 WARN（说明别的章节还没有支撑句）。

**为什么这么严**：论文里最贵的错误是"某个数字被归给了没读过的那篇文献"。上面每一条拦的都是它。

## 12. 范本档案 `evidence/exemplar_dossier.md`

```
lit.py evidence exemplar-pick --run <id> --venue "<目标期刊全名>" [--n-venue 3] [--n-field 3]
# 模型填写档案各表
lit.py evidence exemplar-check --run <id>
```

`exemplar-pick` 只做可判定的选择部分：

- 只在**有真实全文**（`07_pdf_status.json` = downloaded/cached）且未被标撤稿的选中条目里挑；
- `target-venue` 一组按目标刊精确匹配刊名，`same-field` 一组优先跨子方向，**两组零重复**
  （"三篇重复计数不算六篇范本"）；
- 顺带报出这批范本的 `referenced_works_count` 均值 → 给出**目标参考文献数下限的观察值**。
  这是从真实同刊/同领域论文算出来的，比"一般综述要引 100 篇"这种印象靠谱；拿不到就直说拿不到，
  不编数字。

档案必填七节（缺节直接判失败）：Corpus ／ **Official author guidance** ／ **Observed style in
published papers** ／ Move table ／ Skeletons ／ Anti-patterns ／ Transfer plan。三条硬规则：

1. **官方作者指南与"从已发表论文观察到的体例"必须分开写。** 两者常被混称"期刊要求"，
   但只有前者是指南、后者只是惯例；`exemplar-check` 会看官方指南那节是否真空着。
2. **学的是可复用的写作决定，不是句子。** 原句只能出现在分析笔记里，进了骨架就要抽象成结构。
3. `exemplar-check` 只数 Corpus 之后各节里出现的 uid，并要求 ≥60% 的范本真被引用到、
   表格里没有空行 —— 防止"档案生成了但没人填"。

**没有目标刊就不谈目标刊体例**：`--venue` 缺省时脚本会明说"本次不做任何『符合目标刊格式』的声称"。
自己临时挑的对照刊不能事后被当成目标刊依据。

## 写作与配图不在这里

本技能只负责把"能进稿子的论断 + 有依据的体例观察"准备好。真正下笔、审稿回复、cover letter
交给 `research-copilot:academic-writing`；引用格式合规交给 `research-copilot:papercheck`；
配图交给 `research-copilot:giiisp-scientific-image-generation`。

## PaperSpine 里**没有**采纳的部分

它的 17 步 production protocol、Web 工作区/launcher、`translation_package`、排版交付与
`artifact_check.py` 那套宿主编排，属于另一个产品的骨架，与本技能"脚本做算术、模型做判断"的
分工不一致，不搬。它的"学期刊"依赖用户自己喂进去的目标刊论文；本技能这边范本来自刚刚检索并
下载过全文的那批，来源可追溯，这一点比它强。
