"""技能沉淀 / 修补 / 合并 / 归档（原 EvolutionEngine 的技能职责拆分）。

- save_skill：复盘结果沉淀 SKILL.md + 子文件（watchdog 热加载，评估告警）
- apply_patch：技能自我改进（保留 frontmatter 整体替换正文，评估择优回滚）
- maybe_prune：定期审阅技能库（Curator：窄技能合并进 umbrella + 过时技能归档）
- apply_merges：执行合并（正文并入 umbrella 后归档被吸收技能）
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from src.llm.skill_eval import get_evaluator
from src.utils import config, console

from ._utils import _file_mtime, _strip_ws, archive_skill, call_llm_json, split_frontmatter
from .metrics import (
    EVENT_SKILL_ARCHIVED, EVENT_SKILL_MERGED, EVENT_SKILL_PATCH_REVERTED,
    EVENT_SKILL_PATCHED, EVENT_SKILL_SAVED, METRICS,
)
from .prompts import (
    CFG, _HERMES_CURATOR_REVIEW_PROMPT, _PRUNE_OUTPUT_PROTOCOL,
)


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
        now = time.time()
        candidates = [
            s for s in manager.skills
            if now - _file_mtime(s.location) >= CFG.prune_min_age
        ]
        if len(candidates) < 3:
            return  # 技能太少，不值得清理
        if client is None:
            return
        # 候选列表附带真实使用统计（load 次数/最近使用）与 pin 状态，
        # 供 LLM 审阅参考（hermes 明确 use=0 不是归档依据，只注入数据不改判定标准）
        catalog = []
        for s in candidates:
            usage = manager.usage_of(s.name)
            stat = (f" (loads={usage['loads']}, "
                    f"last_used={time.strftime('%m-%d', time.localtime(usage['last_used']))})"
                    if usage else " (从未被加载)")
            pinned = " (pinned)" if s.pinned else ""
            catalog.append(f"- {s.name}: {s.description}{stat}{pinned}")
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
        )
        if data is None:
            return
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
            if archive_skill(skill, archive_dir_name=CFG.archive_dir_name):
                archived += 1
                METRICS.incr(EVENT_SKILL_ARCHIVED)
        if merged or archived:
            console.ok(f"[进化] 技能库维护：合并 {merged} 个窄技能，归档 {archived} 个技能")

    def apply_merges(self, merges, manager) -> int:
        """执行 Curator 合并：把窄技能正文作为小节并入 umbrella 技能后归档。

        对标 hermes curator 的 consolidation（umbrella-ification）：
        - target 必须存在且未被 pin（umbrella 保持可被继续补丁）
        - absorb 中的技能必须存在且未被 pin，正文以「## 技能名」小节追加进
          umbrella 的 SKILL.md 正文（保留 frontmatter），随后归档原技能目录
        返回实际合并的技能数；任何异常跳过该条合并，不影响其余处理。
        """
        if not isinstance(merges, list):
            return 0
        merged = 0
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
            try:
                umbrella.location.write_text(new_text, encoding="utf-8")
            except OSError as e:
                console.warn(f"[进化] 技能合并写入 {target_name!r} 失败：{e}")
                continue
            # 合并成功 → 归档被吸收的窄技能目录
            for name, _ in sections:
                skill = manager.get(name)
                if skill is None:
                    continue
                if archive_skill(skill, archive_dir_name=CFG.archive_dir_name):
                    merged += 1
                    METRICS.incr(EVENT_SKILL_MERGED)
            console.ok(f"[进化] 技能合并：{target_name} 吸收 "
                       f"{', '.join(n for n, _ in sections)}（已热加载）")
        return merged
