"""
DSH 捆绑后端的更新管理 —— 针对随客户端捆绑的 ./dsh 目录（npm 包 @deepseek-ai/dsh）。

职责：
  - 从 npm registry 查询最新版本（urllib，轻量）
  - 读取捆绑安装的当前版本
  - 在暂存目录执行 npm install，成功后原子换目录（失败不影响旧版）
  - 版本号比较

注意：只管理捆绑的 ./dsh；客户端"附着"到外部 DSH 实例时不影响该实例。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

DSH_PKG = "@deepseek-ai/dsh"
DEFAULT_REGISTRY = "https://registry.npmmirror.com"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def registry_url() -> str:
    return os.environ.get("DSH_NPM_REGISTRY", DEFAULT_REGISTRY).rstrip("/")


def latest_version(timeout: float = 20.0) -> str:
    """查询 registry 上 @deepseek-ai/dsh 的最新版本号。"""
    url = "%s/%s/latest" % (registry_url(), urllib.parse.quote(DSH_PKG, safe="@"))
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("查询版本失败 (HTTP %s): %s" % (e.code, url))
    version = str(data.get("version", "")).lstrip("v")
    if not version:
        raise RuntimeError("registry 返回异常: 缺少 version 字段 (%s)" % url)
    return version


def installed_version(dsh_dir: Path) -> Optional[str]:
    """读取捆绑安装的当前版本；未安装返回 None。"""
    pkg = dsh_dir / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if not pkg.is_file():
        return None
    try:
        with open(pkg, "r", encoding="utf-8") as f:
            return str(json.load(f).get("version", "")).lstrip("v")
    except Exception:
        return None


def parse_version(v: str) -> Tuple[int, int, int]:
    """语义化版本转元组用于比较；非法段按 0 处理。"""
    core = v.lstrip("v").split("-")[0].split(".")
    nums = [0, 0, 0]
    for i, part in enumerate(core[:3]):
        try:
            nums[i] = int(part)
        except ValueError:
            break
    return nums[0], nums[1], nums[2]


def update_available(current: Optional[str], latest: str) -> bool:
    return current is None or parse_version(latest) > parse_version(current)


# ---------------------------------------------------------------- npm 安装

def resolve_npm_cmd(node_exe: Optional[str] = None) -> list:
    """返回 npm 的启动命令前缀（列表）。

    优先使用捆绑的 Node 自带的 npm（runtime/node_modules/npm/bin/npm-cli.js），
    否则回退到系统 PATH 上的 npm / npm.cmd。
    """
    if node_exe:
        runtime = Path(node_exe).parent
        npm_cli = runtime / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if npm_cli.is_file():
            return [node_exe, str(npm_cli)]

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        if npm.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", npm]
        return [npm]

    node = node_exe or shutil.which("node")
    if node:
        npm_cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if npm_cli.is_file():
            return [node, str(npm_cli)]

    raise RuntimeError("未找到 npm。请安装 Node.js，或在 runtime/ 放完整的便携版 Node。")


def cleanup_stale(dsh_dir: Path) -> None:
    """清理上次更新遗留：孤儿 npm 进程 + 暂存/备份目录。

    更新卡死或客户端被强杀时，npm 子进程会残留（父进程已死，即使跑完
    也无人执行目录替换），且会锁住 npm 缓存——下一次更新时新旧两个 npm
    互相争锁、全部空转，这正是"一直在升级中"的根因。所以每次安装前先
    杀掉命令行带 @deepseek-ai/dsh 的 node/cmd 残留进程（此刻我们自己的
    npm 尚未启动，不会误杀），再删残留目录。
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='cmd.exe'\" | "
        "Where-Object { $_.CommandLine -like '*@deepseek-ai/dsh*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        print("[update] 清理残留 npm 进程失败（忽略）: %s" % e, flush=True)
    for pattern in ("dsh_stage_*", "dsh_old_*"):
        for p in dsh_dir.parent.glob(pattern):
            shutil.rmtree(p, ignore_errors=True)


def _npm_install(dest: Path, target: str, node_exe: Optional[str],
                 timeout: int) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / "package.json").is_file():
        (dest / "package.json").write_text(
            json.dumps({"name": "dsh-bundled", "version": "1.0.0", "private": True}),
            encoding="utf-8",
        )
    cmd = resolve_npm_cmd(node_exe) + [
        "install", target,
        "--registry", registry_url(),
        "--no-audit", "--no-fund",
    ]
    print("[update] %s  (cwd=%s)" % (" ".join(cmd), dest))
    proc = subprocess.Popen(
        cmd, cwd=str(dest), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=CREATE_NO_WINDOW,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 不能只杀直接子进程（cmd.exe）：node 孙进程会漏杀成孤儿，
        # 锁住 npm 缓存拖垮下一次更新，必须 /T 杀整棵进程树
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc.kill()
        raise RuntimeError("npm install 超时（%d 秒），已终止" % timeout)
    if proc.returncode != 0:
        tail = (err or out or "")[-1500:]
        raise RuntimeError("npm install 失败 (rc=%s):\n%s" % (proc.returncode, tail))
    if installed_version(dest) is None:
        raise RuntimeError("npm install 完成但未找到 %s 的 package.json" % DSH_PKG)


def install_update(dsh_dir: Path, version: Optional[str] = None,
                   node_exe: Optional[str] = None, timeout: int = 1800) -> str:
    """把捆绑 DSH 升级到最新版（或指定版本），返回新版本号。

    采用暂存目录 + 原子换目录：安装失败时旧版保持原样。
    """
    target = DSH_PKG + ("@" + version if version else "@latest")
    cleanup_stale(dsh_dir)  # 先清掉上次卡死留下的孤儿 npm 与残留目录
    stage = dsh_dir.parent / ("dsh_stage_%d" % (int(time.time() * 1000) % 1000000))
    backup = dsh_dir.parent / ("dsh_old_%d" % (int(time.time() * 1000) % 1000000))
    try:
        _npm_install(stage, target, node_exe, timeout)
        new_version = installed_version(stage)
        # 原子换目录
        if dsh_dir.exists():
            os.replace(dsh_dir, backup)
        os.replace(stage, dsh_dir)
        shutil.rmtree(backup, ignore_errors=True)
        print("[update] 完成，版本 %s" % new_version)
        return str(new_version)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
