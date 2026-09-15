#!/usr/bin/env python3
"""
从 unpkg 拉取 @rexnow/danmu-universal 的最新构建产物，写入 widgets/danmu-universal.js。

设计要点：
- 先查 npm registry 拿 latest 版本号（unpkg 根路径 302 到带版本号的地址，可从中解析）
- 下载 dist 产物，校验非空、非 HTML 错误页、体积合理
- 任何一步失败都以非 0 退出，但**不覆盖**已有文件（保证仓库里始终是可用的旧版本）
- 版本号写入 widgets/danmu-universal.version（便于工作流与 .fwd 对齐）

用法: python3 scripts/update_danmu.py
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

PKG = "@rexnow/danmu-universal"
REGISTRY = f"https://registry.npmjs.org/{PKG}/latest"
UNPKG_ROOT = f"https://unpkg.com/{PKG}"
OUT_JS = Path("widgets/danmu-universal.js")
OUT_VER = Path("widgets/danmu-universal.version")
MIN_SIZE = 100_000          # 合理下限，防止抓到错误页
UA = {"User-Agent": "TFEL00-Forward-Widget-updater/1.0"}


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_latest_version():
    """优先用 npm registry；失败则退回到解析 unpkg 的重定向地址。"""
    try:
        data = json.loads(fetch(REGISTRY, timeout=60).decode("utf-8"))
        v = data.get("version")
        if v:
            return v
    except Exception as e:
        print(f"[warn] registry 查询失败: {e}", file=sys.stderr)

    # 回退：从 https://unpkg.com/pkg@1.2.3/dist/... 解析
    try:
        req = urllib.request.Request(UNPKG_ROOT, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            final = r.geturl()
        m = re.search(re.escape(PKG) + r"@([0-9][^/]*)", final)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"[warn] unpkg 重定向解析失败: {e}", file=sys.stderr)

    return None


def validate(body: bytes, version: str):
    if len(body) < MIN_SIZE:
        raise ValueError(f"体积异常: {len(body)} 字节 < {MIN_SIZE}")
    head = body[:400].lstrip()
    if head[:1] == b"<" or b"<!DOCTYPE" in head or b"<html" in head[:200].lower():
        raise ValueError("返回内容像 HTML 错误页，不是 JS")
    if b"WidgetMetadata" not in body:
        raise ValueError("内容中未找到 WidgetMetadata，疑似非模块文件")
    print(f"[ok] 校验通过: {len(body)} 字节, version={version}")


def main():
    version = get_latest_version()
    if not version:
        print("[error] 无法获取最新版本号", file=sys.stderr)
        return 1
    print(f"[info] unpkg latest version = {version}")

    old = OUT_JS.read_bytes() if OUT_JS.exists() else b""
    old_ver = OUT_VER.read_text().strip() if OUT_VER.exists() else ""

    if old_ver == version and old:
        print(f"[skip] 已是最新 {version}，无需更新")
        return 0

    url = f"{UNPKG_ROOT}@{version}/dist/danmu-universal.js"
    print(f"[info] 下载 {url}")
    try:
        body = fetch(url)
        validate(body, version)
    except Exception as e:
        # 关键：失败时保留旧文件，避免把仓库改坏
        print(f"[error] 拉取或校验失败，保留原有文件: {e}", file=sys.stderr)
        return 1

    if body == old:
        print("[skip] 内容与现有文件一致，仅同步版本号标记")
        OUT_VER.write_text(version + "\n")
        return 0

    OUT_JS.write_bytes(body)
    OUT_VER.write_text(version + "\n")
    # 供工作流读取，写出到 GITHUB_OUTPUT
    import os
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"version={version}\n")
            f.write("changed=true\n")
    print(f"[done] 已更新到 {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
