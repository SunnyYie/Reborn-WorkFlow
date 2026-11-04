# 敬拜定歌与祷告话语 Workflow

这是 **Codex 对话入口 + 可运行状态/PCO/RAG/下载/歌单引擎**，不是独立聊天机器人。先收集主题与偏好，查询 RAG 及真实 PCO，确定三首和顺序；再定调、试听、交付资源；然后拟定祷告话语，最后补经文、确认回应/结束并组装完整 Word。自然语言推荐和话语由 Codex 完成，不内置独立模型。Word 导出需要 `python-docx`，默认优先使用本机 bundled Python；MP3 校验用现有 `ffprobe`，不移调、不转码。

## RAG 与混合资料

所有命令继续使用同一个 `--state`。先用 `rag-search "歌名"` 查看本地精确/别名命中、多版本或缺失。PCO `catalog/inspect` 仍用于核验身份和选择现有调，不意味着命中歌曲需要重下歌词。

歌单明确确认后，为命中歌曲执行 `material-prepare`：

```json
{"song_id": "真实PCO歌曲ID", "id": "选定RAG完整main记录ID", "version_basis": "说明实际版本对应依据及差异"}
```

会话在 `materials/songs.sqlite3` 建独立数据库副本，绑定当前歌单指纹与原件哈希；不改全局资料库。resources/download 遇到有效绑定时返回 `reused`，不查询或下载该首文档，但仍处理定调确认后的 MP3。其他歌曲正常走 PCO。数据库可用全局选项 `--rag-db` 指定，通常不需要修改。

PCO 下载完成后，缺失 RAG 的原始中文 TXT/DOCX 可通过 `material-prepare` 提交 `song_id`、`version_basis`、`structure_basis`、`lyric_sections` 与 `route`。每条歌词必须出现在已下载且哈希相符的原件中。结构需说明是源文明确标注还是现场提案，不冒充录音还原；LL/Tag 需要原文独立标记。

```json
{
  "song_id": "真实PCO歌曲ID",
  "version_basis": "当前已确认编曲的中文歌词原件",
  "structure_basis": "原文标 Verse/Chorus；以下演唱顺序为待确认提案",
  "lyric_sections": [{"label": "V", "lines": ["填写原文实际歌词"]}, {"label": "C", "lines": ["填写原文实际歌词"]}],
  "route": [{"label": "IN"}, {"label": "V"}, {"label": "C"}]
}
```

简谱/PDF 不自动解析；现在可导入经核验的分析，详见下文“已核验简谱”。不能声称全自动音乐分析已经实现。纯本地、不需要 PCO 定调的快速复用仍可直接使用 `rag/run assemble`。

## 开始使用

在 Codex 中说：

> 先读取 `/Users/sunyi/ai/Agent/Reborn-WorkFlow/workflow/SKILL.md`，启动三首歌选歌流程。

也可以在本目录运行以下命令。`run` 自动选取 Python 3.9+ 并检查 XML 运行时；可用 `WORKFLOW_PYTHON` 指定解释器。当前机器的 Homebrew Python 3.14 XML 模块有动态库问题，启动器会回退到系统 Python，不修改全局环境。

```sh
./run new
./run doctor
./run --state /absolute/path/to/state.json show
./run --state /absolute/path/to/state.json status
./run --state /absolute/path/to/state.json spoken-context
./run catalog
./run catalog --query "让我"
./run --state /absolute/path/to/state.json inspect 26911580
```

示例 ID 仅演示命令；每次用实际查询结果确认，不能视作固定歌单。`doctor` 使用现有 `pco me` 验证 CLI；API 使用 `PCO_CLIENT_ID` + `PCO_SECRET` 或 `pco-cli` macOS Keychain（不读取 CLI YAML 配置）。不要在命令参数、JSON 或聊天中放凭据。环境权限不足时申请批准，不重新索要 Secret。

会话默认保存在本目录的 `runs/<session-id>/state.json`；`new --runs-dir <directory>` 可改位置。所有状态命令把 `--state` 放在子命令前面。修改命令通过 `--input <JSON文件>` 或标准输入接收 JSON；每个命令向标准输出返回 JSON，校验错误退出码为 2。不要把含完整歌词的 `inspect` 输出重定向为持久日志。

`status`是只读当前摘要，不修改state，不联网。它核验各阶段的当前确认、成品/来源哈希、视觉记录，列出`cleanup_remaining`。新正式导出`required_cleanup_version=1`，`delivery_complete`还必须有`artifact-finalize`完成记录及无待清理产物；只有验收或手动清理不算完成。`finalization_required/valid`及`next_action`明确缺口；测试稿不会成为正式完成。旧记录保留，不自动升级；新导出required_review_version=5，旧版验收不能使其交付完成或成为清理替代稿。旧报告只作历史，不能覆盖此摘要。`spoken-context`只读已绑定资料，逐首列出`route_index`、来源小节、材料指纹；不会自动选版本或推断现场BPM。

