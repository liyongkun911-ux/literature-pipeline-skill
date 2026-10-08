# Stage 7 — 全文获取与阅读导航

## PDF：只走合法通道，拿不到就分类交付

`lit.py pdfs fetch --run <id>` 先试记录自带的定位符，全失败时再按 **DOI** 逐一问五个源
（不按标题猜——实测对一批 IEEE/Elsevier 论文标题匹配 0 命中，且猜错会把别人的 PDF 挂进库；
唯一例外是无 DOI 时对 arXiv 做标题+首作者双闸门回落，见 `arxiv_title`）：

| 路由名 | 来源 | 实测说明 |
|---|---|---|
| `arxiv` | `arxiv_id` → `https://arxiv.org/pdf/<id>` | 最可靠；`export.arxiv.org` 与主站都可用，偶发抖动重试即通 |
| `openalex_oa_url` | 记录里的 `best_oa_location.pdf_url` / `oa_url` | 零额外请求；常拿回 HTML 落地页而非 PDF |
| `unpaywall` | `api.unpaywall.org/v2/<doi>?email=…` 的 `best_oa_location` | 需真实邮箱，占位符 422 |
| `semanticscholar_pdf` | **per-DOI** `graph/v1/paper/DOI:<doi>?fields=openAccessPdf,externalIds` | 2026-09 实测：**search 池 429 时这个端点照样 200**，且能补出记录里没有的 `externalIds.ArXiv`，本批靠它多捞回 3 篇 |
| `openalex_locations` | `/works/doi:<doi>?select=open_access,locations` 的**全部** locations | `best_oa_location` 常常 pdf_url=None，而尾巴上的机构库/HAL 才有副本 |
| `unpaywall_repo` | `oa_locations[]` 里除 best 之外的 `url_for_pdf` | 出版商版没有时，仓库版 `acceptedVersion` 常有 |
| `crossref_pdf` | `api.crossref.org/works/<doi>` 的 `link[]` 中 `content-type=application/pdf` 条目 | 实测 Springer 直链可脚本下载；Elsevier 的 TDM 端点没 key 会 403，如实记录不绕 |
| `europepmc_pdf` | Europe PMC 按 DOI 查 PMC-OA（仅 `isOpenAccess:"Y"`）的渲染直链 | **浏览器专属**：脚本请求被 bot 墙 403，浏览器内同源 fetch 可下；另注意 EBI 偶发 200+空壳 JSON（`{"version":"6.9"}`），代码已加缺 `hitCount` 重试 |
| `arxiv_title` | 无 DOI 时 arXiv `ti:"标题"` 检索 | 双闸门防错配：标题 bigram 相似度 ≥0.8 **且**首作者姓氏在条目作者里；实测《Attention Is All You Need》命中、伪作者被拒 |
| `local_user_file` | 用户自己放进 `runs/<id>/pdfs/` 的文件 | 手动补 |

存储：`pdf.storage_root` 设置后 PDF 统一落在 `<storage_root>/<run-id>/`，manifest 记**绝对路径**。
默认 `"pdfs"` 是**相对当前工作目录**的，即 `<cwd>/pdfs/<run-id>/`；要固定位置就写绝对路径
（课题目录用法：指向 `<课题目录>/references/pdfs`，PDF 随课题归档）；留空则落在 run 目录的
`runs/<id>/pdfs/`。

下载用**流式写盘**（`common.http_stream`）：先验 `%PDF` 魔数、边写边判体积上限
（`pdf.max_mb`，默认 80），任何失败都不留 `.part` 也不留半个 PDF——Windows 上文件句柄没关就
`os.remove` 会抛 `PermissionError`，所以关闭在删除之前。40MB 时代把合法 OA 的
AeroVerse（arXiv 实测 43.9MB）误杀过，故放宽。429/5xx 按 `http.retries` 指数退避重试
（403 等墙类错误**不重试**，当场记录）。已下载文件的来路记在 `pdfs/.routes.json`，
重跑时 `cached` 行仍能看出是 arXiv 版还是出版商版。

### 拿不到 ≠ 一种情况

`pdf_manifest.csv` 的 `pdf_class` + `pdf_unresolved.md` 按下一步动作分：

| 分类 | 含义 | 你的下一步 |
|---|---|---|
| `anti_bot_blocked` | 索引认定有开放副本，本机取直链被拒 | **浏览器直接点开** md 里的入口（出口 IP/UA 层面的拦截，不是内容封闭） |
| `landing_only` | 只解析到落地页 HTML，没有 PDF 直链 | 打开入口在页面上自己点 PDF |
| `closed_paywall` | 订阅制且三个开放索引都没有副本 | 机构订阅 / 馆际互借 / 向作者索取 |
| `no_open_copy` | 有 DOI、三个索引都问过、都没有直链 | 同上，别指望再跑一次会变大 |
| `too_large` / `transient_error` | 超上限 / 网络抖动 | 调 `max_mb` / 直接重跑 |

