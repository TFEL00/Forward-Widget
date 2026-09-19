#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从上游 huangxd-/danmu_api 构建「自动弹幕」Forward 插件脚本，写入 widgets/auto-danmu-v2.js。

背景
----
「自动弹幕」模块并非上游直接发布的成品，而是 danmu_api 项目用
build-forward-widget.js（esbuild）打包出的 dist/logvar-danmu.js。

稳定性策略
----------
1. 优先尝试 main 分支；构建或校验失败则自动回退到最近的 tag。
2. 任何一步失败都不覆盖仓库中的现有文件，脚本以非 0 退出。
3. 以「上游 ref 的 commit sha」判断是否需要更新，避免因构建产物的非确定性
   （依赖版本漂移）产生每周一次的无效提交。
4. 构建成功后套用自定义标题与描述，与现有模块保持一致。

用法: python3 scripts/update_auto_danmu.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UPSTREAM = "https://github.com/huangxd-/danmu_api.git"

OUT_JS = Path("widgets/auto-danmu-v2.js")
STATE = Path("widgets/auto-danmu-v2.source.json")
FWD = Path("TFEL.fwd")

MODULE_ID = "forward.auto.danmu2"
# 自定义文案：构建产物默认是「自动链接弹幕v2」并带推广码，这里统一覆盖
CUSTOM_TITLE = "自动弹幕"
CUSTOM_DESC = "自动获取播放链接并从服务器获取弹幕"

MIN_SIZE = 800_000         # 上游各版本产物在 0.9-1.4MB 区间
MAX_SIZE = 3_000_000
TAG_FALLBACKS = 2          # 回退时最多尝试几个最近的 tag

# 上游 main 曾因新增的服务端「本地弹幕」模块（依赖 node:fs/path/crypto 与
# fast-xml-parser）而无法在浏览器环境打包。下面这个插件把这两个模块替换为
# 空实现。Forward 环境本就没有文件系统，该功能本就不可用，且调用点均有
# try/catch 兜底，因此不影响其他功能。
# 注意：仅在上游原生构建失败时才会启用。
STUB_PLUGIN_FILE = "build-patch-local-danmu-stub.js"
STUB_PLUGIN_JS = """
// 由同步脚本注入的构建补丁（仅在原生构建失败时使用）
export const localDanmuStubPlugin = {
  name: 'forward-local-danmu-stub',
  setup(build) {
    build.onResolve({ filter: /local-danmu-(?:store|parser)\\.js$/ }, () => ({
      path: 'local-danmu-stub',
      namespace: 'forward-local-danmu-stub'
    }));

    build.onLoad({ filter: /^local-danmu-stub$/, namespace: 'forward-local-danmu-stub' }, () => ({
      loader: 'js',
      contents: `
        export async function saveLocalDanmu() { return null; }
        export async function getLocalDanmu() { return null; }
        export async function listLocalDanmu() { return []; }
        export async function removeLocalDanmu() { return false; }
        export async function findLocalDanmu() { return null; }
        export function parseLocalDanmu() { return []; }
        export function normalizeLocalKey(v) { return String(v || ''); }
        export function normalizeLocalYear() { return null; }
        export function normalizeLocalType() { return ''; }
        export function normalizeLocalEpisode() { return null; }
        export function normalizeLocalSeason() { return 1; }
        export function buildLocalDanmuResourceKey() { return ''; }
        export function groupLocalDanmuResources() { return []; }
      `
    }));
  }
};
"""

# 把插件挂进 esbuild 的 plugins 数组；锚点变了就认为补丁失败
PLUGIN_ANCHORS = [
    ("      plugins: [\n        forwardRuntimeCompatPlugin,",
     "      plugins: [\n"
     "        (await import('./" + STUB_PLUGIN_FILE + "')).localDanmuStubPlugin,\n"
     "        forwardRuntimeCompatPlugin,"),
    ("plugins: [",
     "plugins: [\n        (await import('./" + STUB_PLUGIN_FILE + "')).localDanmuStubPlugin,"),
]


