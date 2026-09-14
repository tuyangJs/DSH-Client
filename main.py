"""
DSH Client —— 用 JadeUI (Python) 把 DeepSeek Harness 的 Web GUI 装进桌面窗口。

架构（方案 A：薄壳封装 + 自动更新）
    [DSHClient.exe (Python/JadeUI)]
        │  1. 把自己的 dsh-client 预设注入捆绑 DSH 的数据目录 <base>/dsh-home（幂等，
        │     每次启动都执行 → 更新 DSH 后下次启动自动重新注入）
        │  2. 启动捆绑 DSH:  node <dsh>/lib/bin.js web --port N
        │     （N 从 3080 起自动顺延找空闲端口；DSH_HOME 指向 <base>/dsh-home，
        │      与机器上用户自己装的 DSH 完全隔离，绝不附着外部实例）
        │  3. 轮询 http://127.0.0.1:N 直到就绪
        │  4. 创建 JadeView 窗口并导航到该 URL（复用 WebView2 渲染）
        │  5. 窗口全部关闭 → 停掉自己拉起的后端进程
        └─  6. 后台检查捆绑 DSH 的新版本（npm registry），一键更新并自动重启

后端解析顺序（后启动的优先）:
    1. 环境变量 DSH_BIN   —— 完整的 dsh 命令（例如 dsh 或 dsh.exe）
    2. 环境变量 DSH_ENTRY —— @deepseek-ai/dsh/lib/bin.js 的绝对路径
    3. 随应用捆绑的  ./dsh/node_modules/@deepseek-ai/dsh/lib/bin.js
    4. 开发机兜底路径（见 DEV_FALLBACK_ENTRY，分发时删除）
Node 解析顺序:
    1. 环境变量 DSH_NODE
    2. 随应用捆绑的  ./runtime/node.exe
    3. PATH 上的 node

默认只跑自己捆绑的 DSH（独立数据目录 dsh-home）。仅当显式传 --attach 时，
才附着到目标端口上已在运行的 DSH（此时改用 HTTP API 把预设注入那个实例）。
更新功能只管理捆绑的 ./dsh 目录，不打扰外部实例。
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Optional

import updater

APP_NAME = "DeepSeek Harness"
DEFAULT_PORT = 3080
READY_TIMEOUT = 180          # 后端启动最长等待（秒）
SINGLETON_PORT = 45899       # 单实例锁用的本地端口
RELAUNCH_DELAY_ENV = "DSH_RELAUNCH_DELAY"  # 重启衔接：新进程先睡几秒再抢单实例锁

# 开发机兜底：本机 npx 安装的 DSH 入口。分发版应删除，改走 ./dsh 捆绑目录。
DEV_FALLBACK_ENTRY = (
    r"C:\Users\ihanl\AppData\Local\npm-cache\_npx\1e7f6d9597241db0"
    r"\node_modules\@deepseek-ai\dsh\lib\bin.js"
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 页面图标 → 窗口图标
# DSH 页面只声明了 SVG favicon，WebView2 无法把 SVG 设成窗口位图图标。
# 客户端捆绑 assets/favicon.png（与 DSH 页面图标同款），通过 preload_js 把页面的
# icon 链接替换为本地 HTTP 服务提供的 PNG，配合 use_page_icon 让窗口图标跟随页面。

class IconHttpServer:
    """本地图标服务：把捆绑的 PNG 图标以 HTTP 提供给页面 / WebView2。"""

    def __init__(self) -> None:
        self.base = None        # 例如 http://127.0.0.1:45890
        self.port = None
        self._server = None
        self._thread = None
        self._png = None
        self._ctype = "image/png"
        for name, ctype in (("favicon.png", "image/png"), ("favicon.ico", "image/x-icon")):
            p = base_dir() / "assets" / name
            if p.is_file():
                try:
                    self._png = p.read_bytes()
                    self._ctype = ctype
                    break
                except Exception as e:
                    print("[icon] 读取 %s 失败: %s" % (name, e), flush=True)
        if self._png is None:
            print("[icon] 未找到 assets/favicon.png / favicon.ico，窗口图标保持默认", flush=True)
            return
        self._start()

    def _start(self) -> None:
        png, ctype = self._png, self._ctype

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/favicon.png":
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(png)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(png)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # 静默
                pass

        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except Exception as e:
            print("[icon] 图标服务启动失败: %s" % e, flush=True)
            return
        self._server = server
        self.port = server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        print("[icon] 图标服务就绪: %s" % self.base, flush=True)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None


def build_preload_js(icon_base: Optional[str], title_overlay: bool = True) -> Optional[str]:
    """生成注入页面的脚本：图标链接替换 + title-overlay 页面适配。返回 None 表示无需注入。

    注意：preload_js 在 document 刚创建时执行，此时页面自己的 <link rel="icon">
    还没解析进来，因此必须等到 DOMContentLoaded 再替换，否则会被页面原链接压过。
    """
    parts = []

    if icon_base:
        png_url = "%s/favicon.png" % icon_base
        # 图标链接推迟到 load 之后 1.2s 再注入：实测 DLL 的后台图标更新线程
        # 与启动期其他操作（invoke 响应/覆盖层创建）并发时偶发 SEH 访问冲突。
        parts.append(
            "(function(){"
            "var ICON_URL='" + png_url + "';"
            "function apply(){try{"
            "var links=document.querySelectorAll('link[rel~=\"icon\"]');"
            "for(var i=0;i<links.length;i++){links[i].parentNode.removeChild(links[i]);}"
            "var l=document.createElement('link');l.rel='icon';l.type='image/png';"
            "l.href=ICON_URL;document.head.appendChild(l);"
            "window.__DSH_CLIENT_ICON=ICON_URL;"
            "console.log('[DSH-Client] PNG 图标链接已注入(延迟): '+ICON_URL);"
            "}catch(e){console.log('[DSH-Client] icon inject failed',e);}}"
            "function schedule(){setTimeout(apply,1200);}"
            "if(document.readyState==='complete'){schedule();}"
            "else{window.addEventListener('load',schedule);}"
            "})();"
        )

    if title_overlay:
        # 按文档《自定义标题栏》适配：内置按钮覆盖层高 48px、每按钮宽 46px。
        # 拖拽来源（用户指定）：
        #   - header.wSkVaW_header 存在且可见（非 wSkVaW_headerHidden）时，header 整块
        #     为拖拽区（drag!important），透明拖拽条隐藏；
        #   - 无 header（新对话页等）时，动态创建透明拖拽条（fixed 顶部 48px，
        #     右侧 138px 留给系统按钮）作为拖拽区；
        # 全页 button 排除拖拽（no-drag!important，标题栏按钮保持可点）；
        # 头栏右侧再预留 148px 避免控件被系统按钮覆盖。
        nodrag_css = (
            "header.wSkVaW_header,"
            "header.wSkVaW_header *{-webkit-app-region:drag!important;}"
            "button,"
            "header.wSkVaW_header .wSkVaW_headerActions,"
            "header.wSkVaW_header .wSkVaW_headerActions *,"
            "header.wSkVaW_header .wSkVaW_headerUtilities,"
            "header.wSkVaW_header .wSkVaW_headerUtilities *"
            "{-webkit-app-region:no-drag!important;}"
        )
        strip_css = (
            "#__dsh_client_drag{position:fixed!important;top:0!important;"
            "left:0!important;right:138px!important;height:48px!important;"
            "z-index:2147483000!important;-webkit-app-region:drag!important;"
            "background:transparent!important;}"
        )
        parts.append((
            "(function(){"
            "var RESERVE=148,lastRun=0;"
            "var CSS1='%s';"
            "var CSS2='%s';"
            "var ANCHORS=['[class*=\"sessionLogButton\"]','[class*=\"trigger\"]'];"
            "function syncDragFallback(){try{"
            "var header=document.querySelector('header.wSkVaW_header');"
            "var hidden=!header||header.classList.contains('wSkVaW_headerHidden')"
            "||header.offsetParent===null;"
            "var bar=document.getElementById('__dsh_client_drag');"
            "if(!hidden){"
            "if(bar){bar.style.display='none';}"
            "return;"
            "}"
            "if(!bar){"
            "bar=document.createElement('div');bar.id='__dsh_client_drag';"
            "var root=document.querySelector('[data-slot=\"root\"]');"
            "var host=root||document.body;"
            "if(!host)return;"
            "host.appendChild(bar);"
            "console.log('[DSH-Client] 无 header：已创建透明拖拽区');"
            "}else if(bar.style.display==='none'){"
            "bar.style.display='';"
            "}"
            "}catch(e){}}"
            "function ensureStyles(){try{"
            "if(!document.getElementById('__dsh_client_drag_css')){"
            "var s=document.createElement('style');s.id='__dsh_client_drag_css';"
            "s.textContent=CSS1;document.head.appendChild(s);}"
            "if(!document.getElementById('__dsh_client_nodrag_css')){"
            "var s2=document.createElement('style');s2.id='__dsh_client_nodrag_css';"
            "s2.textContent=CSS2;document.head.appendChild(s2);}"
            "console.log('[DSH-Client] 标题栏拖拽 CSS 已注入');"
            "}catch(e){console.log('[DSH-Client] styles failed',e);}}"
            "var lastTheme='';"
            "function currentTheme(){try{"
            "var s=document.documentElement.style.colorScheme;"
            "if(s==='dark'||s==='light')return s;"
            "return document.body.hasAttribute('data-ds-dark-theme')?'dark':'light';"
            "}catch(e){return 'light';}}"
            "function pushTheme(){try{"
            "var t=currentTheme();if(t===lastTheme)return;"
            "var sent=false;"
            "if(typeof jade!=='undefined'&&jade.invoke){"
            "var p=jade.invoke('setTitlebarTheme',{theme:t});"
            "if(p&&p.catch){p.catch(function(e){});}sent=true;"
            "}else if(window.jade&&window.jade.ipcSend){"
            "window.jade.ipcSend('setTitlebarTheme',JSON.stringify({theme:t}));sent=true;}"
            "if(sent){lastTheme=t;console.log('[DSH-Client] 主题: '+t);}"
            "else{console.log('[DSH-Client] 无可用 IPC 通道 (jade.invoke/ipcSend 均不存在)');}"
            "}catch(e){console.log('[DSH-Client] theme push failed',e);}}"
            "function adapt(){"
            "var now=Date.now();if(now-lastRun<200)return;lastRun=now;"
            "syncDragFallback();"
            "pushTheme();"
            "for(var a=0;a<ANCHORS.length;a++){"
            "var btn=document.querySelector(ANCHORS[a]);"
            "if(!btn)continue;var el2=btn,bar2=null;"
            "for(var j=0;j<8&&el2;j++,el2=el2.parentElement){"
            "var rr=el2.getBoundingClientRect();"
            "if(rr.width>=window.innerWidth*0.6&&rr.top<=8&&rr.height<120){bar2=el2;break;}"
            "}"
            "if(bar2&&!bar2.__dshReserved){bar2.__dshReserved=true;"
            "var prev=bar2.style.paddingRight||'0px';"
            "bar2.style.paddingRight='calc('+prev+' + '+RESERVE+'px)';"
            "console.log('[DSH-Client] 头栏右侧已为系统按钮预留 '+RESERVE+'px');}"
            "break;"
            "}"
            "}"
            "function start(){ensureStyles();adapt();"
            "try{new MutationObserver(adapt).observe(document.body,"
            "{childList:true,subtree:true,attributes:true});"
            "}catch(e){}}"
            "if(document.readyState==='loading'){"
            "document.addEventListener('DOMContentLoaded',start);}"
            "else{start();}"
            "window.addEventListener('load',start);"
            "})();"
        ) % (strip_css, nodrag_css))

    return "\n".join(parts) if parts else None


# ---------------------------------------------------------------- 预设固化（client-bridge 插件自动注入）

CLIENT_PRESET_ID = "dsh-client"
CLIENT_PRESET_META = (
    "name: DSH Client\n"
    "description: 基于部署默认预设 standard，附加 DSH Client 桌面事件桥插件。\n"
)


def ensure_settings_default(settings_path: Path, preset_id: str) -> bool:
    """在 settings.yaml 中确保 agent-presets.default = preset_id（文本级编辑，保留其余内容）。
    注意：这只是 API 不可用时的兜底；正常路径走 settings.update RPC。"""
    text = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else ""
    lines = text.splitlines()
    out: list = []
    found_block = False
    default_done = False
    for idx, line in enumerate(lines):
        if line.strip() == "agent-presets:":
            found_block = True
            out.append(line)
            j = idx + 1
            while j < len(lines) and (lines[j].startswith("  ") or lines[j].strip() == ""):
                if lines[j].startswith("  default:"):
                    out.append("  default: %s" % preset_id)
                    default_done = True
                else:
                    out.append(lines[j])
                j += 1
            if not default_done:
                out.append("  default: %s" % preset_id)
            out.extend(lines[j:])
            break
        out.append(line)
    if not found_block:
        if out and out[-1].strip() != "":
            out.append("")
        out.append("agent-presets:")
        out.append("  default: %s" % preset_id)
    new_text = "\n".join(out).rstrip("\n") + "\n"
    if new_text != text:
        settings_path.write_text(new_text, encoding="utf-8")
        return True
    return False


def ensure_client_preset(entry: Path, dsh_home: Path) -> bool:
    """把 client-bridge 固化进用户预设：复制部署默认预设 standard 的 composition +
    事件桥行，并把默认预设切到 dsh-client。幂等，内容变化时重写。"""
    std = None
    for p in [entry, *entry.parents]:
        candidate = p / "config" / "agent-presets" / "standard" / "agent.cordis.yml"
        if candidate.is_file():
            std = candidate
            break
    if std is None:
        print("[preset] 未找到 standard 预设 composition", flush=True)
        return False
    target = dsh_home / ".agent-presets" / CLIENT_PRESET_ID
    target.mkdir(parents=True, exist_ok=True)

    # 文件名带内容哈希：内容变化 → URL 变化，绕过 Node ESM 模块缓存
    import hashlib
    template_js = base_dir() / "preset" / "client-bridge.js"
    if not template_js.is_file():
        print("[preset] 缺少模板 preset/client-bridge.js", flush=True)
        return False
    data = template_js.read_bytes()
    digest = hashlib.md5(data).hexdigest()[:8]
    bridge_name = "client-bridge-%s.js" % digest
    bridge_js = target / bridge_name
    if not bridge_js.is_file() or bridge_js.read_bytes() != data:
        bridge_js.write_bytes(data)
        print("[preset] 已写入 %s" % bridge_name, flush=True)
    for old in target.glob("client-bridge-*.js"):
        if old.name != bridge_name:
            try:
                old.unlink()
            except OSError:
                pass

    meta_file = target / "preset.yml"
    if not meta_file.is_file() or meta_file.read_text(encoding="utf-8") != CLIENT_PRESET_META:
        meta_file.write_text(CLIENT_PRESET_META, encoding="utf-8")

    composition = std.read_text(encoding="utf-8").rstrip("\n") + "\n\n"
    composition += (
        "# ==== DSH Client 事件桥（由 DSH-Client 自动维护，请勿手改） ====\n"
        "- id: client-bridge\n"
        "  name: '%s'\n" % str(bridge_js.resolve()).replace("\\", "/")
    )
    comp_file = target / "agent.cordis.yml"
    if not comp_file.is_file() or comp_file.read_text(encoding="utf-8") != composition:
        comp_file.write_text(composition, encoding="utf-8")
        print("[preset] 已生成 agent.cordis.yml（standard + client-bridge）", flush=True)

    if ensure_settings_default(dsh_home / "settings.yaml", CLIENT_PRESET_ID):
        print("[preset] settings.yaml 默认预设 -> %s" % CLIENT_PRESET_ID, flush=True)
    else:
        print("[preset] 预设已就绪（无变化）", flush=True)
    return True


def api_rpc(base_url: str, method: str, payload: dict, timeout: float = 12.0) -> dict:
    """DSH 的 /api/<method> RPC（client-request 信封）。返回 result.value。"""
    body = json.dumps({
        "type": "client-request",
        "rpcId": "dsh-client-%s" % uuid.uuid4().hex[:12],
        "method": method,
        "payload": payload,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/" + method,
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    result = data.get("result") or {}
    if not result.get("ok"):
        raise RuntimeError("API %s 失败: %s" % (
            method, json.dumps(result.get("error", {}), ensure_ascii=False)[:300]))
    return result.get("value") or {}


def ensure_client_preset_api(base_url: str) -> bool:
    """通过 DSH 自身 API 固化 client-bridge 预设。

    不猜测任何路径：agentPreset.copy 由对方 DSH 服务把其默认预设复制到其用户根，
    settings.update 走其 settings 服务；插件行按 DSH 家目录约定落盘（API 不可用时的兜底
    是本地 ~/.dsh 写入）。最后用 agentPreset.list 校验默认预设与挂载状态。
    """
    presets = api_rpc(base_url, "agentPreset.list", {})
    rows = presets.get("presets") or []
    default_id = next((p["id"] for p in rows if p.get("isDefault")),
                      rows[0]["id"] if rows else "standard")
    print("[preset-api] 默认预设: %s" % default_id, flush=True)

    existing = next((p for p in rows if p.get("id") == CLIENT_PRESET_ID), None)
    if existing is None or existing.get("broken"):
        try:
            api_rpc(base_url, "agentPreset.copy", {
                "from": default_id,
                "agentPreset": CLIENT_PRESET_ID,
                "name": "DSH Client",
            })
            print("[preset-api] 已复制 %s -> %s" % (default_id, CLIENT_PRESET_ID), flush=True)
        except RuntimeError as e:
            if "exist" not in str(e).lower():
                raise
            print("[preset-api] %s 已存在，跳过复制" % CLIENT_PRESET_ID, flush=True)

    # 插件目录：DSH 家目录的用户根（copy 落盘位置；部署自定义 home 时由本地兜底逻辑再补）
    home = Path(os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"))
    preset_dir = home / ".agent-presets" / CLIENT_PRESET_ID
    preset_dir.mkdir(parents=True, exist_ok=True)

    template_js = base_dir() / "preset" / "client-bridge.js"
    if not template_js.is_file():
        print("[preset-api] 缺少模板 preset/client-bridge.js", flush=True)
        return False
    data = template_js.read_bytes()
    digest = hashlib.md5(data).hexdigest()[:8]
    bridge_name = "client-bridge-%s.js" % digest
    bridge_js = preset_dir / bridge_name
    if not bridge_js.is_file() or bridge_js.read_bytes() != data:
        bridge_js.write_bytes(data)
        print("[preset-api] 已写入 %s" % bridge_name, flush=True)
    for old in preset_dir.glob("client-bridge-*.js"):
        if old.name != bridge_name:
            try:
                old.unlink()
            except OSError:
                pass

    row = "- id: client-bridge\n  name: '%s'\n" % str(bridge_js.resolve()).replace("\\", "/")
    comp_file = preset_dir / "agent.cordis.yml"
    if comp_file.is_file():
        comp = comp_file.read_text(encoding="utf-8")
        if "id: client-bridge" in comp:
            comp = re.sub(r"- id: client-bridge\s*\n\s*name: '[^']*'", row.rstrip("\n"), comp)
        else:
            comp = comp.rstrip("\n") + "\n\n# ==== DSH Client 事件桥（由 DSH-Client 自动维护，请勿手改） ====\n" + row
    else:
        comp = "# ==== DSH Client 事件桥（由 DSH-Client 自动维护，请勿手改） ====\n" + row
    comp_file.write_text(comp, encoding="utf-8")

    try:
        api_rpc(base_url, "settings.update", {
            "ns": "agent-presets",
            "patch": {"default": CLIENT_PRESET_ID},
        })
        print("[preset-api] settings 默认预设 -> %s" % CLIENT_PRESET_ID, flush=True)
    except Exception as e:
        print("[preset-api] settings.update 失败: %s" % e, flush=True)

    try:
        rows2 = (api_rpc(base_url, "agentPreset.list", {}) or {}).get("presets") or []
        p = next((x for x in rows2 if x.get("id") == CLIENT_PRESET_ID), None)
        if p is not None and not p.get("broken") and p.get("isDefault"):
            print("[preset-api] 校验通过：%s 已就绪且为默认预设" % CLIENT_PRESET_ID, flush=True)
            return True
        print("[preset-api] 校验未通过: %s" % json.dumps(p, ensure_ascii=False), flush=True)
    except Exception as e:
        print("[preset-api] 校验失败: %s" % e, flush=True)
    return False


def preset_inject_worker(url: str, dsh_dir: Path) -> None:
    """后台线程：优先 API 注入，失败再退回本地文件注入。"""
    try:
        if ensure_client_preset_api(url):
            return
    except Exception as e:
        print("[preset-api] API 注入失败（%s），退回本地注入" % e, flush=True)
    try:
        _entry = find_dsh_entry(dsh_dir)
        if _entry is not None:
            _home = Path(os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"))
            ensure_client_preset(_entry, _home)
    except Exception as e:
        print("[preset] 本地注入失败: %s" % e, flush=True)


# ---------------------------------------------------------------- 后端管理

CLIENT_HOME_DIR = "dsh-home"   # 客户端自己的 DSH 数据目录名（位于应用目录下）


def base_dir() -> Path:
    """应用所在目录：开发时是脚本目录，Nuitka 打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def client_home() -> Path:
    """客户端捆绑 DSH 的数据目录：与机器上用户自己装的 DSH（~/.dsh）完全隔离。

    预设/设置/会话都落在这里；每次启动都幂等注入 → 更新 DSH 后下次启动自动重注入。
    """
    return base_dir() / CLIENT_HOME_DIR