### 边界（不接受请求扩展）

- **不绕付费墙、不绕反爬墙**：不加镜像站、不伪装浏览器绕 CDN、不试 Sci-Hub/LibGen 一类通道。
  MDPI 那类 gold OA 被 CDN 403 时，换 UA 也照样 403（实测两种 UA 无差别），正确动作是
  交给用户浏览器打开，不是继续找旁路。
- **合法例外是用户自己的权限**：机构订阅由他本人登录态访问没问题。`browser-use` 驱动已登录
  Chrome 逐篇下载可行，但要**逐篇确认**、手动节奏，不做批量抓取（那会超出个人使用范围）。
- 交付物里的链接只有 `doi.org/<doi>` 与索引返回过的 URL，**不凭空拼下载直链**。

```bash
lit.py pdfs fetch --run <id>          # 下载 + 生成 pdf_unresolved.md
lit.py pdfs browser-queue --run <id>  # 把没拿到的打包成 08_browser_queue.json/.md
lit.py pdfs browser-serve --run <id>  # 起本地回传服务器（浏览器代取用，见下节）
lit.py pdfs browser-commit --run <id> # 用户手动下载的文件折回 manifest（08_browser_results.json）
lit.py pdfs retag --run <id>          # 把结果折回标签：状态/待读 vs 状态/需手动获取
lit.py zotero build --run <id>        # 重新生成带新标签的 RIS/Bib/条目
```

顺序很重要：**先 pdfs，再 zotero 写库**，否则标签得二次改。

## 浏览器通道：用户本人权限的合法代取（CARSI）

脚本侧被 bot 墙/订阅墙挡住的条目（`anti_bot_blocked`、`landing_only`、`closed_paywall`），
若用户**本人有权访问**（机构订阅），由 agent 驱动其已登录的浏览器逐篇代取是合法路径：

1. `pdfs browser-queue`：把 `manual_required` 条目连同合法入口（`doi.org` 落地页、索引给出的
   仓库页等）打包成 `08_browser_queue.json` + `.md`。
2. 用户在浏览器里完成登录（本校实测：**CARSI** `ds.carsi.edu.cn` → 选校 → 学校统一身份认证，
   凭据必须用户本人输入；登录一次全浏览器会话共享）。⚠️ 实测坑：
   - 出版商自家 WAYF 名单**查不到本校**（实测：Springer WAYF 列表里没有本校，而 CARSI 目录里有）——
     判断学校能否联邦登录一律查 CARSI 目录，别用 WAYF 下死刑；
   - Springer 页脚显示 `Not affiliated` **是误报**，权限实际生效；判据只有 PDF 直链是否 200。
3. `pdfs browser-serve`：起 `127.0.0.1` 回传服务器（随机 token，只收队列内 uid）。agent 在论文
   页面上下文里执行 `fetch(pdfUrl,{credentials:'include'})` 验魔数后 POST 到本机——
   **大文件不过 agent 上下文**。收到全部 uid 或超时后自动折回 manifest/status。
4. `pdfs retag` 同步标签。到手条目 route 记 `browser`，来路可追溯。

节奏约束不变：**逐篇、人工节奏、用户可随时叫停**；没有订阅权限的条目（`closed_paywall`）
这条路一样拿不到，别反复尝试。


## 交给 Zotero 找 PDF

`lit.py zotero find-pdf --run <id>` 产出勾选清单。走本地 API 通道时可直接给条目挂
URL 附件（`build` 已写入 `attachments`），用户在 Zotero 里点开即下载——
这是当前无需额外权限的最省事路径。

## 逐篇阅读导航

`lit.py notes template` 生成 `reading_notes.jsonl`（每行一篇，10 个字段）。
**内容只能来自摘要或已下载的全文**，不许凭标题编。`notes build` 会检查 7 个必填字段：
只要还有缺字段就**拒绝出稿**（列缺项 + 写 `08_notes_incomplete.json`，不生成 `reading-nav.md`）。
理由写在铁律 11：空壳导航会被当成"读过的结论"引用。要在进度中途看总表就加 `--force`，
它出的稿子顶部会带 `⚠️ 未完成草稿 N/M 篇` 标记，那些篇的结论仍不可引用。

必填：研究问题 / 方法与技术路线 / 主要结论 / 可复用点 / 局限与疑点 / 与本主题的关联 / 精读章节指引。
"精读章节指引"要具体到章节（例：只读 §3 模型与 §5.2 消融，跳过 §4），不要写"全文精读"。

### 怎么把本地 PDF 变成可标页码的原文

