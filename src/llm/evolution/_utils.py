"""自我进化模块公共工具（原 evolution.py 内重复逻辑抽取）。

- JsonStore：通用 JSON 列表文件存取（合并生效话术 / 画像两份重复模板）
- split_frontmatter：SKILL.md frontmatter 与正文拆分
- archive_skill：技能目录归档（可恢复，防覆盖）
- call_llm_json：统一的「LLM 调用 → JSON」助手（thinking 降级 + 解析兜底）
- _strip_ws / _file_mtime：技能正文比较 / 文件时间戳
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from src.llm.utils.jsonutil import parse_json_object
from src.utils import console


class JsonStore:
    """通用 JSON 列表文件存取（去重 load/save/append 重复模板）。

    - load：文件缺失/损坏/非列表返回空列表
    - save：超过 max_items 时丢弃最旧（保尾）；写失败只告警不抛
    - append：追加单条，可选按字段去重（命中返回 False 不写入）
    """

    def __init__(self, path: str | Path, *, label: str,
                 max_items: int | None = None) -> None:
        self.path = Path(path)
        self.label = label
        self.max_items = max_items

    def load(self) -> list[dict]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def save(self, items: list[dict]) -> None:
        if self.max_items is not None and len(items) > self.max_items:
            items = items[-self.max_items:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        except OSError as e:
            console.warn(f"[进化] 写入{self.label}失败：{e}")

    def append(self, item: dict, *, dedup_key: str | None = None) -> bool:
        items = self.load()
        if dedup_key is not None and any(
                it.get(dedup_key) == item.get(dedup_key) for it in items):
            return False
        items.append(item)
        self.save(items)
        return True


def split_frontmatter(text: str) -> tuple[str, str]:
    """拆 SKILL.md 的 frontmatter 与正文。

    返回 (frontmatter, body)：frontmatter 以 `---` 结尾（不含其后换行），
    body 已去前导空行。
    - 不以 `---` 开头 → 视为无 frontmatter：( "", 原文)
    - 以 `---` 开头但未闭合 → 整个文本视为 frontmatter：(原文, "")
    - 正常 → (frontmatter, body)
    """
    if not text.startswith("---"):
        return "", text
    sep = text.find("\n---", 3)
    if sep < 0:
        return text, ""
    return text[:sep + 4], text[sep + 4:].lstrip("\n")


def archive_skill(skill, *, archive_dir_name: str = "_archived") -> bool:
    """把技能目录移入 <技能根>/<archive_dir_name>/<技能名>/。

    目标已存在则跳过（防覆盖），返回是否真的归档；失败只告警不抛。
    """
    dest = skill.location.parent.parent / archive_dir_name / skill.name
    if dest.exists():
        console.warn(f"[进化] 归档目录已存在，跳过技能 {skill.name!r}")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(skill.location.parent), str(dest))
        return True
    except OSError as e:
        console.warn(f"[进化] 归档技能 {skill.name!r} 失败：{e}")
        return False


async def call_llm_json(
    candidates: list[tuple[Any, str, str]],
    messages: list[dict],
    *,
    label: str,
    temperature: float = 0.4,
    max_tokens: int = 2048,
    timeout: float = 60.0,
    prefer_disabled_thinking: bool = True,
    task: str = "",
) -> dict | None:
    """统一的「LLM 调用 → JSON 解析」助手。

    - candidates: [(client, model, 模型标签)]，按序尝试，全部失败返回 None
    - prefer_disabled_thinking: 先带 thinking disabled 请求，不支持则降级普通模式
      （DeepSeek 等默认开启思考时 content 为空、内容全在 reasoning_content）
    - content 为空时兜底读 reasoning_content；解析失败返回 None 并打日志
    - task: 5.16 记账任务名（如 "review"）；为空不记账，签名向后兼容
    - 失败一律 fail-open（打日志返回 None），不抛异常影响主流程
    """
    start = time.time()
    resp = None
    used_model = ""
    for client, model, c_label in candidates:
        try:
            kwargs = dict(model=model, messages=messages,
                          temperature=temperature, max_tokens=max_tokens)
            if prefer_disabled_thinking:
                try:
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            **kwargs, extra_body={"thinking": {"type": "disabled"}}),
                        timeout=timeout,
                    )
                except Exception:
                    console.dim(f"[进化] {c_label}不支持 thinking 参数，降级为普通模式")
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(**kwargs), timeout=timeout)
            else:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs), timeout=timeout)
            used_model = model
            break
        except Exception as e:
            console.warn(f"[进化] {label}模型调用失败（{c_label}）：{e}")
    if resp is None:
        return None
    # 5.16 记账（旁路）：记录成功调用的 token 与耗时，失败不影响结果
    if task:
        try:
            usage = getattr(resp, "usage", None)
            from .usage import record_usage
            record_usage(
                task, used_model,
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
                time.time() - start,
            )
        except Exception:
            pass
    msg = resp.choices[0].message
    content = (msg.content or "").strip()
    if not content:
        content = (getattr(msg, "reasoning_content", None) or "").strip()
    data = parse_json_object(content)
    if not data:
        console.dim(f"[进化] {label}输出无法解析，跳过本次")
        return None
    return data


def _strip_ws(text: str) -> str:
    """去掉全部空白字符，用于判断技能正文是否发生实质变化。"""
    return "".join(text.split())


def _file_mtime(path: Path) -> float:
    """文件最后修改时间戳（读取失败时返回 0，视为刚创建不参与清理）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
