"""自我进化引擎：对话后后台复盘 + 定期自我提示，一次 LLM 调用沉淀七类进化。

对标 hermes-agent 的学习闭环（技能自动沉淀 / 技能自我改进 / 话题进化 /
行为反思 / 话术优化与回评 / 技能库清理 / 定期自我评估），用轻量的
「LLM 自评 + 生成」实现，不引入外部框架：

- 技能沉淀：从对话中识别「可复用的方法/知识」，生成 SKILL.md 写入
  技能目录（Skills 模块 watchdog 监听自动热加载，沉淀后立即生效）
- 技能修补：复盘发现现有技能执行失效时，自动改写其 SKILL.md 正文
  （保留 frontmatter，watchdog 热加载后立即生效）
- 技能清理：定期审阅技能库，把过时/低质/重复的技能移入 _archived/
  （不再进入 Available skills 列表）
- 话题进化：从互动中提炼新话题，追加 topics.yml 并注入运行中的
  ProactiveEngine（重启后由 topics.yml 自动加载，双重生效）
- 行为反思：评估回复质量，把经验教训写入记忆库（后续检索可带出）
- 话术闭环：话术建议登记为「生效建议」注入系统提示，到期后由复盘
  回评续期保留或移除，形成 建议 → 生效 → 回评 的闭环；
  全部建议同时追加到 data/evolution_advice.md 供用户审阅
- 观众画像：提炼「观众是谁 / 偏好 / 主播应长期保持的行为」等长期事实，
  落盘 data/evolution_profile.json（对标 hermes 的 USER.md/MEMORY.md），
  由 llm_brain 每轮按关键词召回注入，补充向量记忆检索之外的召回
- 技能子文件沉淀：技能可随 SKILL.md 一起落盘 references/templates/scripts
  子文件（对标 hermes 技能包的细节沉淀），由 read_skill_resource 按需读取
- 技能合并（Curator consolidation）：定期审阅时把同属一类工作的窄技能
  正文并入 umbrella 技能后归档（对标 hermes 的 umbrella-ification）
- pin 保护：SKILL.md frontmatter 标 pinned: true 的技能禁止自动修补/合并/归档
- GEPA 系统提示词进化：独立模块 prompt_evo.py，分析对话失败点变异候选行为
  策略段，与当前策略同批评审择优落盘 evolution_policy.json，由 llm_brain
  注入系统提示（对标 hermes 的 GEPA 变异-评估-择优）

触发路径：
- 对话后复盘（maybe_review）：节流 + 轮次阈值判定，达标才调用 LLM
- 定期自我提示（periodic_tick）：后台按 EVOLUTION_PERIODIC_INTERVAL
  检查，空闲期主动补一次复盘（对标 hermes 定期自我评估），两者共享
  节流状态与互斥锁，不重复消费 token；
技能清理独立节流（每天至多一次）。
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import yaml
from openai import AsyncOpenAI

from src.llm.skill_eval import get_evaluator
from src.utils import config, console

_TOPICS_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "src", "llm", "topics.yml")

# 生效话术建议文件：llm_brain 每轮注入系统提示的共享介质（同时供到期回评）
_ADVICE_ACTIVE_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "data", "evolution_advice_active.json")

# 观众画像文件：复盘提炼的长期事实（对标 hermes 的 USER.md/MEMORY.md），
# llm_brain 每轮按关键词召回注入，补充向量记忆检索之外的召回
_PROFILE_PATH = os.path.join(
    config.cfg.PROJECT_ROOT, "data", "evolution_profile.json")

# 画像条目上限：超出丢弃最旧（防止画像无限膨胀稀释召回）
_PROFILE_MAX = 30

# 话术建议生效时长：到期后由复盘回评续期保留或移除（秒）
_ADVICE_TTL_SECONDS = 86400

# 技能库清理节流：审阅间隔（秒，每天至多一次）
_PRUNE_INTERVAL_SECONDS = 86400

# 技能库清理最小年龄：新沉淀的技能需存在这么久才参与审阅（防止误归档）
_PRUNE_MIN_AGE = 7 * 86400

# 技能评估最低分：新建技能评估分低于该值告警（建议人工复核）；
# 修补时新版低于旧版直接回滚（择优保留，对标 GEPA 的 fitness 选择）
_EVAL_MIN_SCORE = 0.35

# 归档目录名：被清理技能移入 <技能根>/_archived/<技能名>/，不再被扫描收录
_ARCHIVE_DIR = "_archived"

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
    "5. skill_patch：发现现有技能在执行中失效（观众吐槽、按技能操作出错、方式过时）时填 "
    "{name（现有技能名，与 Available skills 列表一致）, reason（一句话失效原因，≤60 字）, "
    "patch（修正后的完整指令正文，Markdown，开头用 # 技能名，≤300 字，整体替换原正文，不含 frontmatter）}；"
    "没有则填 null\n"
    "6. advice_status：用户消息末尾附带了「待评估话术建议」列表时，对每条输出 "
    "{text（原样返回该建议文本）, keep（true=续期保留 / false=已失效移除）}，逐条评估不要遗漏；"
    "没有待评估建议时填 []\n"
    "7. profile：提炼最近对话中关于「观众是谁 / 观众偏好 / 主播应长期保持的行为」的"
    "长期事实画像，0-2 条，每项 {owner（归属者：self=主播自己 / 观众名，不确定填 chao）, "
    "fact（一句话 ≤50 字）}；没有则填 []\n"
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

# 进化话题的默认冷却与分类（与 topics.yml 其它条目一致）
_TOPIC_COOLDOWN_MINUTES = 3
_TOPIC_CATEGORY = "learned"


def _format_turns(turns: list[dict]) -> str:
    """把对话轮次格式化为复盘素材文本（区分弹幕/主播/AI 三方角色）。"""
    from src.memory import memory
    return memory.format_turns_text(turns)


def _parse_review_json(content: str) -> dict:
    """容错解析复盘 JSON 对象：直接解析失败后截取首个 { 到末尾 } 兜底。"""
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        pass
    start = content.find("{")
    if start < 0:
        return {}
    end = content.rfind("}")
    if end <= start:
        return {}
    try:
        data = json.loads(content[start:end + 1])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _make_topic_id(concept: str) -> str:
    """根据话题内容生成稳定 id（learned_ + 内容哈希前 8 位）。"""
    digest = hashlib.md5(concept.encode("utf-8")).hexdigest()[:8]
    return f"learned_{digest}"


def _strip_ws(text: str) -> str:
    """去掉全部空白字符，用于判断技能正文是否发生实质变化。"""
    return "".join(text.split())


def _file_mtime(path: Path) -> float:
    """文件最后修改时间戳（读取失败时返回 0，视为刚创建不参与清理）。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------- 生效话术建议（data/evolution_advice_active.json） ----------