def http_ok(url: str, timeout: float = 2.0) -> bool:
    """端口上是否已有 HTTP 服务在响应。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_node() -> str:
    env = os.environ.get("DSH_NODE")
    if env:
        return env
    bundled = base_dir() / "runtime" / "node.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("node")
    if found:
        return found
    raise RuntimeError(
        "未找到 Node.js。请设置环境变量 DSH_NODE，"
        "或把便携版 node.exe 放到 runtime/node.exe（或安装 Node.js）。"
    )


def bundled_dsh_entry(dsh_dir: Path) -> Path:
    return dsh_dir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"


def find_dsh_entry(dsh_dir: Path) -> Optional[Path]:
    """返回 @deepseek-ai/dsh/lib/bin.js 的路径。"""
    env = os.environ.get("DSH_ENTRY")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise RuntimeError("DSH_ENTRY 指向的文件不存在: %s" % env)
    bundled = bundled_dsh_entry(dsh_dir)
    if bundled.is_file():
        return bundled
    dev = Path(DEV_FALLBACK_ENTRY)
    if dev.is_file():
        return dev
    return None


def npm_root(entry: Path) -> Path:
    """bin.js → 所在 npm 安装根目录（node_modules 的上级），作为子进程工作目录。"""
    for p in [entry, *entry.parents]:
        if p.name == "node_modules":
            return p.parent
    return entry.parent


def build_spawn_cmd(port: int, dsh_dir: Path):
    """构造 (命令列表, 工作目录)。返回 None 表示无法构造。"""
    dsh_bin = os.environ.get("DSH_BIN")
    if dsh_bin:
        return [dsh_bin, "web", "--port", str(port)], None
    entry = find_dsh_entry(dsh_dir)
    if entry is None:
        return None
    return [find_node(), str(entry), "web", "--port", str(port)], str(npm_root(entry))


def stop_child(child: Optional[subprocess.Popen]) -> None:
    if child is None or child.poll() is not None:
        return
    print("[backend] 停止 DSH 进程...")
    if os.name == "nt":
        # 连同子进程树一起结束，避免残留 node 子进程
        subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()


def start_backend(port: int, dsh_dir: Path, attach: bool = False):
    """返回 (url, child, spawned, home)。

    默认（attach=False）：总是启动客户端捆绑的 DSH——独立数据目录 dsh-home，
    端口被占自动顺延（绝不附着外部实例，插件/预设/设置完全自包含）。
    attach=True：旧行为——目标端口已有 DSH 则直接附着。
    """
    if attach:
        url = "http://127.0.0.1:%d" % port
        if http_ok(url):
            print("[backend] --attach 已附着到运行中的 DSH: %s" % url)
            return url, None, False, None

    chosen = None
    for offset in range(0, 100):
        candidate = port + offset
        if not port_busy(candidate):
            chosen = candidate
            break
    if chosen is None:
        raise RuntimeError("端口 %d-%d 全部被占用。" % (port, port + 99))

    cmd = build_spawn_cmd(chosen, dsh_dir)
    if cmd is None:
        raise RuntimeError(
            "未找到 DSH 后端。请设置 DSH_BIN 或 DSH_ENTRY，"
            "或将 DSH 安装到 ./dsh（见 README.md）。"
        )
    argv, cwd = cmd
    home = client_home()
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DSH_HOME"] = str(home)  # 客户端自己的数据目录，与机器上其他 DSH 隔离
    url = "http://127.0.0.1:%d" % chosen
    print("[backend] 启动捆绑 DSH: %s (cwd=%s, home=%s)" % (" ".join(argv), cwd, home))
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )

    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if child.poll() is not None:
            raise RuntimeError("DSH 后端提前退出，退出码 %s。" % child.returncode)
        if http_ok(url):
            print("[backend] 就绪: %s" % url)
            return url, child, True, home
        time.sleep(0.5)

    stop_child(child)
    raise RuntimeError("DSH 后端 %d 秒内未就绪。" % READY_TIMEOUT)


# ---------------------------------------------------------------- 单实例锁

def acquire_singleton() -> Optional[socket.socket]:
    """绑定一个本地端口作为单实例锁；已有实例在跑则通知它显示窗口后退出。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(4)
        return s
    except OSError:
        # 已有实例：通过锁端口发 show 指令，让主实例唤起窗口
        try:
            c = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=1.0)
            c.sendall(b"show")
            c.close()
            print("DSH Client 已在运行，已通知显示窗口。", flush=True)
        except OSError:
            print("DSH Client 已在运行。", flush=True)
        sys.exit(0)