多版本`rag-search`现在返回路径、歌词乐段及各次器乐小节；唯一无疑点候选可给`recommended_id`，仍保持`choose_version`，不绕过用户版本选择。

### 间奏窗口

写稿前先`spoken-context`，再将以下字段并入相应`spoken-requirements.slots[]`（索引、指纹、数值和依据必须来自本次实际资料，不照抄示例）：

```json
{
  "id": "song3_interlude", "song_position": 3, "placement": "interlude",
  "kind": "words", "scripture_required": false, "target_seconds": null,
  "route_index": 7, "material_fingerprint": "实际材料指纹",
  "window": {
    "bars": 8, "beats_per_bar": 4, "bpm": 70, "reserve_seconds": 5,
    "tempo_basis": "reference_only", "basis": "实际来源及BPM拍点核对依据"
  }
}
```

窗口上限为`bars * beats_per_bar * 60 / bpm - reserve_seconds`，稿长还受`target_seconds`约束。拍点需明确，不能把6/8分子直接当每小节BPM拍数。参考窗口仅是保守数学估计，不代表现场排练或标题BPM。新要求遇到资料中已有小节，提案/确认/口述导出会强制核对位置与材料指纹；历史小节覆盖另需`bar_override_basis`。速度确实未知时省略`window`，提供`timing_unknown_reason`并逐字加入对应cue的`unknowns`，确认须接受已披露未知，不能填造参数。历史已确认稿仍可读取，不追溯补造这些字段。

## 第一段：定歌与定调

### 收集主题和偏好

`requirements` 合并已收集的信息；每次更新都会使此前方案和确认失效。主题或快慢缺失时继续收集，偏好未问时阶段为 `collect_preferences`，显式提供空列表表示用户没有偏好。

```json
{
  "theme": "未认识神的迷茫，认识神带来改变，最后坚定跟随",
  "stages": ["寻求", "生命更新", "委身"],
  "slots": ["fast", "slow", "slow"],
  "mood": "柔和、真诚",
  "preferences": [
    {"title": "用户倾向的歌曲", "mode": "preferred"}
  ],
  "notes": "用户明确的其他要求"
}
```

`slots` 必须恰好三项，值为 `fast`、`slow`、`any`。偏好模式为 `preferred` 或 `required`，默认前者；必选曲提交方案前必须补充已核验 `song_id`。不要求填写可选的 stages、mood、notes。

### 查询与提交方案

`catalog` 读取非隐藏歌曲目录并处理分页；`--query` 是 PCO 标题查询，不是语义搜索。`inspect <song-id>...` 读取现役编曲并把元数据与歌词哈希存入会话，歌词全文只返回供助手本轮分析。失败项会单列，不能将失败当作无结果。

`propose` 输入：

```json
{
  "recommended_id": "A",
  "proposals": [
    {
      "id": "A",
      "label": "方案名称",
      "direction": "与其他方案的实质差异",
      "order_reason": "完整三首歌的主题推进和排序理由",
      "tradeoffs": "偏好保留、主题覆盖与音乐条件的取舍",
      "songs": []
    }
  ]
}
```

上面的 `songs` 为结构说明，必须填入恰好三首才可执行。每首需要：

| 字段 | 内容 |
| --- | --- |
| `song_id`, `arrangement_id` | 已经 `inspect` 的真实 ID 字符串 |
| `reason` | 选这首歌的理由 |
| `position_reason` | 为什么在此位置 |
| `theme_evidence` | 歌词主题概括与来源说明，非完整歌词 |
| `evidence_kind` | `current_lyrics` / `historical_source` / `known_version` / `title_only` |
| `tempo` | 助手核验后的 `fast` / `slow` / `unknown` |
| `tempo_basis` | 对实际 BPM、拍号和版本的判断依据 |
| `unknowns` | 尚待核实项数组，无则 `[]` |

代码自动附加真实标题、BPM、拍号与来源指纹，不接受未检查的歌曲/编曲或同一方案重复歌曲。方案数量为 1–3；内容质量与实质差异仍须由 Codex 按 skills 判断。`title_only` 只支持保留待核验记录，不能被当作已验证的匹配结论。未知项必须向用户披露。

### 反馈与确认

用户要求修改时，`feedback` 接收 `{"message": "真实的具体优化反馈"}`，保存旧方案历史并取消旧确认。只说“不满意”时，先追问具体优化方向，再写入反馈、更新需求和重新推荐。

`confirm` 只能在当前方案待确认时调用：

```json
{
  "proposal_id": "A",
  "revision": 3,
  "confirmed": true,
  "user_message": "用户实际明确确认的话",
  "accept_unknowns": true
}
```

