<p align="center">
  <b><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></b>
</p>

# DSH Client —— DeepSeek Harness 桌面客户端（Python + JadeUI）

用 JadeUI（JadeView 的 Python SDK）给 DeepSeek Harness 的 Web GUI 加一个原生窗口壳。
窗口里跑的是原版 DSH Web 界面——由客户端**自己捆绑的 DSH** 提供（端口从 3080 起自动找空闲端口），
插件、主题、skill 坞全部原样可用。

## 它需要 Node.js 吗？

**需要。** 窗口只是浏览器外壳；真正的 DSH（模型调用、工具、文件操作）是一个 **Node.js 进程**，
Web 界面由它在本机提供。Python/JadeUI 只负责：拉起后端 → 等它就绪 → 打开窗口 → 退出时收尾。

分发时有两条路：

| 方式 | 做法 | 用户侧要求 |
|------|------|-----------|
| A. 捆绑（推荐） | exe 目录带 `runtime/node.exe` + `dsh/`（npm 安装的 `@deepseek-ai/dsh`） | 什么都不用装 |
| B. 依赖系统环境 | 不捆绑，靠 PATH 上的 `node` / `dsh` | 用户先装好 Node + DSH |

另外目标机需要 **WebView2 Runtime**（Win11 自带；Win10 未装时会被 JadeView 要求安装）。

## 开发运行

```powershell
cd D:\nodejsApp\DSH-Client
pip install -r requirements.txt
python main.py
```

- **默认只跑自己捆绑的 DSH**：数据目录是应用目录下的 `dsh-home/`（与机器上用户自己装的
  DSH 的 `~/.dsh` 完全隔离，互不干扰）；端口被占自动顺延（3080 → 3081 → …），
  **绝不附着外部实例**；
- 关闭窗口时只停掉自己拉起的后端；
- `--attach`：显式附着到 `--port` 上已在运行的 DSH（旧行为，此时预设注入走对方 DSH 的 HTTP API）；
- `python main.py --check-backend`：只做后端检查/拉起，不开窗口（无 GUI 环境验证用）。

## 后端解析顺序

| 配置 | 说明 |
|------|------|
| `DSH_BIN` | 完整 dsh 命令（如 `dsh`、`C:\...\dsh.exe`） |
| `DSH_ENTRY` | `@deepseek-ai/dsh/lib/bin.js` 的绝对路径 |
| `./dsh/node_modules/@deepseek-ai/dsh/lib/bin.js` | 捆绑目录（打包用） |
| `DSH_NODE` / `./runtime/node.exe` / PATH `node` | Node 可执行文件 |

命令行参数：`--port`、`--check-backend`、`--dev-tools`、`--dsh-entry`、`--node`。

## 自动更新 DeepSeek Harness（捆绑模式）

**首次安装**：客户端不再捆绑 DSH。启动时未检测到 `dsh/` 目录会进入安装向导
（内置协议服务托管 `web/` 引导页）：页面选择 npm 源（npmmirror / npmjs / 自定义）
后下载 DSH 并显示环形进度，完成后自动重启进入主界面；
未检测到 Node.js 时页面会给出 nodejs.org 下载说明（便携版放入 `runtime/`）。

客户端会管理本地 `dsh/` 目录（npm 包 `@deepseek-ai/dsh`）：

1. **启动后自动检查**：后台查询 npm registry 上的最新版本，有新版本弹系统通知（"立即更新 / 忽略"）。
2. **一键更新**：点击后停掉后端 → 在暂存目录 `npm install @deepseek-ai/dsh@latest` → 成功后原子换目录（失败不动旧版）→ 自动重启客户端。
3. **`--attach` 附着外部实例时**：只更新捆绑副本，不打断外部 DSH，下次独立启动时生效。
4. 开发兜底模式（未捆绑、使用 DSH_ENTRY/兜底路径）不参与自动更新。

配置：

