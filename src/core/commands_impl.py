"""控制台命令实现（§3.7 拆分）：原 Application._cmd_* 搬移为模块级函数。

所有命令 handler 签名统一为 `async def handler(runtime, cmd) -> bool`，
由 build_app_commands(runtime) 注册进 CommandRegistry。逻辑零改动，
仅把 self 引用改为 runtime 参数。
"""

import asyncio

from src.utils import console
from tools.memory import memory
from src.core.commands import Command, CommandRegistry


async def handle_memory_command(runtime, cmd: str) -> bool:
    """/memory 子命令：list 列出｜del <id>... 删除｜clear 清空｜decay 衰减。"""
    parts = cmd.split()
    sub = parts[1] if len(parts) > 1 else ""
    mm = runtime.mm
    if mm is None:
        console.dim("记忆系统不可用")
        return True
    if sub == "list":
        files = mm.list_files(limit=200)
        console.header("记忆列表")
        if not files:
            console.dim("暂无记忆（多和 E.V 聊聊天，会话结束会自动蒸馏）")
        for f in files:
            console.kv(str(f.get("id") or "-")[:16],
                       f"{f.get('name') or ''}｜{(f.get('content') or '')[:60]}")
        return True
    if sub == "del" and len(parts) >= 3:
        ids = [p for p in parts[2:] if p]
        deleted = await mm.delete_memories_async(ids)
        console.ok(f"已删除 {deleted} 条记忆")
        await runtime.speak_memory_reply(f"已经删除 {deleted} 条记忆")
        return True
    if sub == "clear":
        mm.clear_all()
        console.ok("已清空全部记忆")
        await runtime.speak_memory_reply("已经清空全部记忆")
        return True
    if sub == "decay":
        n = await asyncio.to_thread(memory.decay_stale_memories)
        console.ok(f"记忆衰减完成，清理 {n} 条")
        return True
    console.dim("用法：/memory list ｜ /memory del <id>... ｜ "
                "/memory clear ｜ /memory decay")
    return True


async def cmd_model(runtime, cmd: str) -> bool:
    """!model <path> 桌宠模式热切换模型。"""
    new_path = cmd[len("!model "):].strip()
    if runtime.validate_cmd_path(new_path) is None:
        console.error(f"非法模型路径（仅允许项目目录内）：{new_path}")
        return True
    if runtime.pet_widget is not None:
        if runtime.pet_widget.switch_model(new_path):
            console.ok(f"已热切换桌宠模型：{new_path}")
        else:
            console.error(f"模型切换失败：{new_path}（文件不存在）")
    else:
        console.dim("已收到模型切换指令（当前非桌宠模式，忽略）")
    return True


async def cmd_clean(runtime, cmd: str) -> bool:
    """!clean 资源清理（运行时内存 + 临时文件）。"""
    from src.utils import cleaner
    cleaner.cleanup_runtime_memory(verbose=True)
    cleaner.cleanup_temp_files(verbose=True)
    return True


async def cmd_plugins(runtime, cmd: str) -> bool:
    """!plugins 插件管理：list / sync / reload <name> / enable|disable <相对路径>。"""
    mgr = runtime.plugin_manager
    if mgr is None:
        console.dim("插件系统未启用")
        return True
    parts = cmd.split()
    sub = parts[1] if len(parts) > 1 else "list"
    if sub == "list":
        entries = mgr.get_plugin_list()
        if not entries:
            console.dim("当前没有已加载的插件")
        else:
            console.header("插件列表")
            for it in entries:
                console.kv(it["name"], f"{it['displayName']} v{it['version']}")
        return True
    if sub == "sync":
        await mgr.sync_enabled_plugins()
        console.ok("插件启用列表已同步（热加载/卸载完成）")
        return True
    if sub == "reload" and len(parts) >= 3:
        try:
            await mgr.reload(parts[2])
        except Exception as e:
            console.error(f"插件热重载失败（{parts[2]}）：{e}")
        return True
    if sub in ("enable", "disable") and len(parts) >= 3:
        try:
            result = await mgr.apply_enabled(parts[2], sub == "enable")
        except OSError as e:
            console.error(f"插件启停失败：{e}")
            return True
        console.ok(result)
        return True
    console.dim("用法：!plugins list ｜ !plugins sync ｜ !plugins reload <name> ｜ "
                "!plugins enable|disable <相对路径>")
    return True