`revision` 必须来自当前 `show`/`propose` 结果，示例数字不可照抄。`accept_unknowns` 只有在已披露未知且用户实际接受后才填写 true。程序校验版本和有序三首歌的指纹；它信任 Codex 忠实记录人工意见，不能自行验证自然语言确认是否真实发生。

### 歌手配置与现有调

歌单 `confirm` 后顶层阶段为 `collect_vocals`，尚不可正式下载。使用同一个 state 收集歌手低/中/高音与男女人数：

```sh
./run --state /absolute/path/to/state.json key-requirements --input singers.json
./run --state /absolute/path/to/state.json key-options
./run --state /absolute/path/to/state.json key-propose --input key-proposals.json
./run --state /absolute/path/to/state.json key-preview --input audition.json
./run --state /absolute/path/to/state.json key-confirm --input key-confirmation.json
```

`key-requirements` 输入示例：

```json
{
  "male_count": 1,
  "female_count": 2,
  "profiles": [
    {"id": "male-mid", "gender": "male", "register": "middle", "count": 1},
    {"id": "female-high", "gender": "female", "register": "high", "count": 2}
  ],
  "notes": "已知的每首主领安排或其他限制"
}
```

`register` 为 `low/middle/high`，分组人数必须等于男女总人数且总数非零。可选 `comfortable_low`、`comfortable_high` 记录用户实际提供的舒适音域；不知道时留空。不通过性别和调名套公式确定舒适音域。

`key-options` 只读已定编曲的现有 Key。读取单个 Key 的详情，再跟随其中的 attachments 链接查 MP3；Key 列表省略附件链接不等于没有音频。结果保存至 `key_catalog`，包含 Key ID/名称、大/小调、尾调及该 Key 下 MP3 ID、文件名和下载限制。只允许选择 Key 专属现成 MP3，不创建调、不按文件名猜关联。查询失败与确实没有附件分开报告。

`key-propose` 输入为 `{"recommended_id": "K1", "proposals": [...]}`，1–3 套且不重复，通常一套足够。每套需 `id`、整体 `reason`、`songs` 三项，songs 必须保持已定歌曲顺序。每首字段：

| 字段 | 内容 |
| --- | --- |
| `song_id` | 已定真实歌曲 ID |
| `key_id` | key-options 查询到的该编曲现有 Key ID |
| `attachment_id` | 此 Key 下可下载的 MP3 ID |
| `lead_groups` | singers profiles 中建议主领组 ID 数组 |
| `reason`, `range_evidence` | 选调原因、旋律/舒适音域实际证据或未核验说明 |
| `unknowns` | 未核实项数组，无则 `[]` |

引擎自动附加真实调名和来源指纹；不会根据低中高标签计算音乐推荐，判断仍由 Codex 结合证据完成。调名不等于旋律音域，分类和人数只能辅助推荐，必须试听，建议试唱。

### 试听与定调确认

`key-preview` 输入 `{"proposal_id": "K1"}`，临时读取三首对应 MP3 到会话 `previews/key-r<revision>/`，不下载歌词，不正式归档。返回绝对本地路径，供 Codex 展示音频。`prepared`/解码成功不是人已经听过；代码不会自动把 listened 设为 true。获取试听文件是为播放而必要的临时读取，与确认后的正式交付区分。

`key-confirm` 必须在用户听完当前三首并认可后调用：

```json
{
  "proposal_id": "K1",
  "revision": 2,
  "confirmed": true,
  "listened": true,
  "user_message": "用户实际表示三首已听完且调合适的话",
  "accept_unknowns": true
}
```

revision 来自 `keys.revision`，不是定歌或话语 revision。有音域未知项时先披露并取得接受，再设 accept_unknowns。程序信任 Codex 忠实记录反馈，不能验证人是否真的听过；不得伪造 user_message 或在文件准备好后自动确认。

`key-feedback` 接收 `{"message": "第二首偏高，请换低一些的已有调"}`，撤销定调确认，保留定歌。修改歌手配置同样需重新推荐/试听；不静默沿用旧调。`keys.stage` 为 `ready_to_propose`、`awaiting_audition` 或 `confirmed`；顶层试听阶段为 `awaiting_key_audition`，确认后为 `keys_confirmed`。

### 统一下载与资源核验

```sh
./run --state /absolute/path/to/state.json resources
./run --state /absolute/path/to/state.json download
./run --state /absolute/path/to/state.json download --output /absolute/download/directory
```

`resources` 和正式 `download` 必须在定调试听确认后执行；只有定歌确认会被拒绝。resources 读取现役编曲、歌曲与编曲附件及 Key 详情提供的附件链接，读取分页，不触发 `/open`。输出来源 ID、Key ID、元数据、分类和指纹，不输出签名 URL 或全文。

优先当前编曲中文歌词字段，其次可确定的中文歌词附件；中文歌词存在但受限时报告受限，不静默降级。仅在没有中文歌词、也没有待分类文档时选择简谱。其他编曲或多个合理文件必须选择，不猜版本。只检查现役来源；缺失结果不包含归档版本，归档内容不自动下载。