def run(cmd, cwd=None, capture=False, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[-600:]
        raise RuntimeError(f"命令失败 ({r.returncode}): {' '.join(cmd)}\n{detail}")
    return (r.stdout or "") if capture else ""


def remote_sha(ref):
    """用 ls-remote 取远端 ref 的 commit sha，无需 token。"""
    try:
        out = run(["git", "ls-remote", UPSTREAM, ref], capture=True).strip()
    except Exception as e:
        print(f"[warn] ls-remote {ref} 失败: {e}", file=sys.stderr)
        return None
    return out.split()[0] if out else None


def newest_tags(limit=TAG_FALLBACKS):
    """按语义版本降序返回最近的 tag。"""
    try:
        out = run(["git", "ls-remote", "--tags", UPSTREAM], capture=True)
    except Exception as e:
        print(f"[warn] 获取 tag 列表失败: {e}", file=sys.stderr)
        return []

    tags = {}
    for line in out.splitlines():
        m = re.search(r"refs/tags/(v[\d.]+)$", line.strip())
        if not m:
            continue
        name = m.group(1)
        nums = tuple(int(x) for x in name.lstrip("v").split(".") if x.isdigit())
        tags[name] = nums
    return [t for t, _ in sorted(tags.items(), key=lambda kv: kv[1], reverse=True)][:limit]


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract_version(text):
    """从产物元数据段解析版本号（新版本用 var wv 间接声明，老版本为字面量）。"""
    head = metadata_head(text)
    m = re.search(r'version:\s*"([\d.]+)"', head)
    if m:
        return m.group(1)
    m = re.search(r'var\s+wv\s*=\s*"([\d.]+)"', text)
    return m.group(1) if m else None


def upstream_version(work):
    """从上游 globals.js 读取权威版本号，避免依赖产物中的变量命名。"""
    src = work / "danmu_api" / "configs" / "globals.js"
    if not src.exists():
        return None
    text = src.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"""VERSION:\s*['"]([\d.]+)['"]""", text)
    return m.group(1) if m else None


def metadata_head(text, span=1200):
    """取 WidgetMetadata 定义段，避免在全文中误匹配同名字段。"""
    if "WidgetMetadata = {" not in text:
        raise ValueError("缺少 WidgetMetadata 定义")
    i = text.index("WidgetMetadata = {")
    return text[i:i + span]


def validate(text, ref, version=None):
    """构建产物必须通过全部检查，否则视为失败。"""
    size = len(text.encode("utf-8"))
    if not (MIN_SIZE <= size <= MAX_SIZE):
        raise ValueError(f"体积异常: {size} 字节（期望 {MIN_SIZE}-{MAX_SIZE}）")
    head = metadata_head(text)
    m = re.search(r'id:\s*"([^"]+)"', head)
    if not m or m.group(1) != MODULE_ID:
        raise ValueError(f"模块 id 不匹配: {m.group(1) if m else '(无)'}")

    ver = extract_version(text) or version
    if not ver:
        raise ValueError("无法解析模块版本号")
    if version and version not in text:
        raise ValueError(f"产物中未包含上游声明的版本号 {version}")
    if "node:fs" in text or "node:crypto" in text:
        raise ValueError("产物中残留 Node 内置模块引用，说明构建配置有误")

    # 非致命项，仅提示
    if "Widget.http.get" not in text:
        print("[warn] 产物中未发现 Widget.http.get，上游可能调整了请求实现")
    print(f"[ok] {ref} 校验通过: {size} 字节, version={ver}")
    return ver


def patch_metadata(text):
    """把模块标题与描述替换为自定义文案（仅改 WidgetMetadata 段内的字段）。"""
    idx = text.index("WidgetMetadata = {")
    head, tail = text[idx:idx + 1200], text[idx + 1200:]

    if not re.search(r'id:\s*"forward\.auto\.danmu2"', head):
        raise ValueError("WidgetMetadata 段内未找到目标 id，补丁位置可能已变")

    new_head, n1 = re.subn(r'title:\s*"(?:[^"\\]|\\.)*"', f'title: "{CUSTOM_TITLE}"', head, count=1)
    new_head, n2 = re.subn(r'description:\s*"(?:[^"\\]|\\.)*"',
                           f'description: "{CUSTOM_DESC}"', new_head, count=1)
    if n1 != 1 or n2 != 1:
        raise ValueError(f"补丁失败: title={n1} description={n2}")

    # 结构性复核：补丁只应改动字符串内容，不应改变括号数量
    if (head.count("{"), head.count("}")) != (new_head.count("{"), new_head.count("}")):
        raise ValueError("补丁后花括号数量发生变化")

    print(f"[ok] 已套用自定义文案: {CUSTOM_TITLE}")
    return text[:idx] + new_head + tail


