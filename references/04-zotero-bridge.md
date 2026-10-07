# Stage 6 — Zotero 桥接（多通道 + 幂等）

**写用户的文献库是不可逆操作。** 任何写库命令都必须先经用户明确同意，且必须带 `--yes`。

## 四条通道，按能力优先自动选

`lit.py zotero probe --run <id>` 只读探测，结果写 `zotero_channels.json`。

| 通道 | 探测端点 | 能做什么 | 前置 |
|---|---|---|---|
| `local_api` | `GET {base}/api/users/0/collections` | 读+写+建 collection+打标签+挂 linked_file 子附件+回读校验，**能力最全**（写需 authorize，见下节） | Zotero 设置里勾选允许本机应用通信 |
| `connector` | `POST {base}/connector/ping` | 写新条目（含 tags 与 URL 附件），落到 Zotero **当前选中的 collection** | Zotero 运行中，装过 Connector 即可 |
| `betterbibtex` | `POST {base}/better-bibtex/json-rpc` | 导出/citation key，辅助校验 | 装 Better BibTeX |
| `sqlite_readonly` | 复制 `zotero.sqlite` 到临时目录后只读打开 | **只读**，用于导入前查重与导入后核对 | 能读到数据目录 |
| `file_handoff` | — | 生成 `bib/recs.ris`，用户拖进 Zotero | 永远可用 |

本机 2026-09-19 实测（Zotero 9.0.6 运行中）：

- `POST /connector/ping` → 200，prefs 里 `downloadAssociatedFiles=true`、`supportsAttachmentUpload=true`
- `GET /api/users/0/items?format=json&limit=100` → 200，**能读到真实条目与 DOI**（本地 API 路径是 `/api/`）
- `GET /bapi/...` → 404 `No endpoint found`（**旧写法，别用**；早前据此误判"本地 API 未开启"）
- `POST /better-bibtex/json-rpc` → 存活（`search` 方法在新版已移除，返回 -32601 属正常响应）

→ 查重默认走 `local_api`，够用。**写入是否被允许需运行时验证**，不要凭读通了就断言可写；
`import` 走 `local_api` 失败会自动提示改用 `connector`。要自动新建 collection 才需要写权限。

## 本地 API 写授权与子附件挂载（📅 2026-10-06 实测，Zotero 9.0.6）

读通了 ≠ 能写。写操作有一条独立授权链；该端点未文档化，行为随版本可能漂移，
下面每条状态码都是实机踩出来的，重遇到先复现再照抄。`lit.py zotero attach` 已实现本节流程。

### 授权：每条写请求带新鲜 key

1. `GET {base}/api/users/0/items?limit=1` → 从**响应头**取 `Zotero-Server-ID`（GET 不需要 key，写请求需要）。
2. `POST {base}/api/local/authorize`，body `{"appName": "literature-pipeline"}`，头带 `Zotero-Server-ID`。
   - 缺该头 → **428**；成功 → 200 `{"key": "...", "remember": true}`。
   - Zotero 会弹授权框，**timeout 放宽到 180s**（30s 会在用户点框之前就超时）。
     用户点"始终允许"后 `remember=true`，后续 authorize 不再弹框。
3. 写请求（POST/PATCH）头带 `Zotero-API-Key: <第2步的 key>` + `Zotero-API-Version: 3` + `Zotero-Server-ID`。
   - 不带 key 的 PATCH → **401**（即使第 2 步刚授权成功过）。
   - **一个 key 复用到底，撞 401 才重新 authorize**。批量写时"每条请求都新授权"会让 authorize 自己
     撞 **429**（实测 19 条连写有 3 条因此失败）；`zotero.py` 的 `LocalWrite` 就是复用式。
   - 写请求与 authorize 都要对 **429 退避 10s**，重试上限 ≤3，不要刷限流。

### 挂 linked_file 子附件

- `POST {base}/api/users/0/items/<parentKey>/children` → **405，本地 API 不支持 children 写入端点**。
- 正确写法是**数组式** `POST {base}/api/users/0/items`，body 为附件对象数组（实测 200）：

```json
[{"itemType": "attachment", "parentItem": "<父条目key>", "linkMode": "linked_file",
  "path": "D:\\dir\\sub\\file.pdf", "title": "<文件名>", "contentType": "application/pdf", "charset": ""}]
```

- 新附件 key 在响应的 `successful["0"].key`；`failed` 非空即没挂上，不得宣布成功。
- Windows 路径必须**纯反斜杠**（回读实测 `D:\\ResearchPrograms\\...`），混 `/` 会存成坏路径。

### 条目匹配：不要用 q=

- `?q=<DOI 或路径片段>` 命中 0 条——**本地 API 索引不含 DOI/path 字段**。
- 做法：拉全条目（`GET /api/users/0/items?format=json&limit=100`，翻页取完），客户端按可信度顺序比对：
  1. **`extra` 里的 `LitPipe uid:`**（本技能 `to_zotero_item` 导入时写进去的自标识，最可靠）；
  2. `norm_doi` 精确相等；
  3. `archiveID`（库里 preprint 的 `DOI` 是空、标识在 `arXiv:<id>`，只按 DOI 会整批漏掉）；
  4. `norm_title` 且全库唯一。
  归一化太弱会把多篇错配到同一条目（实测三篇全命中同一条）；**四条都不唯一就报缺，不猜**。