资源含混时，`review-resources` 输入示例：

```json
{
  "song_id": "已确认歌曲的实际ID",
  "classifications": [
    {
      "source_id": "attachment:实际附件ID",
      "kind": "numbered_score",
      "evidence": "用户确认该附件是要使用的原始简谱"
    }
  ],
  "selected_source_id": "attachment:实际附件ID",
  "user_message": "用户实际选择该版本的话"
}
```

分类值为 `chinese_lyrics`、`numbered_score`、`other`。可只分类不选定；多文件/其他编曲选择必须有 `user_message`。分类和选择绑定当前元数据指纹，文件变化后需重新核验。不得误标中文歌词来绕过优先级。

一次 download 处理文档与已定调 MP3。`results[song_id]` 的原文档 `status/path/kind` 保持兼容，音频放在其 `audio` 对象内，含独立 status/path、Key ID、调名和 MP3 ID。没有文档不建文档占位文件，但仍交付已批准 MP3。已明确属于其他 Key 的简谱不会代替所选调；原始未标调的简谱可交付，但 `score_key_status: unverified` 必须说明，不能声称和 MP3 同调。

下载动作只允许 PCO 附件 `/open` POST；其他 API 写入被拒绝。签名文件请求无 Authorization，重定向仅允许公共 HTTPS。30 MiB 文件上限；保存前校验 PDF、DOCX、DOC、TXT、PNG/JPEG 格式，MP3 以 ffprobe 解码验证（不检测实际调性，调性依赖 Key 关联和用户试听）。不移调、不合成音频。TXT/DOCX 中文歌词另检查中文文本；PDF/图片不做 OCR 或转录。

默认文档和正式 MP3 都在会话 `downloads/`；不覆盖已有不同文件。MP3 尽量从试听缓存复制，不重复调用 /open；正式文件哈希必须与用户试听确认时一致，来源或内容变化则报告需重新试听。每项完成即保存，成功文件有效时不重复下载；缺失与失败可再检查。`completed` 表示三首文档均为成功、RAG 复用或缺失，且三首 MP3 都交付成功，不表示文档全有；其余为 `partial`。

需求/方案变化、明确反馈或已检查元数据变化会撤销确认；下载前还会核验选定编曲是否归档以及歌词/版本是否改变。需要重新推荐确认时不要绕过保护。

## 第二段：祷告与话语

第一段完成定歌、定调试听确认与资源准备后，用同一个 `--state` 进入第二段。顶层 stage 表示第一段总体进度，定歌与定调分别有 confirmation 和 keys.confirmation；第二段使用独立 spoken 状态。资源缺失需披露，不能以缺失当作已验证歌词证据。

```sh
./run --state /absolute/path/to/state.json spoken-requirements --input spoken-requirements.json
./run --state /absolute/path/to/state.json spoken-propose --input spoken-draft.json
./run --state /absolute/path/to/state.json spoken-feedback --input spoken-feedback.json
./run --state /absolute/path/to/state.json spoken-confirm --input spoken-confirmation.json
./run --state /absolute/path/to/state.json spoken-export
```

所有输入也支持标准输入，不需要固定文件名。`spoken-requirements` 接收 `{}` 可启用当前默认安排：

| 位置 ID | 位置 | 形式 | 默认经文要求 |
| --- | --- | --- | --- |
| `song1_interlude` | 第一首间奏 | 祷告 | 可选 |
| `song2_before_intro` | 第二首前奏开始前 | 话语 | 必须 |
| `song3_before_intro` | 第三首前奏开始前 | 话语 | 必须 |
| `song3_interlude` | 第三首间奏 | 话语 | 可选 |

默认 `tone` 为“温柔、真诚、适合口述”，`audience` 为“会众”，`bible_translation` 为“和合本”。`bridge_previous: true` 要求前奏前内容承接上一首；`avoid_title_announcements: true` 禁止正文末段用歌名报幕。这些是可见、可修改的本次要求，Codex 应按用户意见更新，不能静默关闭约束。

可提交 `notes`、`tone`、`audience`、`bible_translation` 等字段局部修改要求。需要不同位置时提交完整 `slots` 列表，每项包含 `id`、`song_position`（1–3）、`placement`（`interlude` / `before_intro`）、`kind`（`prayer` / `words`）、`scripture_required`（布尔）和 `target_seconds`（正数或 null）。未知时长保留 null，不虚构真实乐段长度。

`spoken-propose` 接收 `{"cues": [...]}`，每个请求的位置恰好一项：

