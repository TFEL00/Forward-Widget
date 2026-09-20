#!/usr/bin/env python3
"""
从 unpkg 拉取 @rexnow/danmu-universal 的最新构建产物，套用自定义文案后
写入 widgets/danmu-universal.js。

设计要点：
- 先查 npm registry 拿 latest 版本号，失败则退回解析 unpkg 重定向
- 下载 dist 产物，校验非空、非 HTML 错误页、体积合理、含 WidgetMetadata
- 套用自定义 title / description（上游自带推广性质的描述文案）
- 每次都重新下载并比对最终字节，避免因版本号未变而漏掉文案修改
- 任何一步失败都以非 0 退出，但**不覆盖**已有文件（仓库里始终保持可用版本）

用法: python3 scripts/update_danmu.py
"""
import json
import os
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
MODULE_ID = "baranwang.danmu.universal"
UA = {"User-Agent": "TFEL00-Forward-Widget-updater/1.0"}

# 自定义文案（上游原描述为「通用弹幕插件，支持腾讯、优酷、爱奇艺、哔哩哔哩、人人视频等平台」）
CUSTOM_TITLE = "通用弹幕"
CUSTOM_DESC = "支持从多个主流视频平台获取弹幕数据"


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


def metadata_head(text, span=1200):
    """取 WidgetMetadata 定义段，避免在全文中误匹配同名字段。"""
    if "WidgetMetadata = {" not in text:
        raise ValueError("缺少 WidgetMetadata 定义")
    i = text.index("WidgetMetadata = {")
    return text[i:i + span]


def patch_metadata(text):
    """把模块标题与描述替换为自定义文案（仅改 WidgetMetadata 段内的字段）。"""
    idx = text.index("WidgetMetadata = {")
    head, tail = text[idx:idx + 1200], text[idx + 1200:]

    if not re.search(r'id:\s*"' + re.escape(MODULE_ID) + r'"', head):
        raise ValueError("WidgetMetadata 段内未找到目标 id，补丁位置可能已变")

    new_head, n1 = re.subn(r'title:\s*"(?:[^"\\]|\\.)*"', f'title: "{CUSTOM_TITLE}"',
                           head, count=1)
    new_head, n2 = re.subn(r'description:\s*"(?:[^"\\]|\\.)*"',
                           f'description: "{CUSTOM_DESC}"', new_head, count=1)
    if n1 != 1 or n2 != 1:
        raise ValueError(f"补丁失败: title={n1} description={n2}")

    # 结构性复核：补丁只应改动字符串内容，不应改变括号数量
    if (head.count("{"), head.count("}")) != (new_head.count("{"), new_head.count("}")):
        raise ValueError("补丁后花括号数量发生变化")

    print(f"[ok] 已套用自定义文案: {CUSTOM_TITLE}")
    return text[:idx] + new_head + tail


def write_outputs(**kv):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def main():
    version = get_latest_version()
    if not version:
        print("[error] 无法获取最新版本号", file=sys.stderr)
        return 1
    print(f"[info] unpkg latest version = {version}")

    url = f"{UNPKG_ROOT}@{version}/dist/danmu-universal.js"
    print(f"[info] 下载 {url}")
    try:
        body = fetch(url)
        validate(body, version)
        text = patch_metadata(body.decode("utf-8", errors="surrogateescape"))
        new_bytes = text.encode("utf-8", errors="surrogateescape")
    except Exception as e:
        # 关键：失败时保留旧文件，避免把仓库改坏
        print(f"[error] 拉取、校验或套用文案失败，保留原有文件: {e}", file=sys.stderr)
        return 1

    old = OUT_JS.read_bytes() if OUT_JS.exists() else b""
    if new_bytes == old:
        print(f"[skip] {version} 内容与现有文件一致（文案已是最新），无需更新")
        OUT_VER.write_text(version + "\n")
        write_outputs(status="skipped", changed="false")
        return 0

    OUT_JS.write_bytes(new_bytes)
    OUT_VER.write_text(version + "\n")
    print(f"[done] 已更新到 {version}（{len(new_bytes)} 字节）")
    write_outputs(status="ok", changed="true", version=version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
