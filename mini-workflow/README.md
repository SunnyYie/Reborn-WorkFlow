# PCO-only Mini-Workflow

用户已经选定三首歌时的独立可运行入口。只查询 PCO，优先下载中文歌词、否则原始简谱，按保留模板直接生成 DOCX 和 `.sources.json`。不需要 RAG 数据库、音频、主题、歌手配置或人工整份确认。自然语言理解、读取谱面及核验仍由当前 Agent 完成。

## 开始和查询

```sh
MINI="$PWD/mini-workflow/run"
"$MINI" --help
"$MINI" doctor
"$MINI" new
# STATE 使用上一步实际返回路径，续接不要再 new。
STATE="/实际返回的绝对路径/state.json"
"$MINI" --state "$STATE" status
"$MINI" catalog --query "用户歌名"
"$MINI" --state "$STATE" inspect 实际PCO歌曲ID
```

Python 3.9+、XML 和 `lxml` 必需；启动器优先 bundled Python，可用 `MINI_WORKFLOW_PYTHON` 指定。模板位于 `skills/pco-assemble-worship-setlist/assets/reference.docx`，新 checkout 从私人备份恢复，不创建替代模板。

凭据沿用进程环境变量或本机 PCO Keychain。`doctor` 用实际 API 检查访问，不输出凭据。GET 查询以及附件 `/open` 必要动作之外不执行 PCO 写入。只生成本地文件，不上传。

`--state` 必须放在子命令前；所有带输入的命令接受 `--input /绝对路径/输入.json`，默认从 stdin 接收 JSON 对象。退出码 2 表示失败。`status/show` 不联网、不写 state。默认会话在忽略的 `mini-workflow/runs/`，自定义 `new --runs-dir` 时也必须使用私人目录，不能提交。

## 选择三首

`select` 保存**完整选择**，数组次序即歌序；更改时重新提交所有要保留的内容。三个 main 不能重复，回应/结束可以与 main 重复。

```json
{
  "main": [
    {"title": "用户指定第一首"},
    {"title": "用户指定第二首"},
    {"title": "用户指定第三首"}
  ]
}
```

唯一精确标题且唯一现役编曲可直接解析；有候选但没有唯一精确匹配、或多编曲时返回 `needs_review`。用 catalog/inspect 查看后改传 `{"song_id":"实际ID","arrangement_id":"实际ID"}`，IDs 必须先 inspect，不能自行编造。没有候选保存 `not_found` 空块；查询失败不能当作无候选。

可选 `response`、`ending` 使用同样的标题/ID对象，或 `{"main_position":2}` 引用第二首。省略或 null 均表示留空，不会自动补选。调和 BPM 不接受输入且固定为空。

每个歌曲对象可附用户提供的经文：

```json
{
  "reference": "实际章节",
  "translation": "实际译本",
  "quote": "用户要求加入的原文",
  "source": "实际核验来源或用户原文来源",
  "verification": "source_checked",
  "user_message": "用户实际要求加入该经文的话"
}
```

没法独立核验时用 `unverified` 并披露，未给经文不填。顶层可选 `spoken:[{"title":"用户标题","text":"用户原稿","user_message":"实际指令"}]`，只收用户原稿，不要求生成话语。

## 下载与材料

```sh
"$MINI" --state "$STATE" select --input /本次选择.json
"$MINI" --state "$STATE" resources
"$MINI" --state "$STATE" download
```

资源以 `song_id:arrangement_id` 为键，同曲同编曲的回应/结束只下载一次。结果包括 `downloaded/missing/not_found/needs_review/restricted/auth_error/network_error` 等独立状态；认证失败停止后续查询，未检查项保持未知。成功下载会重核来源版本及本地哈希，相同内容不重复传输。中文歌词受限不回退简谱，不下载音视频或英文歌词，不生成空歌词文件。

分类或版本含混时 `review-resources`：