| 环境变量 / 参数 | 说明 |
|------|------|
| `DSH_NPM_REGISTRY` | npm 源（默认 `https://registry.npmmirror.com`，可改官方源） |
| `--no-auto-update` | 关闭启动时的后台检查 |
| `--check-update` | 无窗口：打印当前/最新版本 |
| `--update-now` | 无窗口：立即安装/更新捆绑 DSH |
| `--dsh-dir` | 捆绑目录（默认 `./dsh`） |

更新失败时客户端自动重新拉起旧后端并刷新页面，不影响使用。

## 窗口图标与标题栏（title-overlay）

- **窗口图标跟随页面**（默认开启）：DSH 页面只有 SVG favicon，WebView2 无法用于窗口图标；
  客户端捆绑 `assets/favicon.png`，通过 preload_js 把页面 icon 链接替换为本地 HTTP 的 PNG，
  配合 `use_page_icon` 让窗口图标跟随。换图标直接替换 `assets/favicon.png`。
  图标注入延迟到页面 load 之后，规避启动期并发（此前 DLL 的野指针 bug 已随
  v2.3.2 / v2.4.0-beta.2 修复，配合修复版 DLL 稳定运行）；
  用 `--no-page-icon` 关闭（打包后想用 exe 图标时）。
- **title-overlay 标题栏**（默认开启，按 JadeView《自定义标题栏》文档实现）：
  - `frame_style="title-overlay"`：系统内置最小化/最大化/关闭按钮覆盖在页面顶部（高 48px，每按钮宽 46px）；
  - 注入页面适配（纯 CSS，本 WebView2 只认样式表规则）：`header.wSkVaW_header` 整块设为
    拖拽区（`-webkit-app-region: drag!important`，双击最大化/还原），全页 `button`
    排除拖拽（`no-drag!important`）保持可点；不注入悬浮拖拽条；
  - 头栏右侧预留 148px 避免控件被系统按钮覆盖；
  - 若与 DSH 页面布局冲突，用 `--no-title-overlay` 关闭，或调整 `main.py` 中
    `build_preload_js` 的 `nodrag_css` / `RESERVE`。

## 与 DSH 的事件桥（client-bridge 插件）

DSH 侧加载 `client-bridge` 插件后（动态插件 `client-1`，或后续固化进预设），
客户端会自动轮询 `/client-bridge/events` 端点，把 agent 生命周期事件转成原生交互：

| DSH 事件 | 客户端表现 |
|------|------|
| `agent-running` | 任务栏进度（不确定态）+ 窗口标题加「● 思考中 ·」前缀 |
| `agent-idle` | 清除进度/标题前缀；窗口失焦时弹「回复完成」通知 + 窗口闪烁 |
| `progress` | 任务栏真实百分比（workflow 按声明阶段推进；普通轮次按步数推进） |
| `agent-error` | 弹错误通知 |
| `subagent-end` | 弹「子任务完成」通知 |
| `desktop-notify` | 模型工具：agent 主动弹原生通知 |
| `desktop-ask` | 模型工具：带「确认/取消」按钮的通知，**阻塞等待用户点击**（15 分钟超时），结果回写 DSH |

- 窗口标题：客户端不设置固定标题，JadeView 自动跟随页面标题（DSH 动态改 `document.title`）；
- 系统托盘：显示主窗口 / 退出（图标用 `assets/favicon.ico`）；
- 用 `--no-notify` 关闭事件桥；轮询间隔 1.2s、失败静默重试；
- 首次轮询只对齐序号，不回放历史事件（避免启动刷屏）。

## 插件固化（client-bridge 预设自动注入）

客户端每次启动都会把事件桥插件固化进**自己 DSH 的数据目录**（幂等，同步、先于后端启动）：

