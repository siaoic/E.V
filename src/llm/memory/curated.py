"""L2 内建长期记忆（Hermes 式）：MEMORY.md / USER.md 纯文本 + 冻结快照。

设计对齐 hermes tools/memory_tool.py 的 MemoryStore，落地四层记忆架构的 L2 层：
- 两个纯文本文件：MEMORY.md（AI 自己的笔记）、USER.md（对观众的认知），
  人可审计、可直接编辑；
- 条目用 "\\n§\\n" 分隔（支持多行条目），字符硬上限（非 token，模型无关）
  防无限膨胀：memory 2200 / user 1375（可用 .env 调整）；
- add / replace / remove 靠唯一子串匹配定位，不用 ID；匹配多条时报错
  要求模型更具体（防歧义）；
- 原子写入（tmp + os.replace）+ 进程内互斥锁，读改写安全；
- 威胁扫描：写入与进快照前扫描 Prompt 注入 / 角色劫持 / 密钥提取 /
  隐形 Unicode（threat.py），命中即拒绝或替换为占位符；
- 冻结快照：load 时生成 system prompt 快照，整个会话保持不变（保 Prefix
  Cache 命中率）；会话中的写入即时落盘但不改动快照，下一次会话生效。

所有运行时写路径落在 <DATA_ROOT>/memories/ 下（走 cfg.DATA_ROOT，硬约束）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from src.llm.memory.threat import first_threat_message
from src.utils import config

# 条目分隔符：多行条目用 § 分隔（与 hermes 一致，避免与条内换行冲突）
_ENTRY_DELIMITER = "\n§\n"

# _reload_target 返回哨兵：文件存在但读失败（不可把"读失败"当"空库"覆盖写）
_READ_FAILED = object()

# 冻结快照渲染用的块标题
_MEMORY_BLOCK_HEADER = "【AI 笔记（MEMORY.md）】"
_USER_BLOCK_HEADER = "【观众认知（USER.md）】"


class CuratedMemoryStore:
    """有界纯文本长期记忆，进程内单例。维护两份状态：

    - memory_entries / user_entries：实时状态，工具写入后立即落盘；
    - _system_prompt_snapshot：load 时冻结，供 system prompt 注入，
      会话内不再变化（前缀缓存稳定）。
    """

    def __init__(self, memory_dir: str, memory_char_limit: int = 2200,
                 user_char_limit: int = 1375) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}
        # 进程内互斥：LLM 工具线程并发写入时读改写安全
        self._lock = threading.Lock()

    # ---------- 加载与冻结快照 ----------

    def load_from_disk(self) -> None:
        """读取 MEMORY.md / USER.md，去重，生成冻结快照（供 system prompt）。

        扫描每条进快照前的条目：命中威胁即替换为占位符（不进 prompt），
        原始文本保留在实时状态里供用户查看/删除——静默删除会掩盖攻击。
        """
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.memory_entries = self._read_file(self.memory_dir / "MEMORY.md")
            self.user_entries = self._read_file(self.memory_dir / "USER.md")
            # 去重（保持顺序，保留首现）
            self.memory_entries = list(dict.fromkeys(self.memory_entries))
            self.user_entries = list(dict.fromkeys(self.user_entries))
            self._system_prompt_snapshot = {
                "memory": self._render_block(
                    self._sanitize_for_snapshot(self.memory_entries, "MEMORY.md"),
                    "memory"),
                "user": self._render_block(
                    self._sanitize_for_snapshot(self.user_entries, "USER.md"),
                    "user"),
            }

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """返回冻结快照段（load 时状态，非实时）；空快照返回 None（不注入）。"""
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # ---------- 实时写入（add / replace / remove / apply_batch） ----------

    def add(self, target: str, content: str) -> dict:
        """追加一条新条目；超字符上限时返回引导合并的错误。"""
        content = (content or "").strip()
        if not content:
            return {"success": False, "error": "内容不能为空。"}
        scan_error = first_threat_message(content)
        if scan_error:
            return {"success": False, "error": scan_error}
        with self._lock:
            # 追加不改写已有内容，跳过漂移检查；但读失败必须拒绝（防整库重写清空）
            if self._reload_target(target, skip_drift=True) is _READ_FAILED:
                return self._read_failed_error(target)
            entries = self._entries_for(target)
            if content in entries:
                return self._success(target, "条目已存在（未重复添加）。")
            new_total = len(_ENTRY_DELIMITER.join(entries + [content]))
            if new_total > self._char_limit(target):
                return self._over_budget(target, "add", new_total)
            entries.append(content)
            self.save_to_disk(target)
        return self._success(target, "已添加条目。")

    def replace(self, target: str, old_text: str, new_content: str) -> dict:
        """把含 old_text 子串的条目替换为 new_content（唯一匹配才替换）。"""
        old_text = (old_text or "").strip()
        new_content = (new_content or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text 不能为空。"}
        if not new_content:
            return {"success": False, "error": "new_content 不能为空（删除用 remove）。"}
        scan_error = first_threat_message(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}
        with self._lock:
            drift = self._reload_target(target)
            if drift is _READ_FAILED:
                return self._read_failed_error(target)
            if drift:
                return self._drift_error(target, drift)
            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
            if not matches:
                return self._no_match(target, old_text, "replace")
            if not self._unambiguous(matches):
                return self._ambiguous(target, matches, "replace")
            idx = matches[0][0]
            test = list(entries)
            test[idx] = new_content
            new_total = len(_ENTRY_DELIMITER.join(test))
            if new_total > self._char_limit(target):
                return self._over_budget(target, "replace", new_total)
            entries[idx] = new_content
            self.save_to_disk(target)
        return self._success(target, "已替换条目。")

    def remove(self, target: str, old_text: str) -> dict:
        """删除含 old_text 子串的条目（唯一匹配才删除）。"""
        old_text = (old_text or "").strip()
        if not old_text:
            return {"success": False, "error": "old_text 不能为空。"}
        with self._lock:
            drift = self._reload_target(target)
            if drift is _READ_FAILED:
                return self._read_failed_error(target)
            if drift:
                return self._drift_error(target, drift)
            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
            if not matches:
                return self._no_match(target, old_text, "remove")
            if not self._unambiguous(matches):
                return self._ambiguous(target, matches, "remove")
            entries.pop(matches[0][0])
            self.save_to_disk(target)
        return self._success(target, "已删除条目。")

    def apply_batch(self, target: str, operations: list[dict]) -> dict:
        """原子应用 add/replace/remove 序列：全有或全无，最终态才查字符上限。

        让模型在单次调用里「腾空间 + 写入」而不是多轮合并重试。
        """
        if not operations:
            return {"success": False, "error": "operations 列表为空。"}
        # 先整体扫描内容（一次投毒拒绝整批），再在锁内应用
        for i, op in enumerate(operations):
            if (op or {}).get("action") in ("add", "replace") and (op or {}).get("content"):
                scan_error = first_threat_message(str(op["content"]))
                if scan_error:
                    return {"success": False, "error": f"操作 {i + 1}：{scan_error}"}
        with self._lock:
            drift = self._reload_target(target)
            if drift is _READ_FAILED:
                return self._read_failed_error(target)
            if drift:
                return self._drift_error(target, drift)
            working: list[str] = list(self._entries_for(target))
            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = str(op.get("content") or "").strip()
                old_text = str(op.get("old_text") or "").strip()
                pos = f"操作 {i + 1}（{act or '未知'}）"
                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}：content 必填。")
                    if content not in working:
                        working.append(content)  # 重复添加幂等跳过
                elif act == "replace":
                    if not old_text or not content:
                        return self._batch_error(
                            target, f"{pos}：replace 需要 old_text 和 content。")
                    result = self._batch_find(working, old_text)
                    if result is None:
                        return self._batch_error(target, f"{pos}：无条目匹配 '{old_text}'。")
                    if result is False:
                        return self._batch_error(
                            target, f"{pos}：'{old_text}' 匹配多条不同条目，请更具体。")
                    working[result] = content
                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}：remove 需要 old_text。")
                    result = self._batch_find(working, old_text)
                    if result is None:
                        return self._batch_error(target, f"{pos}：无条目匹配 '{old_text}'。")
                    if result is False:
                        return self._batch_error(
                            target, f"{pos}：'{old_text}' 匹配多条不同条目，请更具体。")
                    working.pop(result)
                else:
                    return self._batch_error(
                        target, f"{pos}：未知 action（add/replace/remove）。")
            new_total = len(_ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > self._char_limit(target):
                return self._over_budget(target, "batch", new_total)
            self._set_entries(target, working)
            self.save_to_disk(target)
        return self._success(target, f"已应用 {len(operations)} 项操作。")

    # ---------- 只读查询（供命令/工具展示） ----------

    def list_entries(self, target: str) -> list[str]:
        """返回某目标的实时条目列表（调用方自行加锁或仅在展示场景使用）。"""
        return list(self._entries_for(target))

    def char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        return len(_ENTRY_DELIMITER.join(entries)) if entries else 0

    def char_limit(self, target: str) -> int:
        return self._char_limit(target)

    # ---------- 内部：条目读写 ----------

    def _entries_for(self, target: str) -> list[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def _path_for(self, target: str) -> Path:
        if target == "user":
            return self.memory_dir / "USER.md"
        return self.memory_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False):
        """锁内重读磁盘最新状态到内存（捡起外部编辑/多会话写入）。

        返回 None 表示干净重载；返回字符串 = 漂移备份路径（调用方须中止，
        否则整库重写会丢弃无法往返的外部内容）；返回 _READ_FAILED = 文件
        存在但读失败（同样须中止，防把"读失败"当"空库"覆盖写）。
        """
        path = self._path_for(target)
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            return _READ_FAILED
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = self._parse_entries(raw)
        fresh = list(dict.fromkeys(fresh))
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str) -> None:
        """把目标条目原子写入文件（tmp + os.replace）。"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    # ---------- 内部：文件与解析 ----------

    @staticmethod
    def _read_raw_checked(path: Path) -> tuple[str, bool]:
        """读取原始文本，区分「不存在（空）」与「存在但读失败」。

        不存在 → ("", True)；存在但解码失败/IO 错 → ("", False)。解码用
        utf-8-sig（剥 BOM，兼容 Notepad 编辑），strict 不替换——替换会给
        读改写路径一个失真视图，落盘时覆盖真实字节。
        """
        if not path.exists():
            return "", True
        try:
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> list[str]:
        """按条目分隔符切分并去除空条目。"""
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(_ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        """只读加载（load_from_disk 用）：失败降级为空列表（不写回，安全）。"""
        raw, read_ok = CuratedMemoryStore._read_raw_checked(path)
        if not read_ok:
            return []
        return CuratedMemoryStore._parse_entries(raw)

    def _detect_external_drift(self, target: str, raw: str) -> Optional[str]:
        """检测磁盘文件是否有工具无法往返的外部内容（外部编辑/拼写追加）。

        两个信号：往返不一致（重解析重序列化 ≠ 原文）；单条超过整库上限
        （工具写入的单条不可能超限，超限说明外部塞了自由文本）。命中则把
        原文件备份成 .bak.<ts> 并返回路径，调用方拒绝本次改写以防丢内容。
        """
        path = self._path_for(target)
        if not raw.strip():
            return None
        parsed = [e.strip() for e in raw.split(_ENTRY_DELIMITER) if e.strip()]
        roundtrip = _ENTRY_DELIMITER.join(parsed)
        max_entry_len = max((len(e) for e in parsed), default=0)
        if raw.strip() == roundtrip and max_entry_len <= self._char_limit(target):
            return None
        ts = int(__import__("time").time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + "（备份失败，磁盘文件未改动）"
        return str(bak_path)

    def _write_file(self, path: Path, entries: list[str]) -> None:
        """原子写：先写同目录 tmp 再 os.replace，读者永远看到完整旧/新文件。"""
        content = _ENTRY_DELIMITER.join(entries) if entries else ""
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)

    # ---------- 内部：快照清洗与渲染 ----------

    def _sanitize_for_snapshot(self, entries: list[str], filename: str) -> list[str]:
        """命中威胁的条目替换为占位符（快照用），原始条目保留在实时状态。"""
        sanitized: list[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            if first_threat_message(entry):
                sanitized.append(
                    f"[BLOCKED: {filename} 中该条目含威胁内容，已从系统提示中移除；"
                    "可用 /memory curated 查看并删除原条目]")
            else:
                sanitized.append(entry)
        return sanitized

    def _render_block(self, entries: list[str], target: str) -> str:
        """把条目渲染成 system prompt 块（标题 + 用量 + 条目内容）。"""
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = _ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        header = _USER_BLOCK_HEADER if target == "user" else _MEMORY_BLOCK_HEADER
        return f"{header}（{pct}% — {current}/{limit} 字符）\n{content}"

    # ---------- 内部：错误与成功响应 ----------

    def _success(self, target: str, message: str) -> dict:
        return {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{self.char_count(target):,}/{self._char_limit(target):,} 字符",
            "entry_count": len(self._entries_for(target)),
            "message": message,
            "note": "已保存，本轮无需重复操作。",
        }

    def _over_budget(self, target: str, action: str, new_total: int) -> dict:
        current = self.char_count(target)
        limit = self._char_limit(target)
        return {
            "success": False,
            "error": (
                f"目标将达 {new_total:,}/{limit:,} 字符，超出上限。"
                f"请合并重叠条目（replace）或删除过期条目（remove）腾出空间，"
                f"再在同一个批次里完成 add。"
            ),
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        }

    def _no_match(self, target: str, old_text: str, action: str) -> dict:
        return {
            "success": False,
            "error": f"没有条目包含 '{old_text}'。请用下方 current_entries 里的"
                     f"真实文本重试 {action}。",
            "current_entries": self._entries_for(target),
        }

    def _ambiguous(self, target: str, matches: list, action: str) -> dict:
        previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
        return {
            "success": False,
            "error": f"'{matches[0][1][:40]}' 匹配了多条不同条目，{action} 中止。"
                     "请提供更具体的子串以唯一定位。",
            "matches": previews,
        }

    @staticmethod
    def _unambiguous(matches: list) -> bool:
        """匹配的多条条目文本完全相同（重复项）时可安全作用于第一条。"""
        return len({e for _, e in matches}) <= 1

    def _batch_error(self, target: str, message: str) -> dict:
        return {
            "success": False,
            "error": message + " 本批次未应用任何操作（全有或全无）。",
            "current_entries": self._entries_for(target),
        }

    def _batch_find(self, entries: list[str], old_text: str):
        """批量定位子串：None=无匹配；False=多条不同条目；int=命中下标。"""
        idx = [j for j, e in enumerate(entries) if old_text in e]
        if not idx:
            return None
        if len({entries[j] for j in idx}) > 1:
            return False
        return idx[0]

    def _read_failed_error(self, target: str) -> dict:
        return {
            "success": False,
            "error": (
                f"拒绝写入 {self._path_for(target).name}：文件存在但当前无法读取"
                "（被占用/权限变化/编码损坏）。为避免把「读失败」当「空库」"
                "整文件重写清空记忆，本次写入已中止，请稍后重试。"
            ),
        }

    def _drift_error(self, target: str, bak_path: str) -> dict:
        return {
            "success": False,
            "error": (
                f"拒绝写入 {self._path_for(target).name}：磁盘上有工具无法往返的"
                f"内容（外部编辑/并发会话写入）。已备份到 {bak_path}，请先人工"
                "整理为 § 分隔的条目列表再重试，防止静默丢内容。"
            ),
            "drift_backup": bak_path,
        }


# ---------- 进程内单例（懒构建，按当前 cfg 生成） ----------

_instance: Optional[CuratedMemoryStore] = None
_init_lock = threading.Lock()


def _curated_dir() -> str:
    """可写数据根下集中放长期记忆文件（走 cfg.DATA_ROOT 硬约束）。"""
    return os.path.join(config.cfg.DATA_ROOT, "memories")


def get_curated_store() -> CuratedMemoryStore:
    """返回进程内单例（首次访问时按当前 cfg 构建并生成冻结快照）。"""
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                cfg = config.cfg
                store = CuratedMemoryStore(
                    memory_dir=_curated_dir(),
                    memory_char_limit=int(cfg.MEMORY_CURATED_MEMORY_LIMIT or 2200),
                    user_char_limit=int(cfg.MEMORY_CURATED_USER_LIMIT or 1375),
                )
                store.load_from_disk()
                _instance = store
    return _instance


def reset_curated_store() -> None:
    """清空单例（!config memory 热重载时调用），下次访问按新配置重建。"""
    global _instance
    with _init_lock:
        _instance = None
