"""E.V 仓库冗余文件一键清理脚本（安全版）。

用法：
    python scripts/cleanup_redundant.py              # 仅预览（dry-run），打印计划不删除
    python scripts/cleanup_redundant.py --apply      # 交互确认后真正执行
    python scripts/cleanup_redundant.py --apply --yes   # 跳过确认直接执行
    python scripts/cleanup_redundant.py --apply --aggressive --yes  # 连可选清理项一起处理

设计原则：
- 只删除下方清单中【显式列出】的路径，绝不递归清理整个目录；
- 已入库（git 跟踪）的文件用 `git rm` 删除（保留历史可追溯），未入库文件直接删除；
- 运行期依赖（gsv-tts/、src/danmaku/blivedm/、src/utils/*.ttf、
  src/memory/memu/src/ 等）一律不在删除清单内；
- 执行前有 _verify_safe() 白名单校验，触及受保护路径立即中止。
"""

import argparse
import os
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)  # 统一以项目根为基准，路径均为相对路径


# ---------------------------------------------------------------------------
# 1) 已入库（git 跟踪）的冗余文件/目录：删除后需 git rm + 提交
# ---------------------------------------------------------------------------
TRACKED_REMOVE = [
    # 开发者随手拍摄的临时照片
    "assets/IMG_20260807_173358.png",
    # 桌面端打包配置（若不再打包 PyInstaller 桌面端，可删除；dist/ 下的 exe 仍在）
    "ControlCenter.spec",
    # scripts/ 下重复或过期的本地调试 bat（根目录 run.bat 为正式启动入口）
    "scripts/LLM测试.bat",
    "scripts/llamacpp启动.bat",
    "scripts/run.bat",
    "scripts/弹幕启动.bat",
    # 本地调试 / 一次性测试脚本
    "tests/probe_db.py",
    "tests/test_e2e_memu.py",
    "tests/test_llm_llamacpp.py",
    "tests/test_memu_agentic.py",
    "tests/test_tts_input.py",
    # ---- memU 第三方库的开发期文件（保留 src/memu 运行包 + LICENSE.txt + AGENTS.md）----
    "src/memory/memu/.github",
    "src/memory/memu/assets",
    "src/memory/memu/docs",
    "src/memory/memu/npm",
    "src/memory/memu/tests",
    "src/memory/memu/scripts",
    "src/memory/memu/.gitignore",
    "src/memory/memu/.pre-commit-config.yaml",
    "src/memory/memu/.python-version",
    "src/memory/memu/CHANGELOG.md",
    "src/memory/memu/CONTRIBUTING.md",
    "src/memory/memu/INSTALL-LATEST.md",
    "src/memory/memu/MANIFEST.in",
    "src/memory/memu/Makefile",
    "src/memory/memu/README.md",
    "src/memory/memu/SKILL.md",
    "src/memory/memu/pyproject.toml",
    "src/memory/memu/setup.cfg",
    "src/memory/memu/uv.lock",
]

# ---------------------------------------------------------------------------
# 2) 未入库（本地磁盘）的冗余文件/目录：直接删除
# ---------------------------------------------------------------------------
UNTRACKED_REMOVE = [
    # PyInstaller 构建产物
    "build",
    "dist",
    # 临时抓取/研究资料（neurosama 博客抓取 txt 及转换脚本）
    "2",
    # 第三方参考源码与资料（Muice-Chatbot-main、neurosama-perspective、zip、E.V.txt、README）
    "docs",
    # 根目录架构审查文档（未入库）
    "E.V.txt",
    # 根目录重复的本地 bat（与 scripts/llamacpp启动.bat 相同，指向本机 D:\\llama.cpp）
    "llamacpp启动.bat",
    # assets/ 下的调试玩具（图片转 ASCII 脚本及产物，均无业务代码引用）
    "assets/1.py",
    "assets/ascii_art.txt",
    "assets/block_pic.txt",
    "assets/girl_box.png",
    "assets/preview.html",
]

# ---------------------------------------------------------------------------
# 3) 可选清理项：默认跳过，需 --aggressive 才会处理
# ---------------------------------------------------------------------------
OPTIONAL_REMOVE = [
    # 示例 MCP 工具服务器：若删除，需同步移除 configs/mcp_config.json 中的
    # "example_time" 条目，否则 MCPManager 拉起该工具会报错。
    "configs/tools/example_time.py",
]

# ---------------------------------------------------------------------------
# 受保护路径（白名单）：脚本绝不触碰，校验失败立即中止
# ---------------------------------------------------------------------------
PROTECTED = [
    # memU 运行包 / 许可与文档（memory.py 通过 _MEMU_SRC 导入）
    "src/memory/memu/src",
    "src/memory/memu/LICENSE.txt",
    "src/memory/memu/AGENTS.md",
    # TTS 运行期依赖（src/tts/engine.py 从该目录加载 gsv_tts 包与模型）
    "gsv-tts",
    # 弹幕运行期依赖（bili_danmaku.py import blivedm 实际解析到该目录）
    "src/danmaku/blivedm",
    # 运行期字体资源
    "src/utils/字魂布丁体(商用需授权).ttf",
    "src/utils/ArtierEN-2.ttf",
    # Live2D 模型资产（用户明确要求保留；live2d/ 前缀已整体保护，此处再显式点名）
    "live2d/阿芙洛狄忒模型文件",
    "live2d/mianfeimox",
    "live2d/肥牛",
    "live2d/【免费版】悠小喵",
    # 核心入口与配置
    "main.py",
    "pyproject.toml",
    "run.bat",
    ".gitignore",
]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _norm(rel: str) -> str:
    """统一分隔符为 os.sep，保证 Windows 下路径比较正确。"""
    return rel.replace("/", os.sep)