| 字段 | 内容 |
| --- | --- |
| `slot_id` | 对应 requirements 的位置 ID |
| `opening`, `body`, `closing` | 实际口述正文三部分，均非空，代码合并成 `text` |
| `previous_connection` | 前奏前话语如何承接上一首，要求承接且存在上一首时必填；这是说明，不直接导出为正文 |
| `song_connection` | 与本处目标歌曲的内容关联说明，不能只写歌名 |
| `delivery_note` | 留白、回到演唱或示意起前奏的提示 |
| `estimated_seconds` | 正数口述估计；已指定窗口时不得超过窗口，不代表已通过现场排练 |
| `scriptures` | 经文数组，无经文位置为 `[]` |
| `unknowns` | 歌词依据、乐段时间等未核实项，无则 `[]` |

代码从已确认歌序派生当前歌曲和上一首信息，自动按位置顺序整理；前奏前的“当前歌曲”就是将要进入的歌曲。首歌间奏仍回应首歌，第三首间奏仍回应第三首，均不是新增歌曲的前奏。

经文每项结构如下。这里只示范记录方式，不把示例来源声明当作真实核验：

```json
{
  "reference": "诗篇51:10",
  "translation": "和合本",
  "mode": "quotation",
  "quote": "神啊，求你为我造清洁的心，使我里面重新有正直的灵。",
  "source": "填写实际核对的原文来源，而非未查看的链接",
  "verification": "unverified"
}
```

`mode` 为 `quotation`、`paraphrase` 或 `reference_only`。逐字引用的 `quote` 必须原样出现在实际正文中；后两种不能填写 `quote`，避免意译冒充原文。`translation` 与本次选择保持一致。

`verification` 为 `source_checked`（确实查阅并核对可靠来源）、`user_provided`（与用户实际提供原文核对）或 `unverified`（待核对）。所有经文引用未核对时均不能定稿；不能靠 `accept_unknowns` 绕过。程序校验结构及引文是否一致，并不包含圣经数据库或自动核验网页；语境、原文和来源真实性仍由 Codex 核验，不得伪造标记。

`spoken-feedback` 接收 `{"message": "具体要修改的话语意见"}`。先询问清楚再保存，不将用户的局部反馈当作整稿确认。重新生成或修改要求都会取消话语确认，但不会取消歌单确认或清空下载结果。

`spoken-confirm` 输入如下，revision 来自 `spoken.revision`，不是第一段 revision：

```json
{
  "revision": 2,
  "confirmed": true,
  "user_message": "用户实际明确确认整份话语稿的话",
  "accept_unknowns": true
}
```

有其他未知项时须披露并实际获得接受，再填写 `accept_unknowns: true`。没有未知项可省略。定歌确认不等于话语确认。

`spoken-export` 只导出当前已确认的现场稿，默认保存于会话目录的 `spoken-script.md`，可用 `--output-dir` 改目录。导出保留位置标签、口述正文、经文出处和核验记录、估计时长与现场提示；不覆盖已有不同内容的文件。歌名可出现在位置标签，正文结尾则以含义衔接，不报下一首歌名。代码能拦截常见报幕形式，但不能证明自然语言一定关联正确，Codex 仍需审阅。

换歌、换序、改调或歌手配置变化时，旧话语自动变为 stale，保留历史但不允许导出；需按新第一段状态重新确认。旧 schema 1 会话可保留已定歌单和已有文件，但新增定调不可绕过：即使原先 stage 为 completed，也需补齐歌手信息、试听确认，才能继续正式下载或进入话语阶段。

## 第三段：完整歌单

定歌、定调和话语均确认、三首资料已准备后：

```sh
./run --state /absolute/path/to/state.json setlist-propose --input assembly.json
./run --state /absolute/path/to/state.json setlist-export --draft --output /absolute/path/review.docx
./run --state /absolute/path/to/state.json setlist-confirm --input approval.json
./run --state /absolute/path/to/state.json setlist-export --output /absolute/path/final.docx
```

`assembly.json`：

```json
{
  "main": [
    {"song_id": "第一首实际ID", "scripture": {"reference": "实际章节", "translation": "和合本", "quote": "实际核对原文", "source": "实际查阅来源", "verification": "source_checked"}},
    {"song_id": "第二首实际ID", "scripture": {"reference": "实际章节", "translation": "和合本", "quote": "实际核对原文", "source": "实际查阅来源", "verification": "source_checked"}},
    {"song_id": "第三首实际ID", "scripture": {"reference": "实际章节", "translation": "和合本", "quote": "实际核对原文", "source": "实际查阅来源", "verification": "source_checked"}}
  ],
  "response": {"song_id": "选用三首之一的实际ID"},
  "ending": {"song_id": "选用三首之一的实际ID"}
}
```

main 顺序必须匹配已确认歌序；调直接来自试听确认的 Key，不接收外部覆盖。BPM 默认空；可选填写 musician 实际提供的 `bpm` 与 `bpm_basis`，不会自动沿用 PCO/历史 BPM。经文必须有原文、译本、实际来源与核验记录；`user_provided` 也可，但不能伪造。代码验证记录，不替代助手的实际查阅。

