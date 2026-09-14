<p align="center">
  <b><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></b>
</p>

# DSH Client — Desktop Client for DeepSeek Harness (Python + JadeUI)

A native window shell for DeepSeek Harness's web GUI, built with JadeUI (the Python SDK of JadeView).
The window hosts the stock DSH web interface — served by the client's **own bundled DSH** (port auto-probes upward from 3080) —
so plugins, themes, and the skill dock all work out of the box.

## Do I need Node.js?

**Yes.** The window is only a browser shell; the actual DSH (model calls, tools, file operations) is a **Node.js process**
that serves the web UI locally. Python/JadeUI is only responsible for: launching the backend → waiting for it to be ready →
opening the window → cleaning up on exit.

Two distribution modes:

| Mode | How | User requirement |
|------|-----|------------------|
| A. Bundled (recommended) | exe folder ships `runtime/node.exe` + `dsh/` (npm-installed `@deepseek-ai/dsh`) | Nothing to install |
| B. System dependencies | Not bundled; relies on `node` / `dsh` on `PATH` | User installs Node + DSH first |

The target machine also needs **WebView2 Runtime** (built into Win11; on Win10 JadeView will prompt you to install it).

## Development

```powershell
cd D:\nodejsApp\DSH-Client
pip install -r requirements.txt
python main.py
```