def is_git_repo() -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def is_tracked(rel: str) -> bool:
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", _norm(rel)],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def verify_safe(rel: str) -> None:
    """白名单校验：删除目标不得与任一受保护路径重合或互为父子。"""
    target = _norm(rel).rstrip(os.sep)
    for p in PROTECTED:
        protected = _norm(p).rstrip(os.sep)
        if target == protected or target.startswith(protected + os.sep) or protected.startswith(target + os.sep):
            raise SystemExit(f"[安全拦截] {rel} 与受保护路径 {p} 冲突，拒绝删除。")


def path_size(rel: str) -> int:
    """递归统计路径占用字节数（存在才统计）。"""
    path = _norm(rel)
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def collect_cache_files():
    """收集 __pycache__ 目录、*.pyc / *.pyo、.DS_Store（跳过巨型目录）。"""
    hits = []
    skip_dirs = {"runtime", "vendor_pet", ".git", ".trae"}
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for d in list(dirs):
            if d == "__pycache__":
                hits.append(os.path.join(root, d))
                dirs.remove(d)
        for f in files:
            if f.endswith((".pyc", ".pyo")) or f == ".DS_Store":
                hits.append(os.path.join(root, f))
    return hits


def remove_disk(rel: str) -> None:
    """物理删除磁盘上的路径（目录或文件）。"""
    path = _norm(rel)
    if not os.path.exists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def git_remove(rel: str) -> None:
    """对已入库路径执行 git rm（同时删除磁盘文件并暂存删除）。"""
    if not os.path.exists(_norm(rel)):
        return
    subprocess.run(
        ["git", "rm", "-r", "--", _norm(rel)],
        check=False,
    )
    remove_disk(rel)  # 兜底：未入库的残留部分也清掉


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="E.V 仓库冗余文件清理（默认 dry-run 预览）")
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认仅打印计划）")
    parser.add_argument("--aggressive", action="store_true", help="连可选清理项一起处理")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Windows 控制台默认 GBK，切到 UTF-8 避免中文清单乱码（ctypes 懒加载）
    if os.name == "nt":
        import ctypes
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    # 组装计划
    plan = []  # (路径, 来源分类, 是否 git rm)
    for rel in TRACKED_REMOVE:
        verify_safe(rel)
        plan.append((rel, "已入库·冗余", "git"))
    for rel in UNTRACKED_REMOVE:
        verify_safe(rel)
        plan.append((rel, "未入库·冗余", "disk"))
    if args.aggressive:
        for rel in OPTIONAL_REMOVE:
            verify_safe(rel)
            plan.append((rel, "可选·示例工具", "disk"))
    cache_files = collect_cache_files()

    # 过滤掉当前不存在的路径（避免误报）
    plan = [(rel, cat, how) for rel, cat, how in plan if os.path.exists(_norm(rel))]

    git_ok = is_git_repo()

    # 打印计划
    print("=" * 64)
    print(f"E.V 仓库冗余清理计划（{'APPLY 执行' if args.apply else 'dry-run 预览'}）")
    print("=" * 64)
    total_size = 0
    for i, (rel, cat, how) in enumerate(plan, 1):
        size = path_size(rel)
        total_size += size
        method = ("git rm" if how == "git" and git_ok and is_tracked(rel) else "物理删除")
        print(f"  [{cat}] {method:6s}  {rel}  ({size / 1024:.1f} KB)")
    if cache_files:
        print(f"\n  缓存清理（__pycache__ / *.pyc / .DS_Store）：共 {len(cache_files)} 个")
        for rel in cache_files[:20]:
            print(f"  [缓存产物] 物理删除  {rel}")
        if len(cache_files) > 20:
            print(f"  ... 其余 {len(cache_files) - 20} 个略")
    print("-" * 64)
    print(f"共 {len(plan)} 项冗余条目，约 {total_size / 1024 / 1024:.1f} MB（不含缓存）。")

    if not args.apply:
        print("\n以上为预览。确认无误后执行：python scripts/cleanup_redundant.py --apply")
        return

    # 交互确认
    if not args.yes:
        answer = input(f"\n确认删除以上全部 {len(plan)} 项？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消，未删除任何文件。")
            return

    # 执行
    removed = 0
    for rel, cat, how in plan:
        if how == "git" and git_ok and is_tracked(rel):
            git_remove(rel)
        else:
            remove_disk(rel)
        removed += 1
        print(f"[已删除] {rel}")
    for rel in cache_files:
        remove_disk(rel)
    print(f"\n完成：删除 {removed} 项冗余条目 + {len(cache_files)} 个缓存文件。")
    if git_ok:
        print("提示：git rm 已暂存删除，请检查后提交，例如：")
        print('  git commit -m "chore: 清理仓库冗余文件与构建产物"')


if __name__ == "__main__":
    main()