def apply_stub_patch(work):
    """给上游构建脚本注入本地弹幕打桩插件。锚点不匹配则视为补丁失败。"""
    build_js = work / "build-forward-widget.js"
    src = build_js.read_text(encoding="utf-8")
    for anchor, replacement in PLUGIN_ANCHORS:
        if src.count(anchor) == 1:
            build_js.write_text(src.replace(anchor, replacement), encoding="utf-8")
            (work / STUB_PLUGIN_FILE).write_text(STUB_PLUGIN_JS, encoding="utf-8")
            print(f"[info] 已注入打桩插件（锚点: {anchor.splitlines()[0].strip()[:40]}）")
            return True
    raise RuntimeError("找不到可注入的锚点，上游构建脚本结构可能已变更")


def npm_install(work):
    """安装依赖并校验完整性。

    上游仓库没有 package-lock.json，npm install 偶发不完整（表现为构建时
    ERR_MODULE_NOT_FOUND）。这里校验 package.json 中每个直接依赖是否落地，
    不完整就重装，避免白跑一整轮构建。
    """
    try:
        deps = list((json.loads((work / "package.json").read_text(encoding="utf-8"))
                     .get("dependencies") or {}).keys())
    except Exception:
        deps = []

    for attempt in (1, 2):
        run(["npm", "install", "--no-audit", "--no-fund"], cwd=work)
        missing = [d for d in deps if not (work / "node_modules" / d).exists()]
        if not missing:
            return
        print(f"[warn] 依赖安装不完整，缺少 {missing}，重试", file=sys.stderr)
    raise RuntimeError(f"npm install 多次后仍缺少依赖: {missing}")


def run_build(work):
    """执行一次构建，返回 (是否成功, 错误摘要)。"""
    out = work / "dist" / "logvar-danmu.js"
    out.unlink(missing_ok=True)
    r = subprocess.run(["node", "build-forward-widget.js"], cwd=work,
                       capture_output=True, text=True)
    if r.returncode != 0:
        lines = [l.strip() for l in (r.stderr or r.stdout or "").splitlines() if l.strip()]
        # 优先挑出有信息量的错误行，而不是 Node 版本这类尾部噪音
        pick = next((l for l in lines if "ERROR" in l or "Error:" in l), None)
        if not pick:
            pick = lines[0] if lines else "unknown error"
        return False, pick[:200]
    if not out.exists():
        return False, "构建未产出 dist/logvar-danmu.js"
    return True, ""


def build_ref(ref):
    """clone + 安装依赖，然后两段式构建。

    第一段按上游原样构建；失败才注入打桩补丁重试，使偏离上游的程度最小，
    并且上游一旦修复构建即自动回归原生。
    返回 (补丁后的文本, 版本号, 是否使用了补丁)。
    """
    work = Path(tempfile.mkdtemp(prefix="danmu-build-"))
    try:
        print(f"[info] clone {ref} ...")
        run(["git", "clone", "--depth", "1", "--branch", ref, UPSTREAM, str(work)])

        print("[info] npm install ...")
        npm_install(work)

        print("[info] node build-forward-widget.js (原生) ...")
        ok, err = run_build(work)
        used_patch = False

        if not ok:
            print(f"[info] 原生构建失败（{err}），启用本地弹幕打桩补丁重试")
            apply_stub_patch(work)
            print("[info] node build-forward-widget.js (打补丁) ...")
            ok, err2 = run_build(work)
            if not ok:
                raise RuntimeError(f"原生构建失败({err})；打补丁后仍失败({err2})")
            used_patch = True

        out = work / "dist" / "logvar-danmu.js"
        ver = upstream_version(work)
        text = out.read_text(encoding="utf-8", errors="surrogateescape")
        ver = validate(text, ref, ver)
        if used_patch:
            print("[info] 本次产物使用了本地弹幕打桩补丁")
        return patch_metadata(text), ver, used_patch
    finally:
        shutil.rmtree(work, ignore_errors=True)