- **By default only the bundled DSH is run**: the data directory is `dsh-home/` inside the app folder
  (fully isolated from a locally installed DSH's `~/.dsh`); if the port is taken it slides to the next free one
  (3080 → 3081 → …) and **never attaches to an external instance**;
- Closing the window stops only the backend we launched;
- `--attach`: explicitly attach to an already-running DSH on `--port` (old behavior; preset injection then goes through the other DSH's HTTP API);
- `python main.py --check-backend`: only check/launch the backend, no window (for headless verification).

## Backend resolution order

| Configuration | Description |
|------|------|
| `DSH_BIN` | Full dsh command (e.g. `dsh`, `C:\...\dsh.exe`) |
| `DSH_ENTRY` | Absolute path to `@deepseek-ai/dsh/lib/bin.js` |
| `./dsh/node_modules/@deepseek-ai/dsh/lib/bin.js` | Bundled directory (for packaging) |
| `DSH_NODE` / `./runtime/node.exe` / `node` on `PATH` | Node executable |

CLI flags: `--port`, `--check-backend`, `--dev-tools`, `--dsh-entry`, `--node`.

## Auto-updating DeepSeek Harness (bundled mode)

**First install**: the client no longer bundles DSH. When no `dsh/` directory is detected on startup, an install wizard
opens (built-in protocol server serves the `web/` bootstrap page): pick an npm registry (npmmirror / npmjs / custom),
download DSH with a progress ring, and auto-restart into the main UI when done;
if Node.js is not detected, the page links to nodejs.org (place the portable version into `runtime/`).

The client manages the local `dsh/` directory (npm package `@deepseek-ai/dsh`):

1. **Auto-check after startup**: queries the npm registry for the latest version in the background and shows a system notification ("Update now / Ignore") when a newer version exists.
2. **One-click update**: stops the backend → `npm install @deepseek-ai/dsh@latest` in a staging directory → atomically swaps directories on success (the old version stays untouched on failure) → auto-restarts the client.
3. **While attached via `--attach`**: only the bundled copy is updated; the external DSH is left running and the change takes effect next standalone start.
4. Dev fallback mode (not bundled, using `DSH_ENTRY`/fallback path) does not take part in auto-update.

Configuration:

| Env var / flag | Description |
|------|------|
| `DSH_NPM_REGISTRY` | npm registry (default `https://registry.npmmirror.com`, can switch to upstream) |
| `--no-auto-update` | Disable the background check on startup |
| `--check-update` | Headless: print current/latest version |
| `--update-now` | Headless: install/update the bundled DSH immediately |
| `--dsh-dir` | Bundled directory (default `./dsh`) |

If an update fails, the client re-launches the old backend and refreshes the page — usage is not affected.

## Window icon & title bar (title-overlay)

- **Window icon follows the page** (on by default): the DSH page ships only an SVG favicon, which WebView2 cannot use as the window icon;
  the client bundles `assets/favicon.png` and swaps the in-page icon link for a locally served PNG via `preload_js`,
  combined with `use_page_icon` so the window icon tracks the page. Replace `assets/favicon.png` to change the icon.
  Icon injection is deferred until after the page load to avoid startup races (the earlier DLL wild-pointer bug was fixed
  in v2.3.2 / v2.4.0-beta.2 and runs stable with the fixed DLL).
  Disable with `--no-page-icon` (when you want the packaged exe icon).
- **title-overlay title bar** (on by default; implemented per JadeView's "Custom Title Bar" docs):
  - `frame_style="title-overlay"`: built-in minimize/maximize/close buttons overlay the page top (48px tall, 46px per button);
  - page adaptation injected (pure CSS — this WebView2 only honors stylesheet rules): the `header.wSkVaW_header` block becomes the
    drag region (`-webkit-app-region: drag!important`, double-click to maximize/restore), all page `button`s opt out (`no-drag!important`)
    so they stay clickable; no floating drag bar is injected;
  - 148px reserved on the header's right side so controls don't get covered by the system buttons;
  - if it conflicts with the DSH layout, disable with `--no-title-overlay` or adjust `nodrag_css` / `RESERVE` in `build_preload_js` in `main.py`.

## Event bridge with DSH (client-bridge plugin)

Once the DSH side loads the `client-bridge` plugin (dynamic plugin `client-1`, or later baked into a preset),
the client polls the `/client-bridge/events` endpoint and turns agent lifecycle events into native interactions:

| DSH event | Client behavior |
|------|------|
| `agent-running` | Taskbar indeterminate progress + window title prefixed with "● Thinking ·" |
| `agent-idle` | Clears progress/title prefix; when the window is unfocused, pops a "Reply done" notification + window flash |
| `progress` | Real taskbar percentage (workflow advances per declared stage; normal rounds per step) |
| `agent-error` | Error notification |
| `subagent-end` | "Subtask done" notification |
| `desktop-notify` | Model tool: agent pushes a native notification |
| `desktop-ask` | Model tool: notification with "Confirm/Cancel" buttons, **blocking until the user clicks** (15-min timeout), result written back to DSH |

- Window title: the client sets no fixed title; JadeView follows the page title automatically (DSH updates `document.title` dynamically);
- System tray: show main window / exit (icon from `assets/favicon.ico`);
- Disable the bridge with `--no-notify`; polling interval 1.2s, silent retry on failure;
- The first poll only aligns the sequence number and never replays history (avoids a startup flood).

## Preset pinning (client-bridge preset auto-injection)

On every startup the client bakes the event-bridge plugin into **its own DSH data directory** (idempotent, synchronous, before backend launch):

1. Writes into `<app>/dsh-home/.agent-presets/dsh-client/`:
   - `agent.cordis.yml`: copies the **default preset `standard`**'s composition + the `client-bridge` line (plugin file named by content hash `client-bridge-<hash>.js`, so a changed URL busts Node's module cache and new sessions use the new version);
   - `preset.yml` metadata;
   - the plugin body itself (shipped with the client in `preset/`).
2. Switches `<app>/dsh-home/settings.yaml`'s `agent-presets.default` to `dsh-client` — **new sessions automatically mount the plugin**;
3. **No extra steps after a DSH update**: the idempotent injection re-runs on next startup (the plugin file lives outside `dsh/` and survives updates);
4. Disable with `--no-preset`; in `--attach` mode injection goes through the other DSH's HTTP API instead.

Note: the preset does not affect **already-running sessions** (composition is mounted at session start); sessions created after a DSH restart carry the bridge.
`dsh-home` is a fresh data directory: providers must be configured once in the UI on first use
(deliberately isolated from your own `~/.dsh` config/sessions — no cross-contamination).

## Window close behavior (tray resident)

- Clicking the window's X → **hide to tray**, process and backend keep running;
- Tray menu: show main window / exit (only "Exit" truly closes and cleans up the backend);
- While hidden the event bridge keeps working (reply-done notifications still pop; clicking one wakes the window).

## Packaging an exe (per JadeUI official packaging docs)

1. Prepare the runtime directory (DSH itself does **not** need bundling: first launch enters the install wizard, pick an npm registry in the page):

```powershell
mkdir runtime
# Copy a full portable Node into runtime/ (node.exe + npm, from the nodejs.org zip;
# must include runtime/node_modules/npm/bin/npm-cli.js for the wizard and auto-update)
```

2. Install the build tool (JadeUI docs recommend Nuitka 4.0rc7, which fixes the missing vcruntime140.dll in onefile):

```powershell
pip install https://github.com/HG-ha/jadeui/raw/main/scripts/nuitka-4.0.rc7.zip
pip install jadeui[dev]
curl -O https://raw.githubusercontent.com/HG-ha/Jadeui/main/scripts/build.py
```

3. Build (a `--no-onefile` directory build is recommended for the first release; exe icon uses the multi-size ICO generated from the page icon):

```powershell
python build.py main.py --output DSHClient --no-onefile `
  --icon assets/favicon.ico `
  --include-data-dir web=web `
  --include-data-dir runtime=runtime `
  --include-data-dir assets=assets `
  --include-data-dir preset=preset
```

Output: `dist/DSHClient/DSHClient.exe` (JadeUI **automatically includes its DLL**). Distribute the whole directory.
Onefile extracts to a temp directory — slow startup and fragile paths when node_modules is large, so it's not recommended for now.
`assets/favicon.ico` is generated by `python assets/make_icon.py` from `assets/favicon.png` (the page icon; 6 sizes from 16 to 128);
re-run it after swapping the page icon. Once the exe icon matches the page icon you can add `--no-page-icon` to drive the window icon straight from the exe (most stable).

## Notes

- The client uses its **own independent** `dsh-home/` data directory, fully isolated from a locally installed DSH (`~/.dsh`);
  their configs/presets/sessions don't affect each other. Use `--attach` to reuse an existing configuration.
- It errors out if ports 3080–3099 are all busy; by default it auto-slides, so `--port` rarely needs to be set manually.
- Debug the packaged build: `python build.py main.py --console` for logs, or `--dev-tools` for F12.
- Updates use the npm registry; in China `DSH_NPM_REGISTRY=https://registry.npmmirror.com` (the default) works well — override with a private source if needed.