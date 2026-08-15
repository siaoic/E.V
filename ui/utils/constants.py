"""UI 常量：情绪、拖拽 MIME、插件配置字段、记忆层配色、路径等。

从 control_center.py 顶层抽出，供 widgets / dialogs / handlers / pages 共用。
"""

import os
import sys
from typing import Dict, List, Tuple

# 情绪与动作页使用的 6 个基础情绪（固定写死，不再依赖 emotion_actor.EMOTIONS）
EMOTIONS: tuple = ("开心", "生气", "疑惑", "悲伤", "害怕", "厌恶")

# ui/ 目录：源码运行 = 包内目录；frozen = _MEIPASS/ui/（spec datas 打入）
_UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UI_FILE = (
    os.path.join(sys._MEIPASS, "ui", "control_center.ui")
    if getattr(sys, "frozen", False)
    else os.path.join(_UI_DIR, "control_center.ui")
)

# 窗口图标（任务栏）：exe 资源图标不会自动成为窗口图标，须显式 setWindowIcon。
# favicon.ico 通过 spec datas 打进 _MEIPASS/ui/，源码运行直接读 ui/ 下。
ICON_FILE = (
    os.path.join(sys._MEIPASS, "ui", "favicon.ico")
    if getattr(sys, "frozen", False)
    else os.path.join(_UI_DIR, "favicon.ico")
)

# 全局 tooltip 样式：暖米白圆角卡片（对齐控制中心整体风格），作用于
# 全应用所有 QToolTip——记忆表格/图谱节点/工具表等悬浮提示统一美化。
_TOOLTIP_QSS = (
    "QToolTip {"
    " background-color: rgb(252, 250, 245);"
    " color: rgb(70, 65, 55);"
    " border: 1px solid rgba(160, 155, 145, 200);"
    " border-radius: 8px;"
    " padding: 8px 10px;"
    " font-size: 12px;"
    " font-family: \"微软雅黑\";"
    "}"
)

# 拖拽数据类型：表情与动作分开，拖到情绪卡片时按类型绑到对应槽位
_DRAG_MIME_EXPR = "application/x-vtuber-expr"
_DRAG_MIME_MOTION = "application/x-vtuber-motion"

# 各插件配置页的 .env 字段（显示名）：无字段的插件不显示「配置」按钮。
# MCP 服务器不在此列——配置页直接编辑 mcp_config.json 中对应服务器的 JSON 段。
PLUGIN_CONFIG_FIELDS: Dict[str, List[Tuple[str, str]]] = {
    "TOOL_GET_WEATHER_ENABLED": [
        ("OPENWEATHERMAP_API_KEY", "OpenWeatherMap API Key")],
    "TOOL_LOAD_SKILL_ENABLED": [("SKILLS_DIR", "技能根目录（逗号分隔多个）")],
    # 外部服务：mindcraft（进程托管，复用本项目 LLM）
    "service:mindcraft": [
        ("MINDCRAFT_PATH", "Mindcraft 项目路径"),
        ("MINDCRAFT_LLM_BASE_URL", "LLM 服务地址（复用本项目）"),
        ("MINDCRAFT_LLM_MODEL", "LLM 模型"),
        ("MINDCRAFT_BOT_NAME", "Bot 名（与 MC 内角色名一致）"),
        ("MINDCRAFT_HOST", "Minecraft 服务器地址"),
        ("MINDCRAFT_PORT", "Minecraft 端口"),
        ("MINDCRAFT_AUTH", "登录方式（offline / microsoft）"),
        ("MINDCRAFT_MINDSERVER_PORT", "MindServer 端口（socket.io）"),
        ("MINDCRAFT_BRIDGE_ENABLED", "双向桥开关（true=主播朗读 bot 回复）"),
        ("MINDCRAFT_BOT_PERSONA", "Bot 人设（conversing 提示词）"),
    ],
}

# mindcraft bot 默认人设（andy.json 的 conversing 提示词，.env 可覆盖）。
# 保持简短：引擎会自动拼接 $MEMORY/$STATS/$INVENTORY/$COMMAND_DOCS 等占位。
_MINDCRAFT_DEFAULT_PERSONA = (
    "你是主播的 Minecraft 搭档机器人，正在游戏世界里陪伴玩家。"
    "用中文交流，语气活泼俏皮，回复简短自然，不要罗列清单或反复道歉。"
    "收到请求时立即使用命令行动（如 !goToPlayer、!build），不要假装已经执行。"
)

# 记忆类型 → 名称/主色（与 tools/memory/memory_graph.py
# _LAYER_STYLE / _TRACK_COLORS 配色一致；详情弹窗与列表行共用）
_MEMORY_LAYER_COLORS = {
    "core": (140, 100, 200),        # 紫：核心身份
    "state": (70, 140, 210),        # 蓝：关系状态
    "preference": (230, 130, 60),   # 橙：长期偏好
    "archive": (90, 160, 100),      # 绿：历史摘要
}
_MEMORY_LAYER_NAMES = {
    "core": "核心身份", "state": "关系状态",
    "preference": "长期偏好", "archive": "历史摘要",
}
_MEMORY_TRACK_COLORS = {
    "skill": (230, 130, 60),        # 橙：技能
    "memory": (70, 140, 210),       # 蓝：通用记忆
}
_MEMORY_TRACK_NAMES = {"skill": "技能", "memory": "记忆"}