内置 Read 工具对部分出版商 PDF 会报 `PDF is password-protected`，而字节级检查显示
**文件里没有 `/Encrypt`**——是解析器误判，不是真加密，别因此放弃全文、更别降级成"仅摘要"。
改用 Git Bash 自带的 poppler：

```bash
/mingw64/bin/pdftotext -enc UTF-8 "<pdf路径>" /tmp/p_<uid>.txt
```

Python 侧用 `open(txt).read().split("\f")` 切页（`\f` 是页边界），从此每条判读都能写成
"（p7 表 3）"这种可回溯页码的表述；再看每页的短行/编号行就能拼出章节页码地图，
"精读章节指引"直接引这张地图。表格里成排的数值也能这样抓，不用截图。

## 总体阅读顺序：三遍法

`notes build` 按 `role` 分遍排序，同遍内按分数降序：

1. **建立全景** — 综述 + 高被引奠基（`classic` 豁免进来的老文献落这里）
2. **主线** — 方法类 → 基准类 → 应用类，按分数排
3. **争议与缺口** — critique，最后读，用来收敛"哪里还没人做"

产物：
- `reading-nav.md` — 顺序总表 + 每篇导航 + 预计用时累加
- `reading-matrix.csv` — 论文 × 维度矩阵，横向对比用
- `reading-order.json` — 机器可读，含 `depends_on` 前置关系

`depends_on` 由模型填（"读这篇之前必须先读那篇"），脚本只在目标也在清单内时保留，
并渲染成"建议先读"。


## 读到才能引（铁律9）

- 摘要可看 ≠ 读过全文。导航里凡把某个**数字**归给某篇，必须写清出自第几页/哪一节
  （`reading-matrix.csv` 的"数字出处(页/节)"列）；只从标题或摘要推断出来的一律不写数字。
- 只有摘要能看时（订阅制出版商）显式标 `仅摘要`，并说明"该文数值未核验"。
- 格式不符不等于访问被拦：请求 PDF 却拿回 HTML 时按"该路由不可用"处理，
  不要把它存成 `paper.pdf`（本技能用 `%PDF` 魔数 + 体积双重校验）。
- 读的是哪个版本要记清：预印本与正式刊版本内容可能不同，导航里注明读的是 arXiv 版还是 DOI 版。
- **`notes audit` 是这条铁律的自动检查**（填完判读、出导航之前跑）：逐字段核「有数字没页码」
  「引用页超出 PDF 页数」「该标仅摘要没标」「页引对不上原文」。判据与容差见下节。

## notes audit 判什么（含容差与降级）

| 报出 | 含义 | 怎么处理 |
|---|---|---|
| `measure_without_page` | 字段里有小数/百分数却没写页码 | 补页码或改成定性表述 |
| `summary_marker_missing` | 没有本地全文的篇目，某字段没标「（仅摘要）」 | 补标记（这是铁律9的硬要求） |
| `page_out_of_range` | 引用页超过该 PDF 页数 +1 | 回原文核；多半是引了不存在的页 |
| `page_text_mismatch` | 该字段页引 ±1 页内找不到这些数字，**本篇其他被引页也找不到** | 最可疑：数字可能不在原文或写错了 |
| `citation_misplaced` | 数字在本篇其他被引页上有，但不在本字段的页引上 | 页引放错位置，挪到正确字段的页引 |
| `text_layer_thin` | 文字层可抽字符过少（扫描版/图片型） | 内容级核验已跳过，该篇结论按"仅摘要"对待 |

设计取舍（都是为了不误报，实测在 27 篇判读上把 37 处噪声收敛到 3 处）：

- **页序容差 ±1**：有些稿的抽文首页是版权页/卷首页，稿内 "Page k of N" 与 `pdfinfo` 页序差 1。
  仅超 ±1 的越界单独列出、不计入问题数（原文可能确有该页）。
- **凭据只取小数与百分数**：中文稿写「表9」而英文原文写 `Table 9`，跨语言对不上，
  所以表/图编号不参与核验；arXiv 号（`2511.01083`）、DOI 前缀（`10.71443`）这类**标识符不是测量值**，一律排除。
- **不做跨字段判断**：「与本主题的关联」按设计会掺入课题自己的方案数字（如"1% 预算"），不是文献事实，不查。
- **没有本地 PDF 不查页码**：那类篇目只查「有没有标仅摘要」。
- 依赖 `pdfinfo`（页数）与 `pdftotext`（文字层，一次抽全文再按 `\f` 切页）；两者缺一时自动降级并打印说明。


## 诚实措辞

覆盖度只能说到检索边界为止：写"在 OpenAlex/Crossref/arXiv/S2 这四个源、这个时间窗内
没有找到 X"，**不要写"没有相关工作"**；失败的检索要记成"未成功"，不能记成"无结果"。
被排掉的知名工作要列出来并给理由（时间窗 / 层次 / 方向），否则读者无法判断闸门是否咬错了东西。
