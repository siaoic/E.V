"""自我进化引擎的 LLM 提示词与配置常量（原 evolution.py 顶部内容拆分）。

- 4 个提示词常量原样搬入（hermes 原文一字不改，输出协议字段名不动）；
- 路径 / 阈值等顶部常量合并为 EvolutionConfig 数据类，模块级 CFG 单例，
  读取时机与原「模块导入即求值」一致。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ev.utils import config

# ---------------------------------------------------------------------------
# 路径与阈值（原 evolution.py 顶部所有 _XXX_PATH / 阈值常量合并）
# ---------------------------------------------------------------------------

# 话题文件：读 PROJECT_ROOT 内置资源（只读资源根，不随 DATA_ROOT 重定向）
_TOPICS_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "src", "llm", "topics.yml")

# 生效话术建议文件：llm_brain 每轮注入系统提示的共享介质（同时供到期回评）
_ADVICE_ACTIVE_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution", "evolution_advice_active.json")

# 观众画像文件：复盘提炼的长期事实（对标 hermes 的 USER.md/MEMORY.md），
# llm_brain 每轮按关键词召回注入，补充向量记忆检索之外的召回
_PROFILE_PATH = os.path.join(
    config.cfg.DATA_ROOT, "evolution", "evolution_profile.json")


@dataclass(frozen=True)
class EvolutionConfig:
    """进化引擎路径与阈值配置（保持原模块顶部常量语义）。"""

    topics_path: str = _TOPICS_PATH
    advice_active_path: str = _ADVICE_ACTIVE_PATH
    profile_path: str = _PROFILE_PATH
    # 画像条目上限：超出丢弃最旧（防止画像无限膨胀稀释召回）
    profile_max: int = 30
    # 话术建议生效时长：到期后由复盘回评续期保留或移除（秒）
    advice_ttl_seconds: int = 86400
    # 技能库清理节流：审阅间隔（秒，每天至多一次）
    prune_interval_seconds: int = 86400
    # 技能库清理最小年龄：新沉淀的技能需存在这么久才参与审阅（防止误归档）
    prune_min_age: int = 7 * 86400
    # 技能评估最低分：新建技能评估分低于该值告警（建议人工复核）；
    # 修补时新版低于旧版直接回滚（择优保留，对标 GEPA 的 fitness 选择）
    eval_min_score: float = 0.35
    # 归档目录名：被清理技能移入 <技能根>/_archived/<技能名>/，不再被扫描收录
    archive_dir_name: str = "_archived"
    # 进化话题的默认冷却与分类（与 topics.yml 其它条目一致）
    topic_category: str = "learned"
    topic_cooldown_minutes: int = 3


CFG = EvolutionConfig()

# ---------------------------------------------------------------------------
# hermes-agent-main 原提示词（一字不改复制，来源见注释）。作为 user 消息下发；
# 项目侧的结构化输出要求放在独立的 _*_OUTPUT_PROTOCOL（system 消息），
# 以保证与现有 JSON 解析/落盘逻辑兼容——原文不删改、不翻译、不追加。
# ---------------------------------------------------------------------------

# 来源：hermes-agent-main/agent/background_review.py 的 _COMBINED_REVIEW_PROMPT
# （对话后后台复审：记忆 + 技能两个维度，含信号清单、优先级、禁止清单）
_HERMES_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most "
    "sessions produce at least one skill update. A pass that does "
    "nothing is a missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries.\n\n"
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS skill "
    "signal, not just a memory signal. 'stop doing X', 'don't format "
    "like this', 'I hate when you Y' — embed the lesson in the skill "
    "that governs that task so the next session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patch it now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via /skill-name or skill_view in the conversation. If one "
    "of them covers the learning, PATCH it first. It was in play; "
    "it's the right place — provided it is curator-managed. Protected "
    "and user-owned skills are off-limits however relevant; fall "
    "through when one of those is the best fit.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (skills_list + skill_view to "
    "find the right one). Patch it.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via "
    "skill_manage action=write_file. Three kinds: "
    "`references/<topic>.md` for session-specific detail OR condensed "
    "knowledge banks (quoted research, API docs excerpts, domain "
    "notes) written concise and task-focused; `templates/<name>.<ext>` "
    "for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification, fixture generators, probes). Add a one-line "
    "pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. "
    "Name at the class level — NOT a PR number, error string, "
    "codename, library-alone name, or 'fix-X / debug-Y' session "
    "artifact. If the name only fits today's task, fall back to (1), "
    "(2), or (3).\n\n"
    "User-preference embedding: when the user complains about how "
    "you handled a task, update the skill that governs that task — "
    "memory alone isn't enough. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; "
    "skills say 'how to do this class of task for this user'. Both "
    "should carry user-preference lessons when relevant.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "  • Skills in skills.external_dirs (externally owned).\n"
    "  • PINNED skills (marked via 'hermes curator pin'). Pin blocks "
    "autonomous writes entirely — content updates included — because no "
    "user is present to consent. Only a foreground session can change one.\n"
    "  • USER-OWNED skills — anything not curator-managed (hand-written, "
    "URL-installed, or created by a foreground agent at the user's "
    "request). Your writes to these WILL be refused, including to skills "
    "loaded or consulted this session. If one is wrong, say so in your "
    "reply and recommend 'hermes curator adopt <name>' instead.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "  • Unresolved failures: if the session ended WITHOUT actually "
    "finding a working method — you tried several things, none worked, "
    "and told the user to check manually — do NOT write those attempts "
    "up as a 'reliable workflow' or 'recommended approach'. That presents "
    "an untested sequence of failures as validated guidance a future "
    "session will trust and repeat. Either say 'Nothing to save', or, "
    "only if you are independently confident of a real working alternative "
    "(not something you are merely guessing might work), capture ONLY that "
    "alternative — never the dead ends, and never dressed up as best practice.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    "genuinely nothing stands out on either, say 'Nothing to save.' "
    "and stop — but don't reach for that conclusion as a default."
)

# 复盘输出协议（独立 system 消息）：把 hermes 复盘结论固化为六类 JSON，
# 解析与落盘依赖此结构，不得改动字段名与取值类型。
_REVIEW_OUTPUT_PROTOCOL = (
    "你是 AI 虚拟主播的自我进化管家。用户消息中的复盘指令（英文原文）与最近"
    "对话记录请结合执行。请把复盘结论输出为一份 JSON 对象，包含七个可选字段：\n"
    "1. skill：出现「值得沉淀的可复用方法或知识」时填 {name（英文短名小写下划线 ≤30 字符）, "
    "description（一句话 ≤80 字符，说明能力而非实现，不用「强大、全面」类营销词）, "
    "content（完整可执行指令，Markdown，开头用 # 技能名，分步骤，≤300 字）, "
    "resources（可选：需要沉淀的会话细节/参考/模板子文件，0-3 个，每项 "
    "{path（相对技能目录，必须以 references/、templates/ 或 scripts/ 开头，"
    "如 references/mental-models.md）, content（文件内容，Markdown，≤500 字）}；"
    "没有则省略或填 null）}；没有则填 null\n"
    "2. topics：0-2 个观众感兴趣、有直播价值的新话题，每项 {concept（一句话，带人设风味，20-60 字）, "
    "tags（2-4 个英文标签）}；没有则填 []\n"
    "3. lesson：做得好的/差的地方提炼一条经验教训（一句话，≤50 字）；环境性/一次性问题"
    "（设备抽风、观众无来由的恶意）不要沉淀为永久经验；没有则填 null\n"
    "4. advice：对人设或直播话术的优化建议（一段话，≤100 字）；没有则填 null\n"
    "5. skill_patch：发现现有技能在执行中失效（观众负反馈点名、观众吐槽、"
    "按技能操作出错、方式过时）时填 "
    "{name（现有技能名，与 Available skills 列表一致）, reason（一句话失效原因，≤60 字）, "
    "patch（修正后的完整指令正文，Markdown，开头用 # 技能名，≤300 字，整体替换原正文，不含 frontmatter）}；"
    "若 [AUDIENCE FEEDBACK] 块中的负反馈指向某技能，应优先输出 skill_patch；"
    "没有则填 null\n"
    "6. advice_status：用户消息末尾附带了「待评估话术建议」列表时，对每条输出 "
    "{text（原样返回该建议文本）, keep（true=续期保留 / false=已失效移除）, "
    "negative_hits（该建议近期的负反馈命中次数，供评估参考）}，逐条评估不要遗漏；"
    "负反馈命中多的建议应倾向 keep=false；"
    "没有待评估建议时填 []\n"
    "7. profile：提炼最近对话中关于「观众是谁 / 观众偏好 / 主播应长期保持的行为」的"
    "长期事实画像，0-2 条，每项 {owner（归属者：self=主播自己 / 观众名，不确定填 chao）, "
    "fact（一句话 ≤50 字）, action（可选：add=新增 / replace=观众推翻旧画像（附 old_fact=被推翻的旧事实原文）/ "
    "remove=观众明确否定该画像；缺省 add）}；"
    "当观众明确纠正/否定某条画像（如「别叫我这个」）时输出 replace 或 remove；"
    "没有则填 []\n"
    "输出纪律：只输出 JSON，不要任何多余文字、代码块标记或注释；"
    "每个字段写完重新数一遍长度，超了就删；"
    "全部为 null/[] 是合法选项，但不应是默认。"
)

# 来源：hermes-agent-main/agent/curator.py 的 CURATOR_REVIEW_PROMPT
# （技能库后台维护员：目标形状 + Hard rules + 判定标准 + 结构化输出）
_HERMES_CURATOR_REVIEW_PROMPT = (
    "You are running as Hermes' background skill CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit and not a "
    "duplicate-finder.\n\n"
    "The goal of the skill collection is a LIBRARY OF CLASS-LEVEL "
    "INSTRUCTIONS AND EXPERIENTIAL KNOWLEDGE. A collection of hundreds of "
    "narrow skills where each one captures one session's specific bug is "
    "a FAILURE of the library — not a feature. An agent searching skills "
    "matches on descriptions, not on exact names (note: long descriptions "
    "are truncated to 57 chars in the system prompt skill index — keep the "
    "trigger class in that window). One broad umbrella "
    "skill with labeled subsections beats five narrow siblings for "
    "discoverability, not the other way around.\n\n"
    "The right target shape is CLASS-LEVEL skills with rich SKILL.md "
    "bodies + `references/`, `templates/`, and `scripts/` subfiles for "
    "session-specific detail — not one-session-one-skill micro-entries.\n\n"
    "Hard rules — do not violate:\n"
    "1. DO NOT touch bundled, hub-installed, or external-dir skills "
    "(`skills.external_dirs`). The candidate list below is already filtered "
    "to local curator-managed skills only; external skills are externally "
    "owned and read-only to this background curator.\n"
    "2. DO NOT delete any skill. Archiving (moving the skill's directory "
    "into ~/.hermes/skills/.archive/) is the maximum destructive action. "
    "Archives are recoverable; deletion is not.\n"
    "3. DO NOT touch skills shown as pinned=yes. Skip them entirely.\n"
    "3b. DO NOT archive, delete, consolidate, move, or otherwise modify any "
    "skill named in the protected built-ins list (currently: plan). These "
    "back load-bearing UX (slash-command entry points referenced in docs and "
    "tips) and are filtered out of the candidate list below — never resurrect "
    "one as an archive or absorb target.\n"
    "3c. DO NOT archive or prune any skill marked `cron=yes` in the candidate "
    "list. A cron job depends on it and will fail to load it on its next "
    "run. You MAY still consolidate it into an umbrella — but only because "
    "the curator rewrites cron job skill references to follow consolidations; "
    "never simply prune it.\n"
    "4. DO NOT use usage counters as a reason to skip consolidation. The "
    "counters are new and often mostly zero. Judge overlap on CONTENT, "
    "not on use_count. 'use=0' is not evidence a skill is valuable; it's "
    "absence of evidence either way. Corollary: 'use=0' is ALSO not a "
    "reason to PRUNE a skill. Never archive a never-used skill (use=0) "
    "unless it is at least 30 days old (check last_activity / created date) "
    "AND its content is genuinely obsolete or fully absorbed elsewhere — a "
    "recently-created skill simply may not have had its trigger come up yet.\n"
    "5. DO NOT reject consolidation on the grounds that 'each skill has "
    "a distinct trigger'. Pairwise distinctness is the wrong bar. The "
    "right bar is: 'would a human maintainer write this as N separate "
    "skills, or as one skill with N labeled subsections?' When the "
    "answer is the latter, merge.\n\n"
    "How to work — not optional:\n"
    "1. Scan the full candidate list. Identify PREFIX CLUSTERS (skills "
    "sharing a first word or domain keyword). Examples you are likely "
    "to find: hermes-config-*, hermes-dashboard-*, gateway-*, codex-*, "
    "ollama-*, anthropic-*, gemini-*, mcp-*, salvage-*, pr-*, "
    "competitor-*, python-*, security-*, etc. Expect 10-25 clusters.\n"
    "2. For each cluster with 2+ members, do NOT ask 'are these pairs "
    "overlapping?' — ask 'what is the UMBRELLA CLASS these skills all "
    "serve? Would a maintainer name that class and write one skill for "
    "it?' If yes, pick (or create) the umbrella and absorb the siblings "
    "into it.\n"
    "3. Three ways to consolidate — use the right one per cluster:\n"
    "   a. MERGE INTO EXISTING UMBRELLA — one skill in the cluster is "
    "already broad enough to be the umbrella (example: `pr-triage-"
    "salvage` for the PR review cluster). Patch it to add a labeled "
    "section for each sibling's unique insight, then archive the "
    "siblings.\n"
    "   b. CREATE A NEW UMBRELLA SKILL.md — no existing member is broad "
    "enough. Use skill_manage action=create to write a new class-level "
    "skill whose SKILL.md covers the shared workflow and has short "
    "labeled subsections. Archive the now-absorbed narrow siblings.\n"
    "   c. DEMOTE TO REFERENCES/TEMPLATES/SCRIPTS — a sibling has "
    "narrow-but-valuable session-specific content. Move it into the "
    "umbrella's appropriate support directory:\n"
    "      • `references/<topic>.md` for session-specific detail OR "
    "condensed knowledge banks (quoted research, API docs excerpts, "
    "domain notes, provider quirks, reproduction recipes)\n"
    "      • `templates/<name>.<ext>` for starter files meant to be "
    "copied and modified\n"
    "      • `scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification scripts, fixture generators, probes)\n"
    "      Then archive the old sibling. Use `terminal` with `mkdir -p "
    "~/.hermes/skills/<umbrella>/references/ && mv ... <umbrella>/"
    "references/<topic>.md` (or templates/ / scripts/).\n\n"
    "Package integrity — not optional:\n"
    "Before demoting or archiving a skill, inspect it as a COMPLETE "
    "directory package, not just SKILL.md. A skill root may include "
    "`references/`, `templates/`, `scripts/`, and `assets/`; `skill_view` "
    "discovers those relative to the skill root. A reference markdown file "
    "inside another skill is NOT a new skill root and does not get its own "
    "linked-file discovery.\n"
    "If the source skill has support files OR SKILL.md contains relative "
    "links such as `references/...`, `templates/...`, `scripts/...`, or "
    "`assets/...`, DO NOT flatten only SKILL.md into "
    "`<umbrella>/references/<old>.md`. Choose one safe path instead:\n"
    "   • keep it as a standalone skill, OR\n"
    "   • fully merge it by re-homing every needed support file into the "
    "umbrella's canonical `references/`, `templates/`, `scripts/`, or "
    "`assets/` directories AND rewrite the destination instructions to "
    "the new paths, OR\n"
    "   • archive the entire original skill package unchanged.\n"
    "Never leave archived/demoted instructions pointing at files that were "
    "left behind under the old skill directory.\n"
    "4. Also flag skills whose NAME is too narrow (contains a PR number, "
    "a feature codename, a specific error string, an 'audit' / "
    "'diagnosis' / 'salvage' session artifact). These almost always "
    "belong as a subsection or support file under a class-level umbrella.\n"
    "5. Iterate. After one consolidation round, scan the remaining set "
    "and look for the NEXT umbrella opportunity. Don't stop after 3 "
    "merges.\n\n"
    "Your toolset:\n"
    "  - skills_list, skill_view        — read the current landscape\n"
    "  - skill_manage action=patch      — add sections to the umbrella\n"
    "  - skill_manage action=create     — create a new umbrella SKILL.md\n"
    "  - skill_manage action=write_file — add a references/, templates/, "
    "or scripts/ file under an existing skill (the skill must already "
    "exist)\n"
    "  - skill_manage action=delete     — archive a skill. MUST pass "
    "`absorbed_into=<umbrella>` when you've merged its content into another "
    "skill, or `absorbed_into=\"\"` when you're truly pruning with no "
    "forwarding target. This drives cron-job skill-reference migration — "
    "guessing from your YAML summary after the fact is fragile.\n"
    "  - terminal                       — move LOCAL candidate content into "
    "a support subfile when package integrity requires it; never mv, cp, rm, "
    "patch, or rewrite bundled, hub-installed, or external-dir skills\n\n"
    "'keep' is a legitimate decision ONLY when the skill is already a "
    "class-level umbrella and none of the proposed merges would improve "
    "discoverability. 'This is narrow but distinct from its siblings' "
    "is NOT a reason to keep — it's a reason to move it under an "
    "umbrella as a subsection or support file.\n\n"
    "Expected output: real umbrella-ification. Process every obvious "
    "cluster. If you end the pass with fewer than 10 archives, you "
    "stopped too early — go back and look at the clusters you left "
    "alone.\n\n"
    "When done, write a human summary AND a structured machine-readable "
    "block so downstream tooling can distinguish consolidation from "
    "pruning. Format EXACTLY:\n\n"
    "## Structured summary (required)\n"
    "```yaml\n"
    "consolidations:\n"
    "  - from: <old-skill-name>\n"
    "    into: <umbrella-skill-name>\n"
    "    reason: <one short sentence — why merged, not just 'similar'>\n"
    "prunings:\n"
    "  - name: <skill-name>\n"
    "    reason: <one short sentence — why archived with no merge target>\n"
    "```\n\n"
    "Every skill you moved to .archive/ MUST appear in exactly one of the "
    "two lists. If you consolidated X into umbrella Y (patched Y, wrote "
    "a references file to Y, or created Y with X's content absorbed), X "
    "goes under `consolidations` with `into: Y`. If you archived X with "
    "no absorption — truly stale, irrelevant, or obsolete — X goes under "
    "`prunings`. Leave a list empty (`consolidations: []`) if none. Do "
    "not omit the block. The block comes AFTER your human-readable "
    "summary of clusters processed, patches made, and decisions left alone."
)

# 技能库审阅输出协议（独立 system 消息）：把 hermes 维护结论固化为 JSON，
# 归档/合并逻辑依赖此结构，不得改动字段名与取值类型。
# 输出格式 EXACTLY：
#   {"merge": [{"target": <umbrella技能名>, "absorb": [<技能名>...], "reason": <一句话原因>}],
#    "archive": [{"name": <技能名>, "reason": <一句话原因，≤50字>}]}
# merge：把多个窄技能合并进一个「类级 umbrella 技能」（对标 hermes curator 的
#   consolidation）——target 必须是候选列表中已存在的技能（建议选覆盖面最宽的），
#   absorb 是被吸收的窄技能名列表（其正文将以小节形式并入 target，随后被归档）。
#   只有当 N 个技能确实属于同一类工作、合并后明显更利于发现时才输出 merge。
# archive：只归档有充分理由的过时/失效/低质/重复技能。
# 没有要处理的技能时输出 {"merge": [], "archive": []}；只输出 JSON，不要任何多余文字。
_PRUNE_OUTPUT_PROTOCOL = (
    "你是 AI 虚拟主播的技能库维护员。用户消息中的维护指令（英文原文）与候选技能"
    "列表请结合执行。请把审阅结论输出为一份 JSON 对象，格式 EXACTLY：\n"
    "{\"merge\": [{\"target\": <umbrella技能名>, \"absorb\": [<技能名>...], "
    "\"reason\": <一句话原因，≤50字>}], \"archive\": [{\"name\": <技能名>, "
    "\"reason\": <一句话原因，≤50字>}]}\n"
    "merge：把 2 个及以上同属一类工作的窄技能合并进一个已存在的 umbrella 技能"
    "（target 必须在候选列表中，absorb 为被吸收的窄技能名，其正文将作为小节并入 target"
    " 后归档）；只有确实属于同一类工作、合并后更利于发现时才合并。\n"
    "archive：只归档有充分理由的过时/失效/低质/重复技能；没有要归档的技能时填 []。\n"
    "两者都没有时输出 {\"merge\": [], \"archive\": []}；只输出 JSON，不要任何多余文字。"
)
