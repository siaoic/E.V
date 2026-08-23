"""技能沉淀 / 修补 / 合并 / 归档（原 EvolutionEngine 的技能职责拆分）。

- save_skill：复盘结果沉淀 SKILL.md + 子文件（watchdog 热加载，评估告警）
- apply_patch：技能自我改进（保留 frontmatter 整体替换正文，评估择优回滚）
- maybe_prune：定期审阅技能库（Curator：窄技能合并进 umbrella + 过时技能归档）
- apply_merges：执行合并（正文并入 umbrella 后归档被吸收技能）
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from src.llm.evolution.skill_eval import get_evaluator
from src.llm.skills.curator import (
    _dir_sha256, append_curator_ledger, capture_before, curator_enabled,
    snapshot_skills,
)
from src.utils import config, console

from ._utils import _file_mtime, _strip_ws, archive_skill, call_llm_json, split_frontmatter
from .metrics import (
    EVENT_SKILL_ARCHIVED, EVENT_SKILL_MERGED, EVENT_SKILL_PATCH_REVERTED,
    EVENT_SKILL_PATCHED, EVENT_SKILL_SAVED, METRICS,
)
from .prompts import (
    CFG, _HERMES_CURATOR_REVIEW_PROMPT, _PRUNE_OUTPUT_PROTOCOL,
)

# 5.4 包完整性：以下子目录存在文件即视为"整包技能"（含 references/ 等捆绑
# 资源的技能扁平化为小节会留下悬空链接，合并时必须整体保留/整包归档）
_RESOURCE_SUBDIRS = ("references", "examples", "templates", "scripts")


def _has_bundled_package(skill_dir: Path) -> bool:
    """技能是否"整包"——含捆绑子文件或 SKILL.md 相对链接（5.4 包完整性）。

    含子文件/相对链接的技能扁平化为小节会留下悬空链接，禁止并入 umbrella；
    只能三选一：整体保留 / 全量搬移子文件并改写路径 / 整包归档（archive_skill
    整目录移动天然支持）。此检查用于保守拒绝扁平化合并。
    """
    for sub in _RESOURCE_SUBDIRS:
        base = skill_dir / sub
        if base.is_dir():
            try:
                if any(p.is_file() for p in base.rglob("*")):
                    return True
            except OSError:
                pass
    try:
        text = (skill_dir / "SKILL.md").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False
    # 相对链接：Markdown 链接/图片引用非 http(s) 目标（含 ./ ../ 与裸文件名）
    for m in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", text):
        target = m.group(1).strip().lstrip("/")
        if target and not target.lower().startswith(("http://", "https://")):
            return True
    return False


# ---------- 5.10 引用迁移（absorbed_into） ----------

def _scheduler_path() -> Path:
    """定时任务清单路径（可写数据根，与 src/agent/scheduler.py 口径一致）。"""
    return Path(config.cfg.DATA_ROOT) / "agent_schedule.json"


def _scheduler_task_texts() -> list[str]:
    """读取全部定时任务文本；清单缺失/损坏返回空列表。"""
    try:
        with _scheduler_path().open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [str(i.get("task") or "") for i in data if isinstance(i, dict)]
    except (OSError, ValueError):
        return []


def _scheduler_referenced_skills(skill_names: list[str]) -> set[str]:
    """被定时任务文本引用的技能名集合（整词匹配，5.4 cron 引用保护）。

    被调度任务引用的技能不进入合并/归档候选，防止后台策展剪掉在跑的引用。
    """
    texts = _scheduler_task_texts()
    if not texts:
        return set()
    joined = "\n".join(texts)
    referenced = set()
    for name in skill_names:
        if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", joined):
            referenced.add(name)
    return referenced


def _migrate_scheduler_references(mapping: dict[str, str]) -> int:
    """把定时任务文本中的旧技能名改写为 umbrella 名，返回改写条目数（5.10）。

    仅命中"任务文本包含旧技能名整词"的条目才改写（保守匹配，未命中不改写）；
    清单缺失/损坏或改写失败时静默返回 0，不改动任何内容。
    """
    if not mapping:
        return 0
    path = _scheduler_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return 0
    except (OSError, ValueError):
        return 0
    changed = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str):
            continue
        new_task = task
        for old, new in mapping.items():
            new_task = re.sub(
                rf"(?<![a-z0-9_]){re.escape(old)}(?![a-z0-9_])", new, new_task)
        if new_task != task:
            item["task"] = new_task
            changed += 1
    if changed:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            console.warn(f"[进化] 调度任务引用改写失败：{e}")
            return 0
    return changed


def _write_absorbed_marker(skill, umbrella: str) -> None:
    """归档技能目录写 `.absorbed_into` 标记（内容 = umbrella 名；空 = 真修剪）。

    对标 hermes 的 .curator_suppressed：归档技能不复活、不被再次建议/合并。
    目标目录位于 _archived 下；写入失败仅告警，不影响归档结果。
    """
    dest = skill.location.parent.parent / CFG.archive_dir_name / skill.name
    if not dest.is_dir():
        return
    try:
        (dest / ".absorbed_into").write_text(umbrella + "\n", encoding="utf-8")
    except OSError as e:
        console.warn(f"[进化] 归档标记写入失败（{skill.name!r}）：{e}")


class SkillEvolution:
    """技能沉淀 / 修补 / 合并 / 归档（pin 保护 + 评估择优 + 可恢复归档）。"""

    async def save_skill(self, skill: dict) -> None:
        """生成 SKILL.md 写入技能目录（Skills 模块 watchdog 自动热加载）。

        支持随技能一起沉淀子文件（对标 hermes 的 references/templates/scripts）：
        resources 中的 path 必须位于 references/、templates/ 或 scripts/ 下且
        不得越出技能目录（防路径穿越），越界/非法项跳过不影响 SKILL.md 落盘。
        """
        name = (skill.get("name") or "").strip()
        desc = (skill.get("description") or "").strip()
        content = (skill.get("content") or "").strip()
        if not name or not content:
            return
        safe_name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
        if not safe_name:
            return
        root = (config.cfg.SKILLS_DIR or "src/llm/skills").split(",")[0].strip()
        skill_dir = Path(config.cfg.PROJECT_ROOT) / root / safe_name
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            md = (f"---\nname: {safe_name}\ndescription: {desc}\n---\n\n"
                  f"{content}\n")
            (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
        except OSError as e:
            console.warn(f"[进化] 技能沉淀写入失败：{e}")
            return
        # 子文件沉淀：只允许 references/templates/scripts 子目录，防路径穿越
        resources = skill.get("resources")
        if isinstance(resources, list):
            written = self.write_skill_resources(skill_dir, resources)
            if written:
                console.dim(f"[进化] 技能 {safe_name!r} 沉淀 {written} 个子文件")
        # 技能评估（闭环第一环）：新建技能无旧版本对照，评估分过低仅告警
        # 建议人工复核，不拦截落盘（评估失败静默跳过，不影响沉淀）
        try:
            score = await get_evaluator().score_skill(md)
            if score is not None and score < CFG.eval_min_score:
                console.warn(
                    f"[进化] 技能 {safe_name!r} 评估分 {score:.2f} 偏低，"
                    "建议人工复核后修正或移除")
        except Exception as e:
            console.dim(f"[进化] 技能 {safe_name!r} 评估跳过：{e}")
        # 5.5 provenance：按写入来源标记 created_by——后台复盘/Agent 沉淀 →
        # agent（进入 curator 管理范围）；用户/主播前台写入 → user（永不自动策展）
        try:
            from plugins.tools.skills import get_skill_manager
            from .provenance import is_background_review
            who = "agent" if is_background_review() else "user"
            get_skill_manager().mark_created_by(safe_name, who)
        except Exception:
            pass
        console.ok(f"[进化] 技能沉淀：{safe_name}（已热加载）")
        METRICS.incr(EVENT_SKILL_SAVED)

    @staticmethod
    def write_skill_resources(skill_dir: Path, resources: list) -> int:
        """把复盘输出的子文件写入技能目录（返回实际写入数）。

        只接受 references/、templates/、scripts/ 开头的相对路径，且解析后
        必须仍在技能目录内（防 ../ 穿越）；单个文件写入失败只告警跳过。
        """
        written = 0
        for item in resources:
            if not isinstance(item, dict):
                continue
            rel = (item.get("path") or "").strip().replace("\\", "/")
            body = (item.get("content") or "").strip()
            if not rel or not body:
                continue
            if not rel.startswith(("references/", "templates/", "scripts/")):
                console.warn(f"[进化] 子文件路径非法（须在 references/templates/"
                             f"scripts 下）：{rel!r}，已跳过")
                continue
            target = (skill_dir / rel).resolve()
            try:
                target.relative_to(skill_dir.resolve())
            except ValueError:
                console.warn(f"[进化] 子文件路径越出技能目录：{rel!r}，已跳过")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body + "\n", encoding="utf-8")
                written += 1
            except OSError as e:
                console.warn(f"[进化] 子文件 {rel!r} 写入失败：{e}")
        return written

    async def apply_patch(self, patch: dict) -> None:
        """修补现有技能：保留 frontmatter，用 LLM 给出的完整正文整体替换。

        改写 SKILL.md 后由 watchdog 热加载，下一轮即可生效。
        """
        name = (patch.get("name") or "").strip()
        new_body = (patch.get("patch") or "").strip()
        if not name or not new_body:
            return
        from plugins.tools.skills import get_skill_manager
        skill = get_skill_manager().get(name)
        if skill is None:
            console.warn(f"[进化] 技能修补失败：技能 {name!r} 不存在")
            return
        if skill.pinned:
            console.warn(f"[进化] 技能修补失败：技能 {name!r} 已被 pin 保护，禁止自动改写")
            return
        try:
            old = skill.location.read_text(encoding="utf-8")
        except OSError as e:
            console.warn(f"[进化] 技能修补读取失败：{e}")
            return
        # 保留原 frontmatter（name/description），仅替换其后的正文
        fm, _ = split_frontmatter(old)
        new = (fm.rstrip() + "\n\n" + new_body + "\n") if fm else (new_body + "\n")
        if _strip_ws(old) == _strip_ws(new):
            console.dim(f"[进化] 技能 {name!r} 的修补内容与现状一致，跳过")
            return
        # 技能评估择优（闭环核心）：新旧正文同一测试集分别打分，
        # 新版更差则回滚保留旧版；评估失败 fail-open 照写不拦截
        try:
            result = await get_evaluator().evaluate_patch(name, old, new)
            if result is not None:
                new_score, old_score = result
                if new_score < old_score:
                    console.warn(
                        f"[进化] 技能修补 {name!r} 评估分下滑 "
                        f"({old_score:.2f}→{new_score:.2f})，回滚保留旧版")
                    METRICS.incr(EVENT_SKILL_PATCH_REVERTED)
                    return
        except Exception as e:
            console.dim(f"[进化] 技能 {name!r} 修补评估跳过：{e}")
        try:
            skill.location.write_text(new, encoding="utf-8")
        except OSError as e:
            console.warn(f"[进化] 技能修补写入失败：{e}")
            return
        # 5.2：修补成功记一次 patches 遥测（生命周期状态机/复盘参考）
        try:
            get_skill_manager().bump_patch(name)
        except Exception:
            pass
        console.ok(f"[进化] 技能修补：{name}（已热加载）")
        METRICS.incr(EVENT_SKILL_PATCHED)

    async def maybe_prune(self, *, client, model) -> None:
        """定期审阅技能库：窄技能合并进 umbrella，过时/低质/重复技能移入 _archived/。

        对标 hermes curator 的 consolidation + pruning：
        - merge：2 个及以上同属一类工作的窄技能 → 正文并入 umbrella 技能后归档
        - archive：过时/失效/低质/重复技能 → 移入 _archived/（可恢复）
        被合并/归档技能目录移出扫描路径后，watchdog 重扫自动从注册表移除。
        技能太少或新技能未满 CFG.prune_min_age 时跳过，防止误清理。
        client/model 由引擎注入（主模型），未就绪时跳过本次。
        """
        from plugins.tools.skills import get_skill_manager
        manager = get_skill_manager()
        # 5.2：审阅前先跑纯确定性生命周期状态机（active→stale→archived 标记，
        # pinned 跳过），让下方候选统计与注入素材反映最新状态
        try:
            transitions = manager.apply_automatic_transitions()
            if transitions.get("active_to_stale") or transitions.get("stale_to_archived"):
                console.dim(
                    f"[进化] 生命周期状态迁移：stale={len(transitions['active_to_stale'])}, "
                    f"archived={len(transitions['stale_to_archived'])}")
        except Exception:
            pass
        now = time.time()
        candidates = [
            s for s in manager.skills
            if now - _file_mtime(s.location) >= CFG.prune_min_age
        ]
        # 5.4 cron 引用保护：被定时任务文本引用的技能不进入候选（防止剪掉在跑引用）
        referenced = _scheduler_referenced_skills([s.name for s in candidates])
        if referenced:
            candidates = [s for s in candidates if s.name not in referenced]
            console.dim(
                f"[进化] {len(referenced)} 个技能被定时任务引用，本次审阅跳过："
                f"{', '.join(sorted(referenced))}")
        # 5.5 用户资产边界：pinned 或用户前台写入（created_by=user）的技能
        # 绝不自动合并/归档（后台 curator 只管理自己沉淀的技能）
        protected = {s.name for s in candidates if s.pinned}
        for s in candidates:
            if s.name in protected:
                continue
            usage = manager.usage_of(s.name)
            if usage and usage.get("created_by") == "user":
                protected.add(s.name)
        if protected:
            candidates = [s for s in candidates if s.name not in protected]
            console.dim(
                f"[进化] {len(protected)} 个技能受保护（pinned/用户写入），"
                f"本次审阅跳过：{', '.join(sorted(protected))}")
        if len(candidates) < 3:
            return  # 技能太少，不值得清理
        if client is None:
            return
        # 候选列表附带真实使用统计（load/views/patches、生命周期状态）与 pin 状态，
        # 供 LLM 审阅参考（hermes 明确 use=0 不是归档依据，只注入数据不改判定标准）
        catalog = []
        for s in candidates:
            usage = manager.usage_of(s.name)
            if usage:
                stat = (f" (state={usage.get('state', 'active')}, "
                        f"loads={usage['loads']}, "
                        f"views={usage.get('views', 0)}, "
                        f"patches={usage.get('patches', 0)}, "
                        f"last_used={time.strftime('%m-%d', time.localtime(usage['last_used']))})")
            else:
                stat = " (从未被加载)"
            pinned = " (pinned)" if s.pinned else ""
            # 5.4：整包技能标记（含捆绑子文件/相对链接，禁止扁平化合并）
            package = " (整包技能,只可整包归档)" if _has_bundled_package(s.location.parent) else ""
            catalog.append(
                f"- {s.name}: {s.description}{stat}{pinned}{package}")
        catalog = "\n".join(catalog)
        # hermes 维护原文入 user 消息，输出协议入 system 消息（保证 JSON 解析兼容）
        user_content = (_HERMES_CURATOR_REVIEW_PROMPT
                        + "\n\n[CANDIDATE SKILLS]\n" + catalog)
        console.dim("[进化] 开始审阅技能库（合并窄技能 / 清理过时技能）...")
        data = await call_llm_json(
            [(client, model, "主模型")],
            [
                {"role": "system", "content": _PRUNE_OUTPUT_PROTOCOL},
                {"role": "user", "content": user_content},
            ],
            label="技能库审阅",
            temperature=0.2,
            max_tokens=1024,
            task="skill.prune",
        )
        if data is None:
            return
        # Curator 增强（3.6，ENABLE_CURATOR 门控）：执行变更前打快照，
        # 归档/合并动作写审计日志，支持按快照整批回滚；关闭时保持原流程
        snapshot_id = None
        if data.get("merge") or data.get("archive"):
            snapshot_id = snapshot_skills(manager)
        merged = self.apply_merges(data.get("merge"), manager)
        to_archive = data.get("archive")
        if not isinstance(to_archive, list):
            if merged:
                console.ok(f"[进化] 技能库合并：{merged} 个窄技能并入 umbrella")
            return
        archived = 0
        for item in to_archive:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            skill = manager.get(name)
            if skill is None:
                continue
            if skill.pinned:
                console.warn(f"[进化] 技能 {name!r} 已被 pin 保护，跳过归档")
                continue
            # 归档前捕获内容 blob（单条目回滚的前提）；关闭时返回 None 不改变原流程
            entry_id = capture_before(name)
            before_hash = _dir_sha256(skill.location.parent) or ""
            if archive_skill(skill, archive_dir_name=CFG.archive_dir_name):
                archived += 1
                METRICS.incr(EVENT_SKILL_ARCHIVED)
                # 5.10：真修剪无转发目标 → 空 absorbed_into 标记（归档不复活）
                _write_absorbed_marker(skill, "")
                append_curator_ledger(
                    "archive", name,
                    reason=(item.get("reason") or "").strip(),
                    detail=f"snapshot={snapshot_id or '-'}",
                    entry_id=entry_id or "",
                    before=before_hash,
                    after=_dir_sha256(skill.location.parent) or "")
        if merged or archived:
            console.ok(f"[进化] 技能库维护：合并 {merged} 个窄技能，归档 {archived} 个技能")

    def apply_merges(self, merges, manager) -> int:
        """执行 Curator 合并：把窄技能正文作为小节并入 umbrella 技能后归档。

        对标 hermes curator 的 consolidation（umbrella-ification）：
        - target 必须存在且未被 pin（umbrella 保持可被继续补丁）
        - absorb 中的技能必须存在且未被 pin，正文以「## 技能名」小节追加进
          umbrella 的 SKILL.md 正文（保留 frontmatter），随后归档原技能目录
        - 5.4 包完整性：含捆绑子文件/相对链接的整包技能禁止扁平化（整体保留）
        - 5.10 引用迁移：归档时 ledger 记 absorbed_into + 归档目录写标记文件，
          并把定时任务文本中的旧技能名改写为 umbrella 名
        返回实际合并的技能数；任何异常跳过该条合并，不影响其余处理。
        """
        if not isinstance(merges, list):
            return 0
        merged = 0
        absorbed_mapping: dict[str, str] = {}
        for item in merges:
            if not isinstance(item, dict):
                continue
            target_name = (item.get("target") or "").strip()
            absorb = item.get("absorb")
            if not target_name or not isinstance(absorb, list):
                continue
            umbrella = manager.get(target_name)
            if umbrella is None:
                console.warn(f"[进化] 技能合并失败：umbrella 技能 {target_name!r} 不存在")
                continue
            if umbrella.pinned:
                console.warn(f"[进化] 技能合并失败：umbrella 技能 {target_name!r} 已被 pin 保护")
                continue
            try:
                umbrella_text = umbrella.location.read_text(encoding="utf-8")
            except OSError as e:
                console.warn(f"[进化] 技能合并读取 {target_name!r} 失败：{e}")
                continue
            sections = []
            for name in absorb:
                if not isinstance(name, str):
                    continue
                name = name.strip()
                skill = manager.get(name)
                if skill is None:
                    console.warn(f"[进化] 技能合并跳过：技能 {name!r} 不存在")
                    continue
                if skill.pinned:
                    console.warn(f"[进化] 技能合并跳过：技能 {name!r} 已被 pin 保护")
                    continue
                if skill.name == umbrella.name:
                    continue
                # 5.4 包完整性：整包技能扁平化会留下悬空链接，只可整体保留/整包归档
                if _has_bundled_package(skill.location.parent):
                    console.warn(
                        f"[进化] 技能合并跳过：{name!r} 为整包技能（含捆绑子文件/"
                        f"相对链接），禁止扁平化为小节，已整体保留")
                    continue
                try:
                    body = skill.location.read_text(encoding="utf-8")
                except OSError as e:
                    console.warn(f"[进化] 技能合并读取 {name!r} 失败：{e}")
                    continue
                # 只取正文（去 frontmatter），作为小节并入 umbrella；
                # 未闭合 frontmatter 时保持原样（split_frontmatter 返回 (全文, "")）
                _, body_text = split_frontmatter(body)
                if body_text:
                    body = body_text
                sections.append((name, body))
            if not sections:
                continue
            # 保留 frontmatter，正文末尾追加各小节（空行分隔）
            fm, base = split_frontmatter(umbrella_text)
            chunks = [base.rstrip()] if base.strip() else []
            chunks += [f"## {name}\n\n{body.rstrip()}" for name, body in sections]
            new_text = (fm.rstrip() + "\n\n" + "\n\n".join(chunks) + "\n"
                        if fm else "\n\n".join(chunks) + "\n")
            # umbrella 改写前捕获 blob，支撑单条目回滚
            umbrella_entry = capture_before(target_name)
            umbrella_before = _dir_sha256(umbrella.location.parent) or ""
            try:
                umbrella.location.write_text(new_text, encoding="utf-8")
            except OSError as e:
                console.warn(f"[进化] 技能合并写入 {target_name!r} 失败：{e}")
                continue
            append_curator_ledger(
                "merge_patch", target_name,
                reason="umbrella 吸收窄技能小节",
                detail=f"absorbed={','.join(n for n, _ in sections)}",
                entry_id=umbrella_entry or "",
                before=umbrella_before,
                after=_dir_sha256(umbrella.location.parent) or "")
            # 合并成功 → 归档被吸收的窄技能目录
            for name, _ in sections:
                skill = manager.get(name)
                if skill is None:
                    continue
                entry_id = capture_before(name)
                before_hash = _dir_sha256(skill.location.parent) or ""
                if archive_skill(skill, archive_dir_name=CFG.archive_dir_name):
                    merged += 1
                    METRICS.incr(EVENT_SKILL_MERGED)
                    # 5.10：归档目录写标记文件 + ledger 记前进目标（引用迁移依据）
                    _write_absorbed_marker(skill, target_name)
                    absorbed_mapping[name] = target_name
                    append_curator_ledger(
                        "merge", name, reason="merged into umbrella",
                        detail=f"target={target_name}",
                        entry_id=entry_id or "",
                        before=before_hash,
                        after=_dir_sha256(skill.location.parent) or "",
                        absorbed_into=target_name)
            console.ok(f"[进化] 技能合并：{target_name} 吸收 "
                       f"{', '.join(n for n, _ in sections)}（已热加载）")
        # 5.10：合并产生的旧名→umbrella 映射改写定时任务文本（失败静默）
        if absorbed_mapping and curator_enabled():
            migrated = _migrate_scheduler_references(absorbed_mapping)
            if migrated:
                console.dim(
                    f"[进化] 定时任务引用迁移：{migrated} 条任务文本中的旧技能名"
                    f"已改写为新 umbrella 名")
        return merged