def update_fwd(version):
    if not FWD.exists():
        print(f"[warn] {FWD} 不存在，跳过 .fwd 更新", file=sys.stderr)
        return
    data = json.loads(FWD.read_text(encoding="utf-8"))
    hit = False
    for w in data.get("widgets", []):
        if w.get("id") == MODULE_ID:
            if w.get("version") != version:
                w["version"] = version
                hit = True
    if hit:
        FWD.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[ok] TFEL.fwd 自动弹幕版本 -> {version}")
    else:
        print("[info] TFEL.fwd 版本号无需变更")


def parse_ver(v):
    """把版本号字符串转成可比较的数字元组。"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) or (0,)


def write_outputs(**kv):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def main():
    state = load_state()
    cur_version = state.get("version")
    if not cur_version and OUT_JS.exists():
        cur_version = extract_version(OUT_JS.read_text(encoding="utf-8", errors="ignore"))
    cur_ver = parse_ver(cur_version)
    print(f"[info] 仓库当前版本: {cur_version or '(无)'}")

    candidates = []

    main_sha = remote_sha("refs/heads/main")
    if main_sha:
        if main_sha == state.get("sha"):
            print("[skip] main 未变化，且已是上次采用的版本")
        elif main_sha == state.get("main_broken_sha"):
            print("[info] main 上次构建失败且此后未更新，直接跳过")
        else:
            candidates.append(("main", main_sha))

    for tag in newest_tags():
        # 只接受比当前版本更新的 tag，绝不降级
        if parse_ver(tag) <= cur_ver:
            continue
        sha = remote_sha(f"refs/tags/{tag}")
        if not sha or sha == state.get("main_broken_sha") or sha == state.get("sha"):
            continue
        candidates.append((tag, sha))

    if not candidates:
        print("[skip] 上游无可用更新，结束")
        write_outputs(status="skipped", changed="false")
        return 0

    for ref, sha in candidates:
        print(f"\n=== 尝试构建 {ref} ({sha[:8]}) ===")
        text = version = None
        last_err = None
        # 依赖安装偶发不完整（网络原因），最多重试一次
        for attempt in (1, 2):
            try:
                text, version, used_patch = build_ref(ref)
                break
            except Exception as e:
                last_err = e
                print(f"[warn] {ref} 第 {attempt} 次构建失败: {e}", file=sys.stderr)
                text = version = None
                used_patch = False

        if not text or not version:
            print(f"[warn] {ref} 构建失败，尝试下一个候选", file=sys.stderr)
            if ref == "main":
                # 记录坏掉的 main，避免下次重复浪费构建时间
                state["main_broken_sha"] = sha
            continue
        OUT_JS.write_text(text, encoding="utf-8")
        print(f"[ok] 已写入 {OUT_JS} ({len(text.encode('utf-8'))} 字节)")

        state.update({"ref": ref, "sha": sha, "version": version,
                      "patched": used_patch})
        if ref == "main":
            # main 构建成功，清除此前记录的失败标记
            state.pop("main_broken_sha", None)
        save_state(state)
        update_fwd(version)

        write_outputs(status="ok", changed="true", version=version, ref=ref,
                      patched=str(used_patch).lower())
        return 0

    # 全部候选都失败：保留仓库现有文件，只持久化失败记录供下次跳过
    print("[error] 所有候选 ref 均构建失败，保留现有文件，不做任何修改", file=sys.stderr)
    save_state(state)
    write_outputs(status="failed", changed="false")
    return 0  # 以 0 退出，让工作流仍能提交 state；最终由工作流步骤判定失败


if __name__ == "__main__":
    sys.exit(main())