新增回应不在三首内时，先按下文“回应诗歌独立定调”准备其资料与音频，再提供 `song_id`、`scripture`；也可用 `main_id` 指定会话库内完整版本，但仍需真实对应的 PCO 歌曲 ID 和独立试听交付。新增结束可用 `main_id` 选择会话数据库中的完整 RAG 记录。结束先查 ending，检查歌词段落类型覆盖；无合格存量结束表时只在会话副本保存派生草案。多版本须在 ending 项提供明确 `id`；有 Bridge 不漏，没有 LL/Tag 不添加。

审批输入为 `{"revision": 当前setlist.revision, "confirmed": true, "user_message": "实际用户明确确认"}`。正式导出要求独立歌单确认；草稿用 `--draft`，不冒充确认。歌单、调、话语、材料或结束记录变化后，旧文档确认不能继续导出。导出 Word 包含三首标题/调/空 BPM、经文、彩色结构与同色歌词、四处话语、回应、结束及来源清单；之后仍应通过 WPS 检查排版。

`spoken-requirements` 保持原行为；完整歌单接口现在另检查三首正式 MP3 均已交付、文件哈希匹配试听确认、Key/附件/来源指纹一致，缺失或失败不得组装。不能只改顶层 stage 或填写虚假结果。纯本地 RAG 快速组装不新增音频要求。

### 已核验简谱

下载阶段仍只取原件。`material-prepare` 保留原字段，增加 `analysis`，明确区分识别稿与原生文字：

```json
{
  "source_sha256": "下载原件的完整SHA256",
  "confirmed": true,
  "user_message": "实际核验并确认结构的反馈，不得模拟",
  "unknowns": [],
  "evidence": [
    {"target": "lyrics:V1", "page": 1},
    {"target": "route:0", "page": 1},
    {"target": "route:1", "start_seconds": 12, "end_seconds": 30,
     "audio": {"path": "实际音频绝对路径", "sha256": "实际音频SHA256"}}
  ]
}
```

每个歌词段用 `lyrics:实际标签` 引用，每次结构出现用从零开始的 `route:序号` 引用；所有位置均需页码或经哈希校验的音频时间范围。小节数、独立 LL/Tag 同样需实际依据，不得从常见格式猜。未解决疑点、未确认、原件或音频变化会被拒绝。代码只导入已核验结果，不运行 OCR、ASR 或自动理解录音；不能把该接口宣称为全自动分析器。

### 回应诗歌独立定调

回应保持原调、原结构时无需额外步骤，继续写 `{"song_id":"原歌曲ID"}`。可选 `route` 与 `structure_basis` 覆盖本次结构；可选 `bpm` 与 `bpm_basis` 填乐手值；不支持的字段直接报错，不再默默丢弃。

换调或新增回应先用同一会话：

1. `response-options` 输入 `song_id`、`arrangement_id`，读取实际 PCO 版本和现有调。
2. `response-preview` 输入返回的 `key_id`、`attachment_id`，取得对应原始 MP3，给用户试听。
3. `response-confirm` 输入 `sha256`、`confirmed:true`、`listened:true`、实际 `user_message`；复核当前来源后交付同字节 MP3。换歌、换主歌调或歌手配置后此确认失效。
4. 新回应先查 RAG；命中用 `material-prepare`（同样支持 supplementary song_id），复用资料不下载文档。未命中才 `response-resources` 取现成中文歌词或简谱；输入 `{"inspect_only":true}` 先列出实际来源，需要分类或选择时传 `review`，字段与 `review-resources` 相同（classifications/selected_source_id/user_message）；代码将依据绑定当前元数据指纹。使用相同优先级、权限和歧义保护。再按原文或已核验分析导入材料。
5. `setlist-propose` 的 response 支持 `key`，只能是原调或独立确认的调。新歌补核验经文；改变结构/调/BPM时输出完整回应结构，不写“结构一致”。新回应标题包含调与空白/乐手 BPM。

这些命令不修改三首主歌定调与话语确认，不写回 PCO。

### 本次现场信息

`setlist-propose` 可选 `event:{"title":"聚会名称","date":"日期","personnel":"本次人员图例","basis":"本次用户或乐手来源"}`，保留在sources和状态中，默认不进入正文。明确需要时另传`presentation:{"show_event_metadata":true}`显示通栏信息；布尔false表示隐藏，不删除元数据。main/response 可选 `route`、`structure_basis`，route 各项用 `current_cue` 表达本次小节、分工和强弱；未知留空。没有 `current_cue` 时仍只显示历史小节数字加星号，绝不复制历史人员；当前route的小节不能与已确认话语窗口矛盾。