```json
{
  "song_id": "实际ID",
  "arrangement_id": "实际ID",
  "classifications": [
    {"source_id":"实际资源ID","kind":"chinese_lyrics","evidence":"实际分类依据"}
  ],
  "selected_source_id": "实际资源ID",
  "user_message": "实际用户版本选择"
}
```

分类允许 `chinese_lyrics/numbered_score/other`。可只分类不选源，或只选源。不得把已知中文歌词改为 other。选择绑定源指纹，变更须重核；更新 review 会清除该曲旧下载/材料引用，再 download，不删除原件。

读取原件后，`material-prepare`：

```json
{
  "song_id": "实际ID",
  "arrangement_id": "实际ID",
  "source_sha256": "download实际返回的哈希",
  "version_basis": "本次实际编曲和原件身份对应依据",
  "lyric_sections": [
    {"label":"V1","lines":["原件逐行歌词"]},
    {"label":"C","lines":["原件逐行歌词"]}
  ],
  "route": [{"label":"V1"},{"label":"C"}],
  "structure_kind": "proposal",
  "structure_basis": "仅有乐段清单，本次提出的编排建议，不是录音还原",
  "issues": []
}
```

TXT/DOCX 每行歌词须对应原件一行，包含表格内文本；不改词。乐段标签允许 V/C/PC/B/LL/TAG 及编号，器乐 IN/INT/Ins 统一显示 IN。LL/Tag 在文字原件须有独立标记。

完整源顺序用 `source_explicit`，用户给定顺序用 `user_provided`，建议用 `proposal`；未知顺序使用 `structure_kind:"unknown", route:[]`，不推断反复/小节。每个 route 项可有 `current_cue` 和必填的 `cue_basis`，只使用实际源/用户依据的分工、小节或强弱。没有清晰内容可提交部分已核验乐段，未读清处放 issues，不补词。

PDF/扫描图/简谱须附实际核验记录：

```json
{
  "analysis": {
    "verified": true,
    "method": "实际查看和逐处核对方式",
    "note": "实际核验说明；不把识读稿称为PCO原生文字",
    "sections": {"V1":"原件第1页左上区域","C":"原件第1页右上区域"},
    "route": {"0":"原件第1页结构表第1格","1":"原件第1页结构表第2格"}
  }
}
```

`sections` 精确覆盖所有导入乐段。`source_explicit` 的 PDF/图片结构还须 `analysis.route` 精确覆盖零基序号的每次出现；提议/用户顺序不伪造页码证据。不内置 OCR、音频分析或自动谱面理解，没有实际识读能力就留空并报告。

## 导出与收尾

```sh
"$MINI" --state "$STATE" export --output /本会话目录/歌单.docx
"$MINI" --state "$STATE" status
```

无待审/正式双重导出，不要求整份用户确认。必须先执行 download 留下每首实际查询结果，不能未查询就直接生成全空歌单。认证失败后未能检查的歌曲明确记录 not_checked，不当作 missing。同路径、同内容/代码/模板/来源且文件未改时返回 `reused:true`，否则拒绝覆盖已有 DOCX 或 sources，修改后另存明确的新路径。已验收尚未收尾时先 finalize，再生成新文件。

三首各起新页；空经文栏、空结构表不含“待确认”等文字；可用歌词是真实双栏，保持模板字号、配色、间距。回应/结束独立合页；同曲回应引用主歌，新增回应完整展示；结束只放标题与已知结构。用户话语附录另起页。缺失/待核验信息在回复及 sources，而非正文占位。

`export` 返回 warnings、features 及哈希。`status` 区分 `export_valid`、`visual_review_valid`、`finalization_valid`、`content_complete`、`delivery_complete`。资料缺失可以交付空槽位，但 `content_complete=false`；完成文件检查不意味着结构或现场排练已获确认。

在原生 WPS/Word 真实逐页查看后调用 `artifact-finalize`，输入：

