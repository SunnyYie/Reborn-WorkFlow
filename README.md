# Reborn-WorkFlow

敬拜选歌、定调试听、祷告话语与 Word 歌单生成。现有业务引擎保持不变，新增跨平台启动与标准 Agent 工具接入层。

## 使用方式

| 设备 / Agent 能力 | 入口 | 边界 |
| --- | --- | --- |
| Windows 本地执行型 Agent | `Start.cmd`，或 `python reborn.py onboard` | 需要 Python 3.10+；可授权 winget 安装 Python 3.12；不依赖 PowerShell 脚本策略 |
| macOS 本地执行型 Agent | `Start.command`，或 `python3 reborn.py onboard` | 需要已有 Python 3.10+；有 Homebrew 时可授权安装 FFmpeg |
| Linux 本地执行型 Agent | `sh Start.command`，或 `python3 reborn.py onboard` | 需要 Python 3.10+、venv、系统密钥库；无受支持包管理器时由管理员部署音频工具 |
| 支持本地 stdio MCP 的 Agent | 启动器生成的 MCP 配置 | 只连接自己信任的 Agent；配置格式/导入方式由宿主决定 |
| 手机、平板、远程 Agent | 由受信任的电脑运行引擎，经宿主支持的工具连接使用 | 本项目不提供公网服务，不提供手机原生执行或远程 HTTP MCP |
| 纯聊天模式（包括没有执行工具的模式） | 暂不能直接执行 | 上传项目/提示词不等于获得设备操作能力；豆包具体版本未实机认证 |

首次引导会请求安装授权、检查音频工具、选择已有授权模板、初始化空库或保留原库、验证 PCO，并按需在本机安全输入凭据。不会覆盖现有模板、资料库或安装到全局 Python。拒绝授权或缺资料时如实保留待办。

**准备完成、连接有能力的 Agent 后**，用户只需说明主题等需求、选择方案、实际试听并确认内容；Agent 按阶段自行继续，不要求用户解释命令或下一步。

模板和历史歌曲库属于私有资料，不随源码分发。空库可从 PCO 获取新资料，但不具备历史复用能力。现有模板版式、歌词版权和 PCO 权限仍需合法授权。

## 给 Agent / 部署者

先读 [AGENTS.md](AGENTS.md) 和 [Agent.md](Agent.md)。通用命令：

```sh
python reborn.py doctor
python reborn.py setup --yes --with-mcp
python reborn.py doctor --online
python reborn.py mcp-config
python reborn.py run workflow new
python reborn.py run workflow --state <实际返回的路径> next
```

POSIX 可用 `python3`。安装后入口自动使用项目 `.venv`。`setup --system-tools` 另授权现有 winget/Homebrew 安装 FFmpeg；不自动安装包管理器、不提权、不绕过 Windows 执行策略。新装系统工具可能需要重新打开终端刷新 PATH。

`doctor` 默认不联网、不安装、不读取凭据值；`--online` 调用流程实际使用的 PCO API，不依赖 `pco` CLI。原有 `workflow/run doctor` 为兼容保持原行为。

`auth` 仅本机交互式输入并使用 macOS Keychain、Windows Credential Manager 或 Linux Secret Service；没有安全系统后端则失败，不退回明文文件。已有 `PCO_CLIENT_ID` / `PCO_SECRET` 优先，已有 macOS `pco-cli` Keychain 仍兼容。凭据绝不可发到聊天中。

MCP 使用官方 Python SDK（可选依赖），提供资料/规则读取、原流程命令、下一步指引及会话文件资源。它没有独立模型；推荐、经文核验、谱面识读及页面检查仍依赖有相应能力的 Agent。MP3/DOCX 资源能否播放/预览取决于宿主，取得资源不等于已试听或验收。

MCP 仅暴露当前 checkout 管理的完整 workflow 会话；现有外部路径会话和 mini/RAG 写入继续使用原 CLI。不接受任意 shell、任意输出路径、全局库写入或凭据写入工具。不写回 PCO 歌曲、计划或排班。仅 stdio，不监听网络端口。

## 测试与边界

```sh
python -B -m unittest discover -s tests_portable -p 'test_*.py' -v
python -B -m unittest discover -s workflow -p 'test_*.py' -q
python -B -m unittest discover -s rag -p 'test_*.py' -q
python -B -m unittest discover -s mini-workflow -p 'test_*.py' -q
python -B -m unittest discover -s scripts -p 'test*.py' -q
```

原有完整回归依赖本机私有模板与已授权历史原件，不在公共 CI 伪造替代。跨平台 CI 运行无需私有资料的适配层与核心门禁测试；CI 配置存在不代表 CI 已运行或设备/Agent 已认证。模拟确认、模拟音频和测试产物只用于隔离回归，不是现场验收。

详细业务接口见 [workflow/README.md](workflow/README.md)，资料边界见 [SECURITY.md](SECURITY.md)。
本次实际结果、曾出现的依赖失败及未验收设备见 [跨平台验收记录](docs/portable-validation.md)。