重复导出相同请求时使用`setlist-export --reuse-existing --output ...`（待审加`--draft`）。仅在当前selection、来源指纹、导出模式、导出代码/模板均相同，且原DOCX及sources哈希未变时返回已有路径及`reused:true`；不创建请求的新文件，也不追加重复exports。以返回路径为准。请求变化、改过的文件、draft变正式均不会误复用。旧导出无request_digest时不猜测复用；显式不传此参数仍允许另存新副本，且不覆盖现有文件。

### 视觉验收与安全清理

导出自动登记 DOCX 和 sources 的哈希。制作/验收期间可临时保留旧可用稿；新版通过后立即清理，不等用户再次提出，不留到对话最后。只保留一个当前最终DOCX及对应sources，另保留必要原始资料、口述稿和审计。

组装后的[样式与分页验收](../skills/pco-assemble-worship-setlist/references/layout-acceptance.md)为强制步骤。导出前及验收时检查字体/字号/颜色/间距、结构表样式/宽高及分栏，包括正文/表格外框左边界、表格与歌词9pt留白、主歌表格6000twip适中宽度，以及同栏相邻乐段标题前12pt留白；两栏首标题前仍0，段内歌词行距不变。前三首强制独立起页，回应与结束另起合页，附录另起。实际页面必须在原生WPS/Word逐页查看，新稿带required_review_version=5；文件生成成功不等于验收通过。

```sh
./run --state /absolute/state.json artifact-finalize --input actual-review-and-cleanup.json
./run --state /absolute/state.json status
```

`artifact-finalize`只接受当前已确认的最新正式导出，不接受旧稿或待审稿。输入完整真实视觉验收（格式同`artifact-review`），再增加：

```json
{
  "cleanup": {
    "user_message": "实际检查待删文档及来源文件已关闭、无未保存修改的说明",
    "checked_files": [
      {"path": "/absolute/obsolete.docx", "sha256": "登记的旧稿哈希", "closed": true},
      {"path": "/absolute/obsolete.sources.json", "sha256": "登记的旧来源哈希", "closed": true}
    ]
  }
}
```

`checked_files`须精确覆盖本会话全部被替代DOCX/sources和登记预览，不允许漏项、重复、额外原件或`closed:"true"`字符串；无待删项也必须显式传`[]`。代码不能证明应用关闭状态，执行者必须实际核对，仍打开或有未保存修改时暂停并说明，不能伪填关闭或为清理自动关闭用户文档。

命令先在状态副本上检查所有权、最终稿有效验收、旧稿及来源哈希、符号链接、引用保护和整批关闭观察，再自动retire、预演及apply。正常结果`complete:true`且保存finalization审计；任一预检失败均不改状态、不删文件。磁盘I/O中途失败会保存已删除记录，返回`complete:false`及退出码2，便于安全重试；不是文件系统原子事务，不能报告整批成功。

新正式导出要求该合并收尾记录；即使手动cleanup为空也不能绕过。若新正式稿已验收却未收尾，下一次新导出会被拦截，提示`artifact-finalize`；精确复用已有文件不新增产物。完成上一版后继续制作新草稿不受阻，新正式版通过前不删除上一可用版。历史导出不自动迁移；已登记同哈希有效验收可用`path`、`sha256`、`use_existing_review:true`加上述`cleanup`收尾，不必重传`visual`，不会改写原观察时间。

新验收输入保留`path`、`sha256`、`confirmed:true`、实际`user_message`和`checks:["layout","colors","content","pagination"]`，并必须增加实际`visual`：

```json
{
  "app": "WPS",
  "page_count": 5,
  "pages": [
    {"page": 1, "blocks": ["main:1"], "checks": ["chinese", "clipping", "overlap", "pagination"], "note": "第一首本页实际观察"},
    {"page": 2, "blocks": ["main:2"], "checks": ["chinese", "clipping", "overlap", "pagination"], "note": "第二首本页实际观察"},
    {"page": 3, "blocks": ["main:3"], "checks": ["chinese", "clipping", "overlap", "pagination"], "note": "第三首本页实际观察"},
    {"page": 4, "blocks": ["response", "ending"], "checks": ["chinese", "clipping", "overlap", "pagination"], "note": "回应和结束同在独立页"},
    {"page": 5, "blocks": ["spoken"], "checks": ["chinese", "clipping", "overlap", "pagination"], "note": "口述附录本页实际观察"}
  ],
  "songs": [
    {"position": 1, "pages": [1], "lyrics_pages": [1], "checks": ["heading_baseline", "first_lyric_baseline", "table_row_heights", "table_content_width", "instrument_notation", "fonts", "font_sizes", "section_colors", "paragraph_spacing", "lyric_spacing", "table_style", "table_dimensions", "single_song_per_page", "lyrics_pagination"], "note": "第一首逐项实际观察"},
    {"position": 2, "pages": [2], "lyrics_pages": [2], "checks": ["heading_baseline", "first_lyric_baseline", "table_row_heights", "table_content_width", "instrument_notation", "fonts", "font_sizes", "section_colors", "paragraph_spacing", "lyric_spacing", "table_style", "table_dimensions", "single_song_per_page", "lyrics_pagination"], "note": "第二首逐项实际观察"},
    {"position": 3, "pages": [3], "lyrics_pages": [3], "checks": ["heading_baseline", "first_lyric_baseline", "table_row_heights", "table_content_width", "instrument_notation", "fonts", "font_sizes", "section_colors", "paragraph_spacing", "lyric_spacing", "table_style", "table_dimensions", "single_song_per_page", "lyrics_pagination"], "note": "第三首逐项实际观察"}
  ]
}
```