def start_singleton_server(sock: socket.socket, ctx: dict) -> None:
    """主实例侧：接受二次启动的连接，收到 show 指令后唤起窗口。"""
    def accept_loop() -> None:
        while not ctx.get("gui_exiting"):
            try:
                sock.settimeout(1.0)
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1.0)
                data = conn.recv(16)
                if data.strip() == b"show":
                    ctx["pending_show"] = True
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def show_loop() -> None:
        while not ctx.get("gui_exiting"):
            time.sleep(0.4)
            if not ctx.get("pending_show"):
                continue
            ctx["pending_show"] = False
            win = ctx.get("window")
            if win is not None:
                try:
                    if getattr(win, "is_minimized", False):
                        win.restore()
                    win.focus()
                    print("[gui] 二次启动：已唤起窗口", flush=True)
                except Exception as e:
                    print("[gui] 唤起窗口失败: %s" % e, flush=True)

    threading.Thread(target=accept_loop, daemon=True).start()
    threading.Thread(target=show_loop, daemon=True).start()


def relaunch() -> None:
    """以新进程重启本客户端（旧进程立即退出，新进程自带延迟避免锁竞争）。"""
    env = os.environ.copy()
    env[RELAUNCH_DELAY_ENV] = "3"
    if getattr(sys, "frozen", False):
        argv = [sys.executable]
    else:
        argv = [sys.executable, str(Path(__file__).resolve())]
    print("[update] 重新启动: %s" % " ".join(argv))
    subprocess.Popen(argv, env=env, creationflags=CREATE_NO_WINDOW)
    os._exit(0)