async def cmd_tools(runtime, cmd: str) -> bool:
    """!tools 工具 / MCP 配置热更新。"""
    from src.utils import config as cfg_mod
    from plugins.tools import get_merged_tools
    cfg_mod.reload_tool_runtime()
    if cfg_mod.cfg.MCP_ENABLED and cfg_mod.cfg.TOOLS_ENABLED:
        if runtime.mcp is not None:
            await runtime.mcp.stop()
            runtime.mcp.is_enabled = True
            runtime.mcp.load_mcp_config()
            await runtime.mcp.start_all_servers()
        else:
            from src.mcp.manager import MCPManager
            runtime.mcp = MCPManager()
            await runtime.mcp.initialize()
            runtime.brain.mcp = runtime.mcp
    else:
        if runtime.mcp is not None:
            await runtime.mcp.stop()
            runtime.mcp = None
            runtime.brain.mcp = None
    merged = get_merged_tools(runtime.mcp)
    if merged:
        names = [t["function"]["name"] for t in merged]
        console.ok(
            f"工具配置已热更新（{len(names)} 个）：{'、'.join(names)}")
    else:
        console.warn("工具配置已热更新：当前无可用工具（纯对话模式）")
    return True


async def cmd_reload_config(runtime, cmd: str) -> bool:
    """!config 统一配置热更新；!config <组件> 细粒度热更新单个组件。

    细粒度组件：llm / proactive / pf / memory / bili / pet / emotion。
    细粒度失败不崩（保留原实例）；全量任一步失败按原逻辑向上冒泡。
    """
    parts = cmd.split()
    if len(parts) >= 2:
        return await runtime.reload_component(parts[1])
    # 无参：全量热更新（LLM / 主动对话 / 内容过滤 / 记忆 / 弹幕 / 桌宠 / 情绪）
    await runtime.reload_all()
    console.ok("配置已全部热更新（立即生效，无需重启）")
    return True


async def cmd_stt(runtime, cmd: str) -> bool:
    """!stt 语音识别热更新。"""
    from src.utils import config as cfg_mod
    cfg_mod.reload_tool_runtime()
    if cfg_mod.cfg.STT_ENABLED:
        if runtime.stt_engine is not None:
            runtime.stt_engine.stop()
            runtime.stt_engine = None
        try:
            from src.asr.stt import STTEngine
            runtime.stt_engine = STTEngine(cfg_mod.cfg)
            runtime.stt_engine.start()
            console.ok(
                "语音识别已开启：对着麦克风说话即可输入"
                f"（{cfg_mod.cfg.STT_MODEL}）")
        except Exception as e:
            console.warn(f"语音识别启动失败：{e}")
    elif runtime.stt_engine is not None:
        runtime.stt_engine.stop()
        runtime.stt_engine = None
        console.ok("语音识别已关闭")
    return True


async def cmd_tts_audio(runtime, cmd: str) -> bool:
    """!tts_audio <path> 主参考音频热更新。"""
    if runtime.tts is None:
        return True
    new_audio = cmd[len("!tts_audio "):].strip()
    # 空串 = 清空参考音频（合法）；非空须在项目目录内
    if new_audio and runtime.validate_cmd_path(new_audio) is None:
        console.error(f"非法音频路径（仅允许项目目录内）：{new_audio}")
        return True
    runtime.tts.apply_ref(new_audio, runtime.tts.ref_text)
    if new_audio:
        console.ok(f"已热更新 TTS 参考音频：{new_audio}")
    else:
        console.warn("TTS 参考音频已清空，语音合成已关闭")
    return True


async def cmd_tts_text(runtime, cmd: str) -> bool:
    """!tts_text <text> 主参考文本热更新。"""
    if runtime.tts is None:
        return True
    new_text = cmd[len("!tts_text "):].strip()
    # 只更新文本：沿用主参考原始串（ref_audio 可能是主+辅助的合成 dict）
    runtime.tts.apply_ref(runtime.tts._ref_main, new_text)
    console.ok(f"已热更新 TTS 参考音频文本：{new_text}")
    return True