1. 在 `<应用目录>/dsh-home/.agent-presets/dsh-client/` 写入：
   - `agent.cordis.yml`：复制**部署默认预设 standard** 的 composition + `client-bridge` 行（插件文件名为内容哈希 `client-bridge-<hash>.js`，内容更新后 URL 变化，绕过 Node 模块缓存，新会话即用新版）；
   - `preset.yml` 元数据；
   - 插件本体（随客户端 `preset/` 目录分发）。
2. 把 `<应用目录>/dsh-home/settings.yaml` 的 `agent-presets.default` 切到 `dsh-client`，**新创建的会话自动挂载插件**；
3. **更新 DSH 后无需任何额外操作**：下次启动时幂等注入自动重跑（插件文件独立于 `dsh/` 目录，不随更新丢失）；
4. 用 `--no-preset` 关闭自动注入；`--attach` 模式下注入改走对方 DSH 的 HTTP API。

注意：预设对**已运行中的会话**不生效（组合在会话启动时挂载）；DSH 重启后新建的会话即带事件桥。
`dsh-home` 是全新数据目录：首次使用需在界面里配置一次模型 provider（与用户自己的 `~/.dsh`
配置/会话刻意隔离，互不污染）。

## 窗口关闭行为（托盘常驻）

- 点窗口 X → **隐藏到托盘**，进程与后端保持运行；
- 托盘菜单：显示主窗口 / 退出（只有「退出」会真正关闭并清理后端）；
- 隐藏期间事件桥继续工作（回复完成仍会弹通知，点击通知即唤起窗口）。

## 打包成 exe（按 JadeUI 官方打包文档）

1. 准备运行时目录（DSH 本体**不需要**捆绑：首次启动会进安装向导，页面选 npm 源下载）：

```powershell
mkdir runtime
# 复制完整便携版 Node 到 runtime/（node.exe + npm，从 nodejs.org 下载 zip 解压；
# 需包含 runtime/node_modules/npm/bin/npm-cli.js，供安装向导与自动更新使用）
```

2. 安装打包工具（JadeUI 文档推荐 Nuitka 4.0rc7，修复 onefile 缺 vcruntime140.dll 的问题）：

```powershell
pip install https://github.com/HG-ha/jadeui/raw/main/scripts/nuitka-4.0.rc7.zip
pip install jadeui[dev]
curl -O https://raw.githubusercontent.com/HG-ha/Jadeui/main/scripts/build.py
```

3. 打包（首版建议 `--no-onefile` 目录模式；exe 图标用页面图标生成的多尺寸 ICO）：

```powershell
python build.py main.py --output DSHClient --no-onefile `
  --icon assets/favicon.ico `
  --include-data-dir web=web `
  --include-data-dir runtime=runtime `
  --include-data-dir assets=assets `
  --include-data-dir preset=preset
```

产物：`dist/DSHClient/DSHClient.exe`（JadeUI 会**自动包含其 DLL**）。整目录一起分发。
onefile 会把内容解压到临时目录，node_modules 较大时启动慢且路径易出问题，暂不推荐。
`assets/favicon.ico` 由 `python assets/make_icon.py` 从 `assets/favicon.png`（页面图标）生成（16–128 共 6 尺寸）；换页面图标后重跑即可。
exe 图标与页面图标一致后，可加 `--no-page-icon` 让窗口图标直接走 exe 图标（最稳）。

## 注意事项

- 客户端使用**自己独立的** `dsh-home/` 数据目录，与用户自己装的 DSH（`~/.dsh`）完全隔离，
  各自的配置/预设/会话互不影响；需要复用已有配置时请用 `--attach` 附着外部实例。
- 端口 3080–3099 全被占时会报错；默认自动顺延，一般无需手动指定 `--port`。
- 调试打包版：`python build.py main.py --console` 查看日志，或 `--dev-tools` 开 F12。
- 更新用 npm 源在国内可用 `DSH_NPM_REGISTRY=https://registry.npmmirror.com`（默认即此），
  公司内网有私有源时按需覆盖。