```json
{
  "path": "/实际导出.docx",
  "sha256": "实际导出哈希",
  "confirmed": true,
  "user_message": "执行者实际检查观察，不是虚构用户歌单确认",
  "checks": ["layout","colors","content","pagination"],
  "visual": {
    "app": "WPS",
    "page_count": 4,
    "pages": [],
    "songs": [],
    "response_ending": {}
  },
  "cleanup": {
    "user_message": "实际核对待删文件已关闭、无用户修改的观察",
    "checked_files": []
  }
}
```

这是字段示意，不可原样提交空 observations。`visual` 参照现有 [逐页样式检查](../skills/pco-assemble-worship-setlist/references/layout-acceptance.md)，完整记录每页 blocks/checks/note、三首实际 pages/lyrics_pages、所有适用的 v5 字体/字号/颜色/间距/表格/对齐检查，以及独立回应结束页。跨页记录原因和实际调整，不能编造。

mini 唯一的空槽位例外：

- 无歌词的主歌 `lyrics_pages:[]`，从 checks 移除 `heading_baseline/first_lyric_baseline/lyric_spacing/lyrics_pagination/table_lyric_gap/section_spacing`，并用 `not_applicable` 对象为这六项逐一写明实际空白原因。有歌词不能豁免。
- 回应/结束均无表格时，从 response_ending.checks 移除 `table_style/table_dimensions/instrument_notation`，用 not_applicable 对象逐项说明；只要有表格就检查完整项目。主歌即使无结构也有空表，表格检查不豁免。
- 不自动填检查结果，测试模拟不能成为真人/原生验收。无法查看原生分页时保留待验收文件和诊断。

cleanup.checked_files 必须精确覆盖全部待删旧 DOCX/sources 和登记预览，逐条有绝对 path、登记 sha256、实际 `closed:true`。无待删文件用空数组并写实际说明。可以 `use_existing_review:true` 复用同文件哈希的有效原生检查，不重新伪造观察。预检发现仍打开、已修改、缺失、有引用或权限问题即保留；部分删除失败保留审计并返回非零，修复后重试。

`artifact-register` 只接收 `{"path":"/本会话/render-cache/page.png","owner":"/实际导出.docx"}`，预览限定在本会话 render-cache 的 PNG/JPEG/PDF。原始材料和其他文件不可登记为清理预览。完成后仅保留当前 DOCX/sources、原件、会话和核验依据，不批量删目录或猜“旧”文件。

## 回归与证据

```sh
PY="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
"$PY" -B -m unittest discover -s mini-workflow -p 'test_*.py' -q
"$PY" -B -m unittest discover -s workflow -p 'test_*.py' -q
"$PY" -B -m unittest discover -s rag -p 'test_*.py' -q
"$PY" -B -m unittest discover -s scripts -p 'test*.py' -q
```

自动化用合成文字、隔离目录、模拟 PCO 和显式模拟原生观察，不证明真实 PCO 可用或 WPS 看过文件。真实烟测应使用独立新会话、当前真实 PCO 歌曲、原始资料，并另记实际服务及原生检查结果，不能复用模拟确认。

### 2026-11-04 验证记录

- mini 35 项、完整 workflow 154 项、RAG 63 项、仓库策略 3 项全部通过；新增流程没有修改完整 workflow 的 Python 引擎或共享渲染器。
- 真实 PCO 认证、目录、编曲、资源查询和两首中文歌词下载成功（DOCX 附件与歌词字段各一首）；第三首含混附件保留 needs_review，不改断言为缺失或强行回退。
- 真实下载原件导入、Word/sources 导出成功；无 RAG、无 MP3、无定调/现场确认。原生 WPS 实际逐页复查 4 页，含双栏、空调/BPM、空经文、空歌词及空回应/结束分支。
- 对已关闭的旧测试 DOCX/sources 实际执行同次验收清理，原件与当前配对成品保留；status 显示 export_valid、visual_review_valid、finalization_valid 为 true，cleanup_remaining 为空。
- 这只证明技术测试文件可交付，不是活动定稿；content_complete 仍为 false，保留编排建议、未知结构和附件待核验。简谱回退/识读、新增回应、网络及删除失败等分支本次只通过隔离自动化，未做真实 PCO/原生端到端验收。