def dir_size_bytes(path: Path) -> int:
    """递归统计目录内所有文件总字节数（读不了的跳过）。"""
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def newest_stage_dir(dsh_dir: Path) -> Optional[Path]:
    """返回最新的 dsh_stage_* 暂存目录（无则 None）。"""
    newest = None
    for p in dsh_dir.parent.glob("dsh_stage_*"):
        try:
            if newest is None or p.stat().st_mtime > newest.stat().st_mtime:
                newest = p
        except OSError:
            pass
    return newest


# ---------------------------------------------------------------- 更新悬浮球（注入页面：环形进度 + 可拖拽）

UPDATE_BALL_JS = (
    "(function(){"
    "var b=document.getElementById('__dsh_update_ball');"
    "if(!b){"
    "b=document.createElement('div');"
    "b.id='__dsh_update_ball';"
    "b.style.cssText='position:fixed;right:24px;bottom:24px;width:64px;"
    "height:64px;z-index:2147483600;user-select:none;touch-action:none;"
    "cursor:grab;';"
    "b.title='正在更新 DeepSeek Harness（可拖拽）';"
    "b.innerHTML='<svg width=\"64\" height=\"64\" viewBox=\"0 0 64 64\">"
    "<circle cx=\"32\" cy=\"32\" r=\"26\" fill=\"rgba(15,18,22,0.85)\" "
    "stroke=\"rgba(255,255,255,0.15)\" stroke-width=\"4\"/>"
    "<circle id=\"__dsh_update_ring\" cx=\"32\" cy=\"32\" r=\"26\" fill=\"none\" "
    "stroke=\"#10a37f\" stroke-width=\"4\" stroke-linecap=\"round\" "
    "stroke-dasharray=\"163.36\" stroke-dashoffset=\"163.36\" "
    "transform=\"rotate(-90 32 32)\"/></svg>"
    "<div id=\"__dsh_update_pct\" style=\"position:absolute;inset:0;"
    "display:flex;align-items:center;justify-content:center;color:#fff;"
    "font:600 13px/1 system-ui,sans-serif;\">0%</div>';"
    "(document.body||document.documentElement).appendChild(b);"
    "var sx=0,sy=0,ox=0,oy=0,drag=false;"
    "b.addEventListener('pointerdown',function(e){"
    "drag=true;try{b.setPointerCapture(e.pointerId);}catch(err){}"
    "b.style.cursor='grabbing';"
    "var rc=b.getBoundingClientRect();"
    "b.style.left=rc.left+'px';b.style.top=rc.top+'px';"
    "b.style.right='auto';b.style.bottom='auto';"
    "sx=e.clientX;sy=e.clientY;ox=rc.left;oy=rc.top;e.preventDefault();});"
    "b.addEventListener('pointermove',function(e){"
    "if(!drag)return;"
    "var x=Math.max(0,Math.min(ox+e.clientX-sx,window.innerWidth-64));"
    "var y=Math.max(0,Math.min(oy+e.clientY-sy,window.innerHeight-64));"
    "b.style.left=x+'px';b.style.top=y+'px';});"
    "var up=function(){"
    "if(!drag)return;drag=false;b.style.cursor='grab';"
    "try{localStorage.setItem('__dsh_update_ball_pos',"
    "b.style.left+','+b.style.top);}catch(err){}};"
    "b.addEventListener('pointerup',up);"
    "b.addEventListener('pointercancel',up);"
    "try{var saved=localStorage.getItem('__dsh_update_ball_pos');"
    "if(saved){var a=saved.split(',');"
    "var x=parseInt(a[0],10),y=parseInt(a[1],10);"
    "if(!isNaN(x)&&!isNaN(y)){"
    "b.style.left=Math.max(0,Math.min(x,window.innerWidth-64))+'px';"
    "b.style.top=Math.max(0,Math.min(y,window.innerHeight-64))+'px';"
    "b.style.right='auto';b.style.bottom='auto';}}}catch(err){}"
    "}"
    "var r=document.getElementById('__dsh_update_ring');"
    "var p=document.getElementById('__dsh_update_pct');"
    "if(r&&p){"
    "r.setAttribute('stroke-dashoffset',String(163.36*(1-__PCT__/100)));"
    "p.textContent='__PCT__%';"
    "}"
    "})();"
)