def _load_active_advice() -> list[dict]:
    """读取生效话术建议列表（文件缺失/损坏时返回空列表）。"""
    try:
        with open(_ADVICE_ACTIVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_active_advice(items: list[dict]) -> None:
    """覆写生效话术建议列表。"""
    try:
        os.makedirs(os.path.dirname(_ADVICE_ACTIVE_PATH), exist_ok=True)
        with open(_ADVICE_ACTIVE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except OSError as e:
        console.warn(f"[进化] 写入生效话术建议失败：{e}")


def _append_active_advice(text: str) -> None:
    """登记一条生效中的话术建议（含有效期，到期由复盘回评决定去留）。"""
    now = time.time()
    items = _load_active_advice()
    items.append({"text": text, "created": now, "expires": now + _ADVICE_TTL_SECONDS})
    _save_active_advice(items)


def _pending_advice_text() -> list[str]:
    """返回已到期的话术建议文本（供复盘时让 LLM 评估是否续期）。"""
    now = time.time()
    return [
        (it.get("text") or "").strip()
        for it in _load_active_advice()
        if (it.get("expires") or 0) <= now
    ]


class EvolutionEngine:
    """自我进化引擎：节流判定 → 一次 LLM 复盘 → 六类成果落地。"""

    def __init__(self) -> None:
        self._last_review = 0.0      # 上次复盘时间戳（节流用）
        self._last_prune = 0.0       # 上次技能库审阅时间戳（节流用）
        self._turns_since_review = 0  # 上次复盘后新增的对话轮次
        self._review_lock = asyncio.Lock()  # 串行化对话后复盘与定期复盘
        self._client: AsyncOpenAI | None = None
        self._model = ""
        # 主模型客户端（复盘故障兜底 / 技能审阅）
        self._main_client: AsyncOpenAI | None = None
        self._main_model = ""

    def _ensure_client(self) -> AsyncOpenAI | None:
        """按 BUTLER_*（回退 LLM_*）构建管家客户端（复盘首选，智谱）。

        复盘交给管家模型（智谱 glm-4v-flash）执行，不占用主对话 DeepSeek
        并发、不与对话互相限流；且复盘由调用方放入后台任务，不阻塞对话。
        """
        if self._client is None:
            base_url = (config.cfg.BUTLER_BASE_URL
                        or config.cfg.LLM_BASE_URL or "").strip()
            api_key = (config.cfg.BUTLER_API_KEY
                       or config.cfg.LLM_API_KEY or "").strip()
            self._model = (config.cfg.BUTLER_MODEL
                           or config.cfg.LLM_MODEL or "").strip()
            if not (base_url and api_key and self._model):
                return None
            self._client = self._build_client(base_url, api_key)
        return self._client

    def _ensure_main_client(self) -> AsyncOpenAI | None:
        """按 LLM_* 构建主模型客户端（DeepSeek）：复盘故障兜底 + 技能审阅。"""
        if self._main_client is None:
            base_url = (config.cfg.LLM_BASE_URL or "").strip()
            api_key = (config.cfg.LLM_API_KEY or "").strip()
            self._main_model = (config.cfg.LLM_MODEL or "").strip()
            if not (base_url and api_key and self._main_model):
                return None
            self._main_client = self._build_client(base_url, api_key)
        return self._main_client

    @staticmethod
    def _build_client(base_url: str, api_key: str) -> AsyncOpenAI | None:
        """构建 OpenAI 兼容客户端；缺配置返回 None。"""
        if not (base_url and api_key):
            return None
        return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60.0)

    async def maybe_review(self, turns: list[dict],
                           proactive=None) -> None:
        """对话结束后调用：计数 + 节流/轮次阈值判定，达标才复盘。"""
        cfg = config.cfg
        if not cfg.EVOLUTION_ENABLED:
            return
        self._turns_since_review += 1
        if not turns:
            return
        async with self._review_lock:
            # 拿锁后二次判定（与定期提示并发时防止重复触发）
            now = time.time()
            if now - self._last_review < cfg.EVOLUTION_MIN_INTERVAL:
                return
            if self._turns_since_review < cfg.EVOLUTION_MIN_TURNS:
                return
            self._turns_since_review = 0
            self._last_review = now
            try:
                await self._do_review(turns[-12:], proactive)
            except Exception as e:
                console.warn(f"[进化] 复盘失败（不影响运行）：{e}")
            await self._maybe_run_prune()

    async def periodic_tick(self, turns: list[dict],
                            proactive=None) -> None:
        """定期自我提示：空闲期主动补一次复盘（对标 hermes 定期自我评估）。

        与 maybe_review 共享节流状态：仅当「距上次复盘已达标且上次复盘后有
        新增对话轮次（当时未凑够轮数或已凑够但时间未到）」时才补刀，
        避免用旧对话重复消费 token。
        """
        cfg = config.cfg
        if not cfg.EVOLUTION_ENABLED:
            return
        if not turns:
            return
        async with self._review_lock:
            now = time.time()
            if now - self._last_review < cfg.EVOLUTION_MIN_INTERVAL:
                return
            if self._turns_since_review < 1:
                return  # 上次复盘后没有新对话，不重复复盘
            self._turns_since_review = 0
            self._last_review = now
            try:
                await self._do_review(turns[-12:], proactive)
            except Exception as e:
                console.warn(f"[进化] 定期复盘失败（不影响运行）：{e}")
            await self._maybe_run_prune()
            await self._maybe_run_prompt_evo(turns[-12:])

    async def _maybe_run_prompt_evo(self, turns: list[dict]) -> None:
        """GEPA 系统提示词进化：定期复盘后顺带触发（独立节流，fail-open）。

        分析对话失败点变异候选行为策略段，与当前策略同批评审择优，落盘后由
        llm_brain 注入系统提示（对标 hermes 的 GEPA）。任何失败不影响运行。
        """
        if not turns:
            return
        try:
            from src.llm.prompt_evo import get_evolver
            await get_evolver().maybe_evolve(turns)
        except Exception as e:
            console.dim(f"[进化] GEPA 提示词进化跳过：{e}")

    async def _maybe_run_prune(self) -> None:
        """节流执行技能库审阅（每天至多一次，失败不影响运行）。"""
        now = time.time()
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        try:
            await self._maybe_prune_skills()
        except Exception as e:
            console.warn(f"[进化] 技能库审阅失败（不影响运行）：{e}")

    # ---------- 复盘执行 ----------

    async def _do_review(self, recent: list[dict], proactive) -> None:
        """调用 LLM 复盘最近对话，解析后落地六类成果。"""
        text = _format_turns(recent)
        if not text:
            return
        # 附上已到期的话术建议，让 LLM 一并回评续期/移除
        pending = _pending_advice_text()
        if pending:
            text += ("\n\n### 待评估的话术建议（逐条判断 keep 决定续期或移除）\n"
                     + "\n".join(f"- {t}" for t in pending))
        # hermes 复盘原文入 user 消息，输出协议入 system 消息（保证 JSON 解析兼容）
        user_content = (_HERMES_COMBINED_REVIEW_PROMPT
                        + "\n\n[CONVERSATION TO REVIEW]\n" + text)
        # 追加技能使用统计：让 LLM 修补/清理决策有真实使用数据支撑
        # （对标 hermes 的 skill usage counters，仅作参考不作硬性依据）
        try:
            from src.llm.tools.skills import get_skill_manager
            usage = get_skill_manager().usage_section()
            if usage:
                user_content += "\n\n[SKILL USAGE STATS]\n" + usage
        except Exception:
            pass
        console.dim("[进化] 开始复盘最近对话...")
        # 候选模型链：管家模型（智谱，首选）→ 主模型（DeepSeek，故障兜底）。
        # 任一模型调用失败自动换下一个，全挂才跳过本次（不阻断主流程）。
        candidates = []
        butler = self._ensure_client()
        if butler:
            candidates.append((butler, self._model, "管家模型"))
        main = self._ensure_main_client()
        if main and self._main_model and self._main_model != self._model:
            candidates.append((main, self._main_model, "主模型"))
        resp = None
        messages = [
            {"role": "system", "content": _REVIEW_OUTPUT_PROTOCOL},
            {"role": "user", "content": user_content},
        ]
        for client, model, label in candidates:
            try:
                try:
                    # 显式关闭思考：DeepSeek 等默认开启时 content 为空、内容全在
                    # reasoning_content，导致复盘 JSON 无法解析（对标 agent 同款降级）
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model,
                            messages=messages,
                            temperature=0.4,
                            max_tokens=2048,
                            extra_body={"thinking": {"type": "disabled"}},
                        ),
                        timeout=60.0,
                    )
                except Exception:
                    console.dim(f"[进化] {label}不支持 thinking 参数，降级为普通模式")
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model,
                            messages=messages,
                            temperature=0.4,
                            max_tokens=2048,
                        ),
                        timeout=60.0,
                    )
                break
            except Exception as e:
                console.warn(f"[进化] 复盘模型调用失败（{label}）：{e}")
        if resp is None:
            return
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        if not content:
            # 兜底：部分服务（如思考模式）正文在 reasoning_content
            content = (getattr(msg, "reasoning_content", None) or "").strip()
        data = _parse_review_json(content)
        if not data:
            console.dim("[进化] 复盘输出无法解析，跳过本次")
            return
        await self._apply_review(data, proactive)

    async def _apply_review(self, data: dict, proactive) -> None:
        """把复盘结果分六类落地。"""
        skill = data.get("skill")
        if isinstance(skill, dict):
            await self._save_skill(skill)
        patch = data.get("skill_patch")
        if isinstance(patch, dict):
            await self._apply_skill_patch(patch)
        topics = data.get("topics")
        if isinstance(topics, list) and topics:
            self._append_topics(topics, proactive)
        lesson = (data.get("lesson") or "").strip()
        if lesson:
            await self._save_lesson(lesson)
        advice = (data.get("advice") or "").strip()
        if advice:
            self._append_advice(advice)
        status = data.get("advice_status")
        if isinstance(status, list) and status:
            self._apply_advice_status(status)
        profile = data.get("profile")
        if isinstance(profile, list) and profile:
            self._append_profile(profile)
        if not (skill or patch or topics or lesson or advice or status or profile):
            console.dim("[进化] 本轮无沉淀内容")

    # ---------- 技能沉淀 ----------

    async def _save_skill(self, skill: dict) -> None:
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
            written = self._write_skill_resources(skill_dir, resources)
            if written:
                console.dim(f"[进化] 技能 {safe_name!r} 沉淀 {written} 个子文件")
        # 技能评估（闭环第一环）：新建技能无旧版本对照，评估分过低仅告警
        # 建议人工复核，不拦截落盘（评估失败静默跳过，不影响沉淀）
        try:
            score = await get_evaluator().score_skill(md)
            if score is not None and score < _EVAL_MIN_SCORE:
                console.warn(
                    f"[进化] 技能 {safe_name!r} 评估分 {score:.2f} 偏低，"
                    "建议人工复核后修正或移除")
        except Exception as e:
            console.dim(f"[进化] 技能 {safe_name!r} 评估跳过：{e}")
        console.ok(f"[进化] 技能沉淀：{safe_name}（已热加载）")

    @staticmethod
    def _write_skill_resources(skill_dir: Path, resources: list) -> int:
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

    # ---------- 技能自我改进 ----------

    async def _apply_skill_patch(self, patch: dict) -> None:
        """修补现有技能：保留 frontmatter，用 LLM 给出的完整正文整体替换。

        改写 SKILL.md 后由 watchdog 热加载，下一轮即可生效。
        """
        name = (patch.get("name") or "").strip()
        new_body = (patch.get("patch") or "").strip()
        if not name or not new_body:
            return
        from src.llm.tools.skills import get_skill_manager
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
        if old.startswith("---"):
            sep = old.find("\n---", 3)
            frontmatter = old[:sep + 4] if sep > 0 else old
            new = frontmatter.rstrip() + "\n\n" + new_body + "\n"
        else:
            new = new_body + "\n"
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
                    return
        except Exception as e:
            console.dim(f"[进化] 技能 {name!r} 修补评估跳过：{e}")
        try:
            skill.location.write_text(new, encoding="utf-8")
        except OSError as e:
            console.warn(f"[进化] 技能修补写入失败：{e}")
            return
        console.ok(f"[进化] 技能修补：{name}（已热加载）")

    # ---------- 技能库清理（Curator：合并 + 归档） ----------

    async def _maybe_prune_skills(self) -> None:
        """定期审阅技能库：把窄技能合并进 umbrella，把过时/低质/重复的技能移入 _archived/。

        对标 hermes curator 的 consolidation + pruning：
        - merge：2 个及以上同属一类工作的窄技能 → 正文并入 umbrella 技能后归档
        - archive：过时/失效/低质/重复技能 → 移入 _archived/（可恢复）
        被合并/归档技能目录移出扫描路径后，watchdog 重扫自动从注册表移除。
        技能太少或新技能未满 _PRUNE_MIN_AGE 时跳过，防止误清理。
        """
        from src.llm.tools.skills import get_skill_manager
        manager = get_skill_manager()
        now = time.time()
        candidates = [
            s for s in manager.skills
            if now - _file_mtime(s.location) >= _PRUNE_MIN_AGE
        ]
        if len(candidates) < 3:
            return  # 技能太少，不值得清理
        client = self._ensure_main_client()
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
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._main_model,
                    messages=[
                        {"role": "system", "content": _PRUNE_OUTPUT_PROTOCOL},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.2,
                    max_tokens=1024,
                ),
                timeout=60.0,
            )
        except Exception as e:
            console.warn(f"[进化] 技能库审阅失败：{e}")
            return
        content = (resp.choices[0].message.content or "").strip()
        data = _parse_review_json(content)
        merged = self._apply_skill_merges(data.get("merge"), manager)
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
            dest = skill.location.parent.parent / _ARCHIVE_DIR / name
            if dest.exists():
                console.warn(f"[进化] 归档目录已存在，跳过技能 {name!r}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(skill.location.parent), str(dest))
                archived += 1
            except OSError as e:
                console.warn(f"[进化] 归档技能 {name!r} 失败：{e}")
        if merged or archived:
            console.ok(f"[进化] 技能库维护：合并 {merged} 个窄技能，归档 {archived} 个技能")

    def _apply_skill_merges(self, merges, manager) -> int:
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
                # 只取正文（去 frontmatter），作为小节并入 umbrella
                if body.startswith("---"):
                    sep = body.find("\n---", 3)
                    if sep > 0:
                        body = body[sep + 4:].lstrip("\n")
                sections.append((name, body))
            if not sections:
                continue
            # 保留 frontmatter，正文末尾追加各小节（空行分隔）
            if umbrella_text.startswith("---"):
                sep = umbrella_text.find("\n---", 3)
                frontmatter = umbrella_text[:sep + 4] if sep > 0 else umbrella_text
                base = umbrella_text[sep + 4:].lstrip("\n") if sep > 0 else ""
            else:
                frontmatter = ""
                base = umbrella_text
            chunks = [base.rstrip()] if base.strip() else []
            chunks += [f"## {name}\n\n{body.rstrip()}" for name, body in sections]
            new_text = (frontmatter.rstrip() + "\n\n" + "\n\n".join(chunks) + "\n"
                        if frontmatter else "\n\n".join(chunks) + "\n")
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
                dest = skill.location.parent.parent / _ARCHIVE_DIR / name
                if dest.exists():
                    console.warn(f"[进化] 归档目录已存在，跳过技能 {name!r}")
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(skill.location.parent), str(dest))
                    merged += 1
                except OSError as e:
                    console.warn(f"[进化] 归档技能 {name!r} 失败：{e}")
            console.ok(f"[进化] 技能合并：{target_name} 吸收 "
                       f"{', '.join(n for n, _ in sections)}（已热加载）")
        return merged

    # ---------- 话题进化 ----------

    def _append_topics(self, new_topics: list[dict], proactive) -> None:
        """把新话题追加进 topics.yml，并注入运行中的 ProactiveEngine。"""
        try:
            with open(_TOPICS_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, ValueError) as e:
            console.warn(f"[进化] 读取话题文件失败：{e}")
            return
        existing = data.get("topics") or []
        seen = {t.get("concept") for t in existing}
        added: list[dict] = []
        for t in new_topics:
            concept = (t.get("concept") or "").strip()
            if not concept or concept in seen:
                continue
            tags = t.get("tags")
            if isinstance(tags, list):
                tags = [str(x) for x in tags][:4]
            else:
                tags = []
            existing.append({
                "id": _make_topic_id(concept),
                "category": _TOPIC_CATEGORY,
                "concept": concept,
                "tags": tags,
                "cooldown_minutes": _TOPIC_COOLDOWN_MINUTES,
            })
            added.append({
                "id": _make_topic_id(concept),
                "category": _TOPIC_CATEGORY,
                "concept": concept,
                "tags": tags,
                "cooldown_minutes": _TOPIC_COOLDOWN_MINUTES,
            })
            seen.add(concept)
        if not added:
            return
        try:
            with open(_TOPICS_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        except OSError as e:
            console.warn(f"[进化] 写入话题文件失败：{e}")
            return
        # 注入运行中的主动引擎：当前会话立即可用，重启后 topics.yml 兜底
        if proactive is not None and hasattr(proactive, "add_topic_seeds"):
            try:
                proactive.add_topic_seeds(added)
            except Exception:
                pass
        console.ok(f"[进化] 话题进化：新增 {len(added)} 个话题")

    # ---------- 行为反思（经验教训写记忆库） ----------

    async def _save_lesson(self, lesson: str) -> None:
        """把复盘出的经验教训写入记忆库（后续检索可带出）。"""
        try:
            from src.memory import memory
            await memory.get_manager().commit_recall_files([{
                "name": "进化经验",
                "description": "进化/经验教训",
                "content": lesson,
                "user": memory._USER_SELF,
            }])
        except Exception as e:
            console.warn(f"[进化] 经验教训写入失败：{e}")
            return
        console.ok("[进化] 行为反思：经验教训已入记忆库")

    # ---------- 话术闭环（建议 → 生效 → 回评） ----------

    def _append_advice(self, advice: str) -> None:
        """沉淀话术优化建议：追加 evolution_advice.md 存档 + 登记生效建议。

        生效建议由 llm_brain 注入系统提示；到期后由复盘回评续期或移除。
        """
        path = os.path.join(config.cfg.PROJECT_ROOT, "data",
                            "evolution_advice.md")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n## {stamp}\n{advice}\n")
        except OSError as e:
            console.warn(f"[进化] 写入话术建议失败：{e}")
            return
        _append_active_advice(advice)
        console.ok("[进化] 话术建议：已追加 evolution_advice.md 并登记生效")

    def _apply_advice_status(self, status_list: list) -> None:
        """根据复盘回评更新生效建议：keep 续期保留，否则从生效列表移除。

        未被回评到的建议保持原状（仍到期，下轮复盘继续评估）。
        """
        verdicts = {
            (it.get("text") or "").strip(): bool(it.get("keep"))
            for it in status_list if isinstance(it, dict)
        }
        if not verdicts:
            return
        items = _load_active_advice()
        if not items:
            return
        kept: list[dict] = []
        removed = 0
        now = time.time()
        for it in items:
            text = (it.get("text") or "").strip()
            if text in verdicts:
                if verdicts[text]:
                    it["expires"] = now + _ADVICE_TTL_SECONDS
                    kept.append(it)
                else:
                    removed += 1
            else:
                kept.append(it)
        _save_active_advice(kept)
        if removed:
            console.ok(f"[进化] 话术回评：保留 {len(kept)} 条，移除 {removed} 条失效建议")

    # ---------- 观众画像（LLM 摘要记忆层：长期事实落盘供关键词召回） ----------

    def _append_profile(self, profiles: list) -> None:
        """把复盘提炼的观众画像条目追加进画像文件（文本去重，超限丢弃最旧）。

        画像文件由 llm_brain 每轮按关键词召回注入系统提示（对标 hermes 的
        USER.md/MEMORY.md），与 memU 向量记忆互为补充。
        """
        from src.memory import memory
        items = _load_profile()
        existing = {it.get("fact", "").strip() for it in items}
        added = 0
        for p in profiles:
            if not isinstance(p, dict):
                continue
            fact = (p.get("fact") or "").strip()
            if not fact or fact in existing:
                continue
            owner = (p.get("owner") or "").strip()[:32] or memory._USER_DEFAULT
            items.append({"owner": owner, "fact": fact, "created": time.time()})
            existing.add(fact)
            added += 1
        if not added:
            return
        if len(items) > _PROFILE_MAX:
            items = items[-_PROFILE_MAX:]
        _save_profile(items)
        console.ok(f"[进化] 观众画像：新增 {added} 条长期事实（共 {len(items)} 条）")


def _load_profile() -> list[dict]:
    """读取观众画像条目列表（文件缺失/损坏时返回空列表）。"""
    try:
        with open(_PROFILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_profile(items: list[dict]) -> None:
    """覆写观众画像条目列表。"""
    try:
        os.makedirs(os.path.dirname(_PROFILE_PATH), exist_ok=True)
        with open(_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except OSError as e:
        console.warn(f"[进化] 写入观众画像失败：{e}")


__all__ = ["EvolutionEngine"]