上述`visual`还必须提供`response_ending:{"page":4,"checks":["fonts","font_sizes","section_colors","paragraph_spacing","table_style","table_dimensions","instrument_notation","separate_page"],"note":"回应和结束的实际样式观察"}`，页码以实际为准。这些是结构示例，不是实际五页验收；总页数与pages按原生应用实际填写，不得漏页/重号。主歌页blocks只能是一首，回应/结束必须恰好合占一个独立页，附录不能混排。每首pages及lyrics_pages须覆盖全部实际页面，不留孤立标题/表格页；跨页时另外填写`pagination_exception:{"reason":"实际原因","adjustments":["实际尝试的排版调整"]}`，未说明跨页会被拒绝。优先整首同页，不能为凑页数删词或使用过小字体。

每首须逐项核对字体、字号、颜色、段落/歌词间距、表格样式及宽高、两侧基线和IN；v5还须在上方每首checks增加`left_edges`、`table_lyric_gap`、`table_width_balance`、`section_spacing`，response_ending.checks增加`left_edges`，并记录实际观察。`section_spacing`核对Verse之间、Chorus/Bridge及其他相邻乐段；仅单乐段时说明不适用。缺任一项拒绝验收。不适用的基线观察写明原因，不照抄示例结论。应用支持`WPS`或`Microsoft Word`，辅助无字体渲染器不作为原生视觉验收。新记录保存`review_version:5`；笼统四项checks或v2/v3/v4不能替代。程序另核对可判定的OOXML结构，但不能证明观察真实发生，不能以单元测试代替WPS观察。无法访问WPS/锁屏时保留待验收及旧可用稿，不登记通过或清理唯一已验收版本。

生成预览后，用 `artifact-register` 输入 `path`、`kind:"temporary"`、`owner:"本次DOCX路径"` 登记。仅允许会话专用 `render-cache/` 下的 PNG/JPEG/PDF，不登记原件、下载、数据库、源文件或最终 DOCX。`cleanup` 默认预演（输入 `{}`）；明确 `{"apply":true}` 才删除。必须先完成对应文件的视觉验收；只删登记且未变更、未引用的缓存，忽略链接/目录重定向/用户改动，并记录结果。当前流程没有生成任何缓存时返回空清单，不扫描目录猜测“废弃”。

低层兼容命令仍保留：`artifact-review`单独记录视觉观察；`artifact-retire`输入`{"path":"旧导出路径","sha256":"旧文件已登记哈希","replacement":"已通过视觉验收的新导出路径","confirmed":true,"user_message":"本次明确废弃与替代说明"}`后才可`cleanup`。它们不能代替新正式导出的合并收尾门禁。新默认流程由`artifact-finalize`执行相同替代和清理保护，保留导出审计并标记retired_for_cleanup/superseded_by，不为删文件破坏溯源。最终稿、用户修改、原件和未登记文件不删除。

## 验证

```sh
/usr/bin/python3 -m unittest discover -s . -v
```

自动化单元/CLI 测试使用隔离临时目录与模拟 PCO/经文资料，覆盖定歌、定调、试听确认、混合资料交付、话语和完整歌单。完整场景需明确选择：

```sh
WORKFLOW_PYTHON=/absolute/bundled/python3
"$WORKFLOW_PYTHON" -B live_scenario.py --run-dir /absolute/new-test-directory --output /absolute/new-test.docx --allow-simulated-human-gates
```

该脚本通过真实 CLI、PCO、RAG、现有 Key MP3、ffprobe 与在线和合本跑“我呼求→让我→如你”混合场景，保留逐命令摘要、隔离会话、来源与异常。2026-11-03 实际验证两首复用、一首中文歌词获取、三首 MP3 解码交付、三处经文在线读取以及完整 Word 导出。它不是任意主题的独立 AI 评测：方案和话语是 Codex 编写的场景输入；歌手人数是测试参数；人类听过/确认只能模拟，不能当作真人验收。

`new --test-run` 标记会话；脚本必须显式允许模拟门禁。测试导出的 Word 有“非现场定稿”标记，结果为 `test_artifact`。没有此标记的正式会话继续要求真实用户反馈，不得从测试会话复制确认。所有项目 skills 均未全局安装。