UPDATE_BALL_REMOVE_JS = (
    "(function(){var b=document.getElementById('__dsh_update_ball');"
    "if(b&&b.parentNode)b.parentNode.removeChild(b);})();"
)


# ---------------------------------------------------------------- 首次安装向导（无捆绑 DSH 时）

SETUP_ESTIMATE_BYTES = 120 * 1048576  # dsh 依赖树估算体积（进度参照，封顶 95%）


def run_setup_gui(dsh_dir: Path) -> None:
    """无捆绑 DSH 时的安装向导：协议服务托管 web/ 引导页，选源 → 下载 → 重启。

    页面交互：jade.invoke('startInstall', {registry}) 触发安装；
    Python 通过 execute_js 调 window.__dshInstallUpdate({pct/phase/error/version}) 推进度。
    """
    from jadeui import JadeUIApp, Window, IPCManager  # 延迟导入
    from jadeui.server import LocalServer

    web_dir = base_dir() / "web"
    if not web_dir.is_dir():
        print("[setup] 缺少 web/ 引导页目录: %s" % web_dir, file=sys.stderr)
        return

    app = JadeUIApp()
    ipc = IPCManager()
    ctx = {"window": None, "installing": False, "gui_exiting": False}

    def push(payload: dict) -> None:
        win = ctx.get("window")
        if win is None:
            return
        try:
            win.execute_js(
                "window.__dshInstallUpdate&&window.__dshInstallUpdate(%s)"
                % json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            print("[setup] 进度推送失败: %s" % e, flush=True)

    def install_progress_thread(stop_flag: dict) -> None:
        """按暂存目录体积估算百分比，每 2 秒推送一次。"""
        while not stop_flag.get("stop"):
            time.sleep(2)
            if stop_flag.get("stop"):
                return
            try:
                newest = newest_stage_dir(dsh_dir)
                total = dir_size_bytes(newest) if newest is not None else 0
                push({"phase": "install",
                      "pct": min(95, int(total * 100 / SETUP_ESTIMATE_BYTES))})
            except Exception:
                pass

    def install_worker(registry: str) -> None:
        if ctx["installing"]:
            return
        ctx["installing"] = True
        stop_flag = {"stop": False}
        threading.Thread(target=install_progress_thread,
                         args=(stop_flag,), daemon=True).start()
        try:
            os.environ["DSH_NPM_REGISTRY"] = registry
            push({"phase": "prepare", "pct": 0})
            # 优先用捆绑 runtime 的 node/npm（分发环境无系统 Node 也能装）
            node_exe = find_node()
            print("[setup] 开始安装 DSH (registry=%s, node=%s)"
                  % (registry, node_exe), flush=True)
            new = updater.install_update(dsh_dir, node_exe=node_exe)
            push({"phase": "done", "pct": 100, "version": new})
            print("[setup] 安装完成: %s，1.5s 后重启" % new, flush=True)
            threading.Timer(1.5, relaunch).start()
        except Exception as e:
            print("[setup] 安装失败: %s" % e, file=sys.stderr, flush=True)
            push({"error": str(e)})
        finally:
            stop_flag["stop"] = True
            ctx["installing"] = False

    @app.on_ready
    def on_ready():
        # 协议服务在初始化后启动，随后创建窗口（与官方 server.start → Window 示例一致）
        server = LocalServer()
        base_url = server.start("dshsetup", str(web_dir))
        print("[setup] 引导页协议服务: %s" % base_url, flush=True)

        def node_ok() -> bool:
            try:
                find_node()
                return True
            except RuntimeError:
                return False

        # IPC handler 必须在 initialize 之后注册（见 run_gui 内同类注释：规避 rebind 野指针 bug）
        @ipc.on("startInstall")
        def handle_start_install(window_id, message):
            try:
                data = json.loads(message or "{}")
                registry = str(data.get("registry", "")).strip()
                if not registry:
                    push({"error": "未收到有效的 registry 地址"})
                    return 1
                threading.Thread(target=install_worker,
                                 args=(registry,), daemon=True).start()
            except Exception as e:
                push({"error": "启动安装失败: %s" % e})
            return 1

        @ipc.on("recheckNode")
        def handle_recheck_node(window_id, message):
            ok = node_ok()
            print("[setup] 重新检测 Node: %s" % ("可用" if ok else "仍缺失"), flush=True)
            try:
                ctx["window"].execute_js(
                    "window.__dshNodeStatus&&window.__dshNodeStatus(%s)"
                    % ("1" if ok else "0"))
            except Exception:
                pass
            return 1

        window = Window(
            title="DSH Client · 初始化",
            width=520, height=640,
            min_width=520, min_height=640,
            resizable=True,
            url="%s/index.html?node=%s" % (base_url, "1" if node_ok() else "0"),
        )
        ctx["window"] = window

        @window.on_new_window
        def on_new_window(new_url: str, frame_name: str):
            # 外部链接（nodejs.org 下载页等）交给系统浏览器
            if new_url.startswith("http://") or new_url.startswith("https://"):
                webbrowser.open(new_url)
            return True

        window.show()

    @app.on_window_all_closed
    def on_all_closed():
        print("[setup] 向导窗口已关闭")
        app.quit()

    log_dir = base_dir() / "logs"
    data_dir = base_dir() / "jadeview-data"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        for old_cache in data_dir.glob("JadeUI_*_data"):
            shutil.rmtree(old_cache, ignore_errors=True)
        log_file = str(log_dir / "jadeview.log")
    except Exception:
        log_file = None
    try:
        # app_name 固定：保证 WebView2 数据目录稳定（与主界面一致）
        app.initialize(log_file=log_file, data_directory=str(data_dir),
                       app_name="DSHClient")
        app.run()
    finally:
        ctx["gui_exiting"] = True


# ---------------------------------------------------------------- GUI（JadeUI）

def run_gui(url: str, ctx: dict, dev_tools: bool) -> None:
    from jadeui import (JadeUIApp, Window, Theme, Events, Notification,
                        IPCManager)  # 延迟导入

    # 输出加载的 DLL 版本，便于确认修复版（v2.3.2 / v2.4.0-beta.2）已生效
    try:
        from jadeui.core import DLLManager
        _dm = DLLManager()
        if _dm.is_loaded() and _dm.has_function("jadeview_version"):
            import ctypes
            buf = ctypes.create_string_buffer(256)
            if _dm.jadeview_version(buf, ctypes.sizeof(buf)) == 1:
                print("[gui] JadeView DLL 版本: %s" % buf.value.decode("utf-8", errors="replace"), flush=True)
    except Exception as e:
        print("[gui] 读取 DLL 版本失败: %s" % e, flush=True)

    app = JadeUIApp()
    # 注意：不要在 app.initialize() 之前注册 IPC handler！
    # jadeui 会在 JadeView_init 后调用 IPCManager.rebind_all()，而 rebind 会
    # 释放旧 ctypes 回调（libffi 蹦床），但 JadeView DLL 的注册表是"追加式"的，
    # invoke 分发又取 .first()（最旧的指针）——结果就是跳进已释放的可执行内存
    # （实测：0xc0000005 @ 可执行堆地址 / 0xc0000409 Rust abort）。
    # 因此 handler 必须在 on_ready（初始化之后）注册，只注册一次，避开该 bug。
    ipc = IPCManager()

    def apply_overlay_style(win: "Window", theme: str) -> None:
        """标题栏按钮样式（高度 48px 与拖拽区一致 + 主题配色）。"""
        if theme == "dark":
            win.set_titlebar_overlay(48, "#e8e8e8", "#404040")
        else:
            win.set_titlebar_overlay(48, "#262626", "#dcdcdc")

    def register_theme_ipc() -> None:
        """在 on_ready（JadeView_init 之后）注册主题 IPC handler。
        修复版 DLL（v2.3.2 / v2.4.0-beta.2）中 register_ipc_handler 为替换式
        注册，且 set_titlebar_overlay 走通道投递（任意线程安全），因此可以
        直接在回调里应用样式；窗口尚未创建时先暂存，on_page_loaded 兜底。"""
        @ipc.on("setTitlebarTheme")
        def handle_theme(window_id, message):
            try:
                data = json.loads(message)
                theme = str(data.get("theme", ""))
                if theme not in ("dark", "light"):
                    return 1
                print("[gui] 收到主题: %s" % theme, flush=True)
                win = ctx.get("window")
                if win is not None:
                    apply_overlay_style(win, theme)
                    print("[gui] 标题栏样式已跟随主题: %s" % theme, flush=True)
                else:
                    ctx["pending_theme"] = theme
            except Exception as e:
                print("[gui] 主题消息处理失败: %s" % e, flush=True)
            return 1

    def apply_pending_theme() -> None:
        """在 DLL 派发的事件回调里应用待处理主题（只允许在此线程上下文调用窗口 API）。"""
        pending = ctx.get("pending_theme")
        win = ctx.get("window")
        if pending and win is not None:
            try:
                apply_overlay_style(win, pending)
                print("[gui] 标题栏样式已跟随主题: %s" % pending, flush=True)
            except Exception as e:
                print("[gui] 应用标题栏样式失败: %s" % e, flush=True)
            ctx["pending_theme"] = None

    def notify(title: str, body: str) -> None:
        try:
            Notification.show(title, body)
        except Exception as e:
            print("[update] 通知失败: %s" % e)

    def notify_buttons(title: str, body: str, ok: str, cancel: str, action: str) -> None:
        try:
            Notification.with_buttons(title, body, ok, cancel, action=action)
        except Exception as e:
            print("[update] 通知失败: %s" % e)

    @Notification.on(Events.NOTIFICATION_ACTION)
    def on_notification_action(data):
        # DLL：点击通知本体 → {"action":"clicked",...}；点击按钮 → {"action":"action_0/1",...}
        try:
            action = str((data or {}).get("action", ""))
            arguments = str((data or {}).get("arguments", ""))
            pending = ctx.setdefault("pending_asks", {})
            if arguments and action in ("action_0", "action_1"):
                # desktop_ask 按钮：回写选择给 DSH 插件
                if pending.pop(arguments, None):
                    choice = "ok" if action == "action_0" else "cancel"
                    respond_to_ask(arguments, choice)
                    print("[bridge] desktop_ask 应答: %s -> %s" % (arguments, choice), flush=True)
                    win = ctx.get("window")
                    if win is not None:
                        if getattr(win, "is_minimized", False):
                            win.restore()
                        win.focus()
                return
            if action == "clicked":
                win = ctx.get("window")
                if win is not None:
                    if getattr(win, "is_minimized", False):
                        win.restore()
                    win.focus()
                    print("[bridge] 通知点击 -> 已唤起窗口", flush=True)
        except Exception as e:
            print("[bridge] 通知点击处理失败: %s" % e, flush=True)

    # ---------------------------------------------------------------- 事件桥（DSH client-bridge 插件）

    def short_session(session_id) -> str:
        s = str(session_id or "")
        return s[-8:] if len(s) > 8 else s

    def handle_bridge_event(ev: dict) -> None:
        kind = ev.get("kind")
        win = ctx.get("window")
        if win is None:
            return
        try:
            if kind == "agent-running":
                win.set_progress(0, 1)  # 任务栏进度：不确定态
                # 动态窗口标题：页面标题加「思考中」前缀
                win.execute_js(
                    "document.title='\\u25cf \\u601d\\u8003\\u4e2d \\u00b7 '"
                    "+(document.title||'').replace(/^\\u25cf \\u601d\\u8003\\u4e2d \\u00b7 /,'')")
                print("[bridge] agent-running -> 进度+标题", flush=True)
            elif kind == "agent-idle":
                win.set_progress(0, 0)
                win.execute_js(
                    "document.title=(document.title||'').replace(/^\\u25cf \\u601d\\u8003\\u4e2d \\u00b7 /,'')")
                focused = bool(getattr(win, "is_focused", True))
                if not focused:
                    notify("DeepSeek Harness", "回复完成 · 会话 %s" % short_session(ev.get("sessionId")))
                    win.flash(2)
                print("[bridge] agent-idle", flush=True)
            elif kind == "progress":
                value = int(ev.get("value", 0))
                value = max(0, min(100, value))
                win.set_progress(value, 2)  # 真实百分比（NORMAL 态）
            elif kind == "agent-error":
                notify("DeepSeek Harness", "出错: %s" % str(ev.get("message", ""))[:120])
                print("[bridge] agent-error", flush=True)
            elif kind == "subagent-end":
                notify("DeepSeek Harness", "子任务完成 · 会话 %s" % short_session(ev.get("sessionId")))
                print("[bridge] subagent-end", flush=True)
            elif kind == "desktop-notify":
                notify(str(ev.get("title", "")), str(ev.get("body", "")))
                print("[bridge] desktop-notify", flush=True)
            elif kind == "desktop-ask":
                rid = str(ev.get("requestId", ""))
                if rid:
                    ctx.setdefault("pending_asks", {})[rid] = True
                    notify_buttons(
                        str(ev.get("title", "")),
                        str(ev.get("body", "")),
                        str(ev.get("button1", "确认")),
                        str(ev.get("button2", "取消")),
                        action=rid,
                    )
                print("[bridge] desktop-ask %s" % rid, flush=True)
        except Exception as e:
            print("[bridge] 原生动作失败: %s" % e, flush=True)

    def respond_to_ask(request_id: str, choice: str) -> None:
        try:
            body = json.dumps({"requestId": request_id, "choice": choice}).encode("utf-8")
            req = urllib.request.Request(
                ctx["bridge_url"].rstrip("/") + "/client-bridge/respond",
                data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except Exception as e:
            print("[bridge] respond 回写失败: %s" % e, flush=True)

    def start_bridge_poller() -> None:
        """轮询 DSH 的 /client-bridge/events 端点，把 agent 生命周期事件转成
        原生通知 / 任务栏进度 / 窗口闪烁。"""
        if not ctx.get("notify"):
            return
        endpoint = ctx["bridge_url"].rstrip("/") + "/client-bridge/events"
        since = -1  # 首次响应只对齐 seq，不回放历史事件
        while not ctx.get("gui_exiting"):
            time.sleep(1.2)
            try:
                with urllib.request.urlopen(
                        "%s?since=%d" % (endpoint, max(since, 0)), timeout=2.5) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception:
                continue  # 后端未就绪或插件未加载：静默重试
            try:
                latest = int(data.get("seq", 0))
            except (TypeError, ValueError):
                continue
            if since < 0:
                since = latest
                continue
            for ev in data.get("events") or []:
                try:
                    if int(ev.get("seq", 0)) > since:
                        handle_bridge_event(ev)
                except Exception as e:
                    print("[bridge] 事件处理失败: %s" % e, flush=True)
            since = latest

    def update_progress_overlay(dsh_dir, stop_flag: dict) -> None:
        """更新期间在页面注入悬浮球（环形进度），每 2 秒刷新。

        以当前已安装 node_modules 的体积为 100% 参照（新旧依赖树相近），
        统计暂存目录已写入体积换算百分比；封顶 95%，避免参照误差导致
        "提前 100%"。更新完成/失败由调用方移除悬浮球。
        """
        ref = 0
        try:
            ref = dir_size_bytes(dsh_dir / "node_modules")
        except Exception:
            pass
        if ref <= 0:
            ref = 120 * 1048576  # 兜底参照 120MB
        while not stop_flag.get("stop"):
            time.sleep(2)
            try:
                win = ctx.get("window")
                if win is None:
                    continue
                newest = newest_stage_dir(dsh_dir)
                total = dir_size_bytes(newest) if newest is not None else 0
                pct = min(95, int(total * 100 / ref))
                win.execute_js(UPDATE_BALL_JS.replace("__PCT__", str(pct)))
            except Exception:
                pass  # 进度显示失败不影响更新本身

    def update_worker() -> None:
        if ctx["updating"]:
            return
        ctx["updating"] = True
        try:
            inst = updater.installed_version(ctx["dsh_dir"])
            if inst is None:
                notify("无法更新", "未找到捆绑的 DSH（dsh/ 目录）。请先按 README 准备捆绑目录。")
                return
            latest = updater.latest_version()
            if not updater.update_available(inst, latest):
                notify("已是最新", "DeepSeek Harness %s 已是最新版。" % inst)
                return
            notify("正在更新", "DeepSeek Harness %s -> %s，请稍候..." % (inst, latest))
            had_child = ctx["child"] is not None
            if had_child:
                stop_child(ctx["child"])
                ctx["child"] = None
            stop_flag = {"stop": False}
            threading.Thread(
                target=update_progress_overlay,
                args=(ctx["dsh_dir"], stop_flag), daemon=True).start()
            try:
                new = updater.install_update(ctx["dsh_dir"], node_exe=ctx["node"])
            finally:
                stop_flag["stop"] = True
                try:
                    w = ctx.get("window")
                    if w is not None:
                        w.execute_js(UPDATE_BALL_REMOVE_JS)
                except Exception:
                    pass
            if ctx["spawned"]:
                notify("更新完成", "已更新至 %s，正在重启客户端..." % new)
                threading.Timer(1.5, relaunch).start()
            else:
                notify("更新完成",
                       "已更新至 %s。当前附着的是外部 DSH 实例，下次独立启动时生效。" % new)
        except Exception as e:
            print("[update] 更新失败: %s" % e, file=sys.stderr)
            notify("更新失败", str(e))
            # 恢复自己拉起的后端（若因更新而停掉）
            if ctx["spawned"] and ctx["child"] is None:
                try:
                    url2, child2, _sp2, _home2 = start_backend(ctx["port"], ctx["dsh_dir"])
                    ctx["child"] = child2
                    w = ctx.get("window")
                    if w is not None:
                        w.load_url(url2)
                except Exception as e2:
                    print("[update] 后端恢复失败: %s" % e2, file=sys.stderr)
        finally:
            ctx["updating"] = False

    def auto_check() -> None:
        time.sleep(3)  # 让开启动峰值
        try:
            if not ctx["auto_update"]:
                return
            inst = updater.installed_version(ctx["dsh_dir"])
            if inst is None:
                return  # 没有捆绑后端，不打扰
            latest = updater.latest_version()
            if updater.update_available(inst, latest):
                notify_buttons(
                    "DeepSeek Harness 有更新",
                    "当前 %s -> 最新 %s" % (inst, latest),
                    "立即更新", "忽略", action="dsu",
                )
        except Exception as e:
            print("[update] 版本检查失败: %s" % e)

    @Notification.on(Events.NOTIFICATION_ACTION)
    def on_action(data):
        try:
            if data.get("arguments") == "dsu":
                threading.Thread(target=update_worker, daemon=True).start()
        except Exception as e:
            print("[update] 通知回调处理失败: %s" % e)

    @app.on_ready
    def on_ready():
        register_theme_ipc()  # 初始化后注册 IPC，规避 SDK rebind 野指针 bug

        window_kwargs = dict(
            title="",  # 空标题：JadeView 自动跟随页面标题（DSH 插件会动态改 document.title）
            width=1280,
            height=820,
            min_width=940,
            min_height=600,
            resizable=True,
            theme=Theme.SYSTEM,
            url=url,
            use_page_icon=ctx["page_icon"],  # 窗口图标跟随页面图标
            preload_js=build_preload_js(
                ctx["icon"].base if ctx["icon"] is not None else None,
                ctx["title_overlay"]),
        )
        if ctx["title_overlay"]:
            # 按文档《自定义标题栏》：内置按钮覆盖层，页面配合拖拽区适配
            window_kwargs["frame_style"] = "title-overlay"
        window = Window(**window_kwargs)
        ctx["window"] = window

        # 诊断日志：观察页面图标相关事件（确认 WebView2 是否上报 favicon 变化）
        for event_name in ("FAVICON_UPDATED", "WEBVIEW_PAGE_FAVICON_UPDATED",
                           "WEBVIEW_PAGE_ICON_UPDATED", "UPDATE_WINDOW_ICON"):
            try:
                event = getattr(Events, event_name, None)
                if event is None:
                    continue

                @window.on(event)
                def on_icon(payload, _name=event_name):
                    print("[gui] 图标事件 %s: %s" % (_name, payload), flush=True)
            except Exception as e:
                print("[gui] 图标事件 %s 注册失败: %s" % (event_name, e), flush=True)

        @window.on_new_window
        def on_new_window(new_url: str, frame_name: str):
            # 拦截页面弹窗：外部链接交给系统浏览器，避免在壳里迷路
            if new_url.startswith("http://") or new_url.startswith("https://"):
                webbrowser.open(new_url)
            return True

        @window.on_closing
        def on_closing():
            # 关闭按钮 -> 隐藏到托盘；仅托盘「退出」允许真正关闭
            if not ctx.get("allow_close"):
                window.hide()
                return True
            return False

        @window.on_page_loaded
        def on_page_loaded(page_url: str):
            print("[gui] 页面加载完成: %s" % page_url)
            apply_pending_theme()

        window.show()
        if ctx["title_overlay"]:
            # 初始只设高度（48px 与拖拽区一致）；配色等页面主题推送后
            # 在 on_page_loaded 里一次到位（其他时机调用会触发 DLL 崩溃，实测）
            try:
                window.set_titlebar_overlay(48)
                print("[gui] 初始标题栏高度已应用 (48px)", flush=True)
            except Exception as e:
                print("[gui] 初始标题栏样式应用失败: %s" % e, flush=True)
        threading.Thread(target=auto_check, daemon=True).start()
        threading.Thread(target=start_bridge_poller, daemon=True).start()

        # 系统托盘
        try:
            from jadeui import Tray
            tray = Tray()
            if tray.id:
                icon_path = str(base_dir() / "assets" / "favicon.ico")
                tray.set_icon(icon_path)
                tray.set_tooltip(APP_NAME)

                def tray_show():
                    w = ctx.get("window")
                    if w is not None:
                        if getattr(w, "is_minimized", False):
                            w.restore()
                        w.focus()

                def tray_quit():
                    w = ctx.get("window")
                    if w is not None:
                        ctx["allow_close"] = True
                        w.close()  # 触发 on_window_all_closed → app.quit()

                tray.set_menu([
                    {"key": "show", "label": "显示主窗口", "on_click": tray_show},
                    {"type": "separator", "key": "sep1"},
                    {"key": "quit", "label": "退出", "dangerous": True, "on_click": tray_quit},
                ])
                tray.show()
                ctx["tray"] = tray
                print("[gui] 系统托盘已创建", flush=True)
        except Exception as e:
            print("[gui] 托盘创建失败: %s" % e, flush=True)

    @app.on_window_all_closed
    def on_all_closed():
        print("[gui] 所有窗口已关闭")
        if ctx["child"] is not None:
            stop_child(ctx["child"])
            ctx["child"] = None
        app.quit()

    log_dir = base_dir() / "logs"
    data_dir = base_dir() / "jadeview-data"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        # 清理旧版随机缓存目录：app_name 未固定时 SDK 每次启动随机生成
        # JadeUI_*_data，且 SDK 的自动清理只扫 %TEMP%\JadeUI，不会回收这里
        for old_cache in data_dir.glob("JadeUI_*_data"):
            shutil.rmtree(old_cache, ignore_errors=True)
        log_file = str(log_dir / "jadeview.log")
    except Exception:
        log_file = None
    try:
        # app_name 固定为 DSHClient：WebView2 数据目录为 jadeview-data/<app_name>_data；
        # 不传时 SDK 每次启动用随机 UUID → 缓存目录每次都变，缓存/登录态全丢
        app.initialize(enable_dev_tools=dev_tools, log_file=log_file,
                       data_directory=str(data_dir), app_name="DSHClient")
        app.run()
    finally:
        ctx["gui_exiting"] = True
        icon_server = ctx.get("icon")
        if icon_server is not None:
            icon_server.stop()


# ---------------------------------------------------------------- 入口

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DeepSeek Harness 桌面客户端 (JadeUI)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="DSH Web 服务起始端口（默认 %d；被占则自动顺延到空闲端口）"
                        % DEFAULT_PORT)
    p.add_argument("--check-backend", action="store_true",
                   help="只检查/启动后端并打印结果，不打开窗口")
    p.add_argument("--check-preset", action="store_true",
                   help="只向客户端自己的 dsh-home 注入预设并校验结果，不打开窗口")
    p.add_argument("--check-update", action="store_true",
                   help="只查询捆绑 DSH 的当前/最新版本并打印，不安装")
    p.add_argument("--update-now", action="store_true",
                   help="立即把捆绑 DSH 安装/更新到最新版（无窗口）")
    p.add_argument("--no-auto-update", action="store_true",
                   help="关闭启动时的后台版本检查")
    p.add_argument("--no-preset", action="store_true",
                   help="不自动注入 dsh-client 预设（默认注入：复制 standard + client-bridge 行，"
                        "并把默认预设切到 dsh-client）")
    p.add_argument("--attach", action="store_true",
                   help="附着到 --port 上已在运行的 DSH（旧行为）。默认关闭：总是启动自己"
                        "捆绑的 DSH（独立数据目录 dsh-home，端口被占自动顺延）")
    p.add_argument("--no-title-overlay", action="store_true",
                   help="关闭 title-overlay 标题栏叠加（默认开启）")
    p.add_argument("--no-page-icon", action="store_true",
                   help="关闭窗口图标跟随页面（默认开启；打包后用 exe 图标时可关闭）")
    p.add_argument("--no-notify", action="store_true",
                   help="关闭事件桥原生通知/闪烁（默认开启，需 DSH 加载 client-bridge 插件）")
    p.add_argument("--dev-tools", action="store_true",
                   help="启用 WebView 开发者工具 (F12)")
    p.add_argument("--dsh-dir", default=str(base_dir() / "dsh"),
                   help="捆绑 DSH 目录（默认 ./dsh）")
    p.add_argument("--dsh-entry", help="覆盖 @deepseek-ai/dsh/lib/bin.js 路径")
    p.add_argument("--node", help="覆盖 Node.js 可执行文件路径")
    return p.parse_args()


def main() -> None:
    # 输出统一 UTF-8，避免被管道/重定向按 GBK 解码出现乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 重启衔接：旧进程刚退出时先睡几秒，避免单实例锁误判"已在运行"
    delay = os.environ.get(RELAUNCH_DELAY_ENV)
    if delay:
        try:
            time.sleep(float(delay))
        except ValueError:
            pass

    args = parse_args()
    if args.dsh_entry:
        os.environ["DSH_ENTRY"] = args.dsh_entry
    if args.node:
        os.environ["DSH_NODE"] = args.node

    dsh_dir = Path(args.dsh_dir)
    node_exe = os.environ.get("DSH_NODE") or None

    # ---- 无窗口的更新 CLI 模式 ----
    if args.check_update:
        inst = updater.installed_version(dsh_dir)
        latest = updater.latest_version()
        print("installed: %s" % (inst or "none"))
        print("latest:    %s" % latest)
        if inst is None:
            print("UPDATE_AVAILABLE yes (尚未安装，可运行 --update-now 安装)")
        elif updater.update_available(inst, latest):
            print("UPDATE_AVAILABLE yes")
        else:
            print("UPDATE_AVAILABLE no")
        return

    if args.update_now:
        inst = updater.installed_version(dsh_dir)
        latest = updater.latest_version()
        if inst is None:
            print("安装 DeepSeek Harness %s ..." % latest)
            new = updater.install_update(dsh_dir, node_exe=node_exe)
            print("INSTALLED %s" % new)
        elif updater.update_available(inst, latest):
            print("更新 %s -> %s ..." % (inst, latest))
            new = updater.install_update(dsh_dir, node_exe=node_exe)
            print("UPDATED %s" % new)
        else:
            print("已是最新: %s" % inst)
        return

    # ---- 无窗口的预设自检模式 ----
    if args.check_preset:
        entry = find_dsh_entry(dsh_dir)
        if entry is None:
            print("PRESET_FAIL 未找到 DSH 入口")
            return
        ok = ensure_client_preset(entry, client_home())
        if ok:
            home = client_home()
            comp = home / ".agent-presets" / CLIENT_PRESET_ID / "agent.cordis.yml"
            settings = home / "settings.yaml"
            print("PRESET_OK home=%s composition=%s" % (home, comp.is_file()))
            print("settings=%s" % settings.is_file())
            if settings.is_file():
                text = settings.read_text(encoding="utf-8")
                m = re.search(r"agent-presets:\s*\n\s*default:\s*(\S+)", text)
                print("default=%s" % (m.group(1) if m else "?"))
        else:
            print("PRESET_FAIL 注入失败")
        return

    # ---- GUI 模式 ----
    # 单实例锁先行：已有实例则直接退出，避免二次启动拉起孤儿后端
    _singleton = acquire_singleton()

    # 首次运行（无捆绑 DSH）：进入安装向导——协议服务托管引导页，
    # 页面上选 npm 源并下载 DSH，完成后自动重启进入正常流程
    if not args.attach and find_dsh_entry(dsh_dir) is None:
        print("[setup] 未检测到捆绑 DSH，进入安装向导", flush=True)
        run_setup_gui(dsh_dir)
        return

    # 预设注入（默认：注入自己的 dsh-home，同步且先于后端启动——新会话创建时
    # 预设必须已在位；幂等，更新 DSH 后下次启动自动重新注入）
    if not args.no_preset and not args.attach:
        try:
            _entry = find_dsh_entry(dsh_dir)
            if _entry is not None:
                ensure_client_preset(_entry, client_home())
        except Exception as e:
            print("[preset] 本地注入失败: %s" % e, flush=True)

    url, child, spawned, home = start_backend(args.port, dsh_dir, attach=args.attach)

    if args.check_backend:
        print("BACKEND_OK url=%s spawned=%s home=%s" % (url, spawned, home or "-"))
        if spawned:
            stop_child(child)
        return

    # --attach 模式：后端就绪后通过其 API 注入（对方部署的 roots/settings 都正确）
    if args.attach and not args.no_preset:
        threading.Thread(target=preset_inject_worker, args=(url, dsh_dir), daemon=True).start()

    icon_server = IconHttpServer() if not args.no_page_icon else None
    ctx = {
        "child": child,
        "spawned": spawned,
        "dsh_dir": dsh_dir,
        "node": node_exe,
        "port": args.port,
        "auto_update": not args.no_auto_update,
        "notify": not args.no_notify,
        "title_overlay": not args.no_title_overlay,
        "page_icon": not args.no_page_icon,
        "updating": False,
        "window": None,
        "icon": icon_server,
        "pending_theme": None,
        "gui_exiting": False,
        "bridge_url": url,
        "pending_asks": {},
        "tray": None,
        "allow_close": False,
        "pending_show": False,
    }

    start_singleton_server(_singleton, ctx)  # 接收二次启动的 show 指令
    try:
        run_gui(url, ctx, args.dev_tools)
    finally:
        if ctx["child"] is not None:
            stop_child(ctx["child"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("启动失败: %s" % e, file=sys.stderr)
        sys.exit(1)