- 多值 `itemType` 串（`journalArticle || preprint || ...`）**必须 URL 编码**，
  空格与 `||` 直接拼进 URL 会 `http.client.InvalidURL`。
- 幂等判定以**库内是否已有 `linkMode=linked_file` 的子附件**为准，不要拿 `07_pdf_status.json` 的
  path 去比：浏览器代取的件是出版商文件名，status 记的是 `uid_slug.pdf`，路径会对不上而误判"未挂"。

### 改已有条目的标签

- `PATCH {base}/api/users/<lib>/items/<key>`，body `{"tags": [...]}`，
  头带 key + `If-Unmodified-Since-Version: <当前 version>` → 204。
- 幂等查重命中的条目会被 `import` 跳过，所以标签变更**只能靠这条 PATCH**，重跑 import 无效。
- **同前缀要替换，不能并集**：`状态/`、`优先级/` 是单值维度，把新标签并进旧列表会出现
  `状态/待读` 与 `状态/到手` 共存（实测发生过）。正确做法：删掉库内与本次意图同前缀的标签，
  其余前缀（用户手写的备注等）原样保留。

### 挂载后必须回读校验

`GET {base}/api/users/<lib>/items/<parentKey>/children` → 取附件 `path` → `os.path.isfile` 逐条确认，
按三类计数打印：`可开 / 路径失效 / 未确认`。**路径失效 ≠ 挂载失败**：附件在库里但磁盘文件没了
（课题目录改名或搬走过就是这样，实测一批 19 个全成路径失效），要修的是 status 与库内 path，不是重挂；
`未确认`（库里根本没有这个件）才是没写进去，必须报错退出。不校验等于没挂。

### 课题目录移动后的路径修复（实测 20/20）

`PATCH {base}/api/users/<lib>/items/<附件key>`，body `{"path": "<新绝对路径>"}`，
头带 `If-Unmodified-Since-Version`（**每条先实时取 version**，否则刚改过的会撞 412）→ 204，回读即生效。
四处 run 侧记账要一起改，否则下次判定还会说"未挂"：`07_pdf_status.json` 的 `path`、
`pdf_manifest.csv` 的 `path` 列、`06_selected.jsonl` 的 `pdf_local_path`、`profile.json` 的
`pdf.storage_root`。以库内路径为准回写 status 最稳——status 里可能记着从未存在过的文件名。

## 幂等：二次运行不得重复导入

`import` 前按 `idempotency_key`（DOI → arxiv_id → 规范化标题）查重，来源优先级：
`local_api` 实读 → `sqlite_readonly` 副本 → `zotero_state.json`（本技能上次导入记录）。
命中即跳过并打印条数。写库结果连同 `imported_dois` 追加进 `zotero_state.json`。

导入后跑 `lit.py zotero verify --run <id>` 回读，逐条比对；有 `MISSING` 就报告，不要宣布成功。

## connector 通道的注意点

- 条目落在 Zotero **当前选中 collection**：执行前提醒用户先在左键选中目标分类，否则进顶层库
- 标签写在 item 的 `tags:[{"tag":"优先级/P1","type":1}]` 里，随导入一次成型，不需要二次写权限
- `--collection <name>` 只在 local_api 通道生效（connector 建不了 collection）

## 标签体系（固定 schema，别自由发挥）

```
优先级/P1|P2|P3     由 score 分档（≥78 / ≥65 / 其余），用户在候选表改判优先
主题/<subtopic>     来自 enrichment.subtopic_label
方法/<method>       来自 enrichment.method_label
角色/综述|方法|基准|应用|争议
状态/待读|在读|已读|需手动获取|无全文   默认 待读，pdfs retag 后自动改
备注/<用户笔记>      候选表 notes 列
```

`KW -`（RIS）和 `keywords =`（BibTeX）都承载同一套标签，走 file_handoff 也不会丢标签。

## 执行顺序

```bash
lit.py zotero probe   --run <id>              # 只读
lit.py zotero build   --run <id>              # 只产文件：bib/recs.ris recs.bib zotero_items.json
# 让用户先看 bib/recs.ris，确认无误
lit.py zotero import  --run <id> --channel connector --yes
lit.py zotero verify  --run <id>
lit.py zotero attach  --run <id> --with-tags          # 只读干跑：列出将挂的附件与将改的标签
lit.py zotero attach  --run <id> --with-tags --yes    # 真写：挂 linked_file + 同前缀替换标签 + 回读校验
```

先 `build` 给用户看产物、拿到明确同意，再 `import`。用户不想自动写库时，交付 `recs.ris`
并说明"拖进 Zotero 即可，标签会一起进来"——这是完全合格的结果，不算失败。

`attach` 走 local_api 通道（唯一能改已有条目的通道），干跑不弹授权框；`--yes` 首次会弹，
让用户点"始终允许"。结果连同每行的父条目 key 落 `zotero_attach_state.json`。