async def cmd_tts_audios(runtime, cmd: str) -> bool:
    """!tts_audios <path> 辅助参考音频热更新。"""
    if runtime.tts is None:
        return True
    new_extras = cmd[len("!tts_audios "):].strip()
    # 空串 = 清空辅助参考（合法）；非空时逐条校验须在项目目录内
    if new_extras and any(
            runtime.validate_cmd_path(p) is None
            for p in new_extras.split("|")):
        console.error("非法辅助音频路径（仅允许项目目录内），已忽略本次更新")
        return True
    runtime.tts.apply_ref_extras(new_extras)
    console.ok(f"已热更新 TTS 辅助参考音频：{new_extras}")
    return True


async def cmd_agent(runtime, cmd: str) -> bool:
    """!agent <任务描述>：后台运行任务执行 Agent（多步读改验）。

    默认不启用（AGENT_ENABLED=false）；运行期间只输出到控制台，
    占用输出互斥锁（§3.6），不阻塞主输入循环。
    """
    if not runtime.cfg.AGENT_ENABLED:
        console.warn("[Agent] AGENT_ENABLED 未开启（.env 设置 AGENT_ENABLED=true）")
        return True
    task = cmd[len("!agent "):].strip()
    if not task:
        console.warn("[Agent] 用法：!agent <任务描述>，例如「!agent 读取 README.md 并总结要点」")
        return True

    def _progress(step: int, max_steps: int, action: dict, observation: str) -> None:
        console.dim(f"[Agent] 步骤 {step}/{max_steps}：{action['name']}({action.get('arguments', {})})")

    async def _run() -> None:
        from src.agent import run_task

        try:
            console.dim(f"[Agent] 开始任务：{task}")
            result = await run_task(task, progress_cb=_progress)
            console.ok(f"[Agent] 任务完成：{result}")
        except Exception as e:
            console.error(f"[Agent] 任务失败：{type(e).__name__}: {e}")

    asyncio.create_task(_run())
    return True


async def cmd_diary(runtime, cmd: str) -> bool:
    """!diary 写一篇今天的日记（LLM 生成，落盘 data/diary/YYYY-MM-DD.md）。

    想什么时候写就什么时候写；当天已有日记时会自动合并重写（不丢内容）。
    后台生成，不阻塞输入循环。
    """
    from src.llm.diary import DiaryWriter

    turns = runtime.mm.recent_turns if runtime.mm is not None else []

    async def _run() -> None:
        try:
            path = await DiaryWriter().write_diary(turns)
            if path:
                console.ok(f"[日记] 已写入 {path}")
            else:
                console.warn("[日记] 生成失败（见上方日志）")
        except Exception as e:
            console.warn(f"[日记] 生成失败：{e}")

    asyncio.create_task(_run())
    return True


def build_app_commands(runtime) -> CommandRegistry:
    """构建本应用的命令注册表（原 Application._build_command_registry，逻辑零改动）。"""
    registry = CommandRegistry()
    registry.register(
        Command("/memory", lambda c: handle_memory_command(runtime, c),
                help="记忆管理：list/del/clear/decay"),
        Command("!diary", lambda c: cmd_diary(runtime, c), exact=True,
                help="写今天的日记（LLM 生成，落盘 data/diary/）"),
        Command("!model ", lambda c: cmd_model(runtime, c),
                help="桌宠模式热切换模型"),
        Command("!clean", lambda c: cmd_clean(runtime, c), exact=True,
                help="清理运行时内存和临时文件"),
        Command("!plugins", lambda c: cmd_plugins(runtime, c),
                help="插件管理：list/sync/reload/enable/disable"),
        Command("!tools", lambda c: cmd_tools(runtime, c), exact=True,
                help="工具 / MCP 配置热更新"),
        Command("!config", lambda c: cmd_reload_config(runtime, c),
                help="配置热更新：!config 全量 ｜ !config llm/proactive/pf/memory/bili/pet/emotion 细粒度"),
        Command("!stt", lambda c: cmd_stt(runtime, c), exact=True,
                help="语音识别热启停"),
        Command("!tts_audio ", lambda c: cmd_tts_audio(runtime, c),
                help="TTS 主参考音频热更新"),
        Command("!tts_text ", lambda c: cmd_tts_text(runtime, c),
                help="TTS 主参考文本热更新"),
        Command("!tts_audios ", lambda c: cmd_tts_audios(runtime, c),
                help="TTS 辅助参考音频热更新"),
        Command("!agent ", lambda c: cmd_agent(runtime, c),
                help="任务执行 Agent：多步读文件→改→验证"),
    )
    return registry
