"""ButlerAgent 共享 prompt 模板 + 正则/归属规则常量。

所有子模块（core/store/summarize）从这里导入共享字符串/规则，避免重复定义。
"""

from __future__ import annotations

import os
import re

from ev.utils.constants import ROLE_ASSISTANT, ROLE_AI_ALIAS

# AI 名字（.env AI_NAME）：配置后提到 AI 自己一律用名字（如 neuro），
# 不再出现「AI 喜欢…」这类无名字表述（图谱/列表也按此名显示）
_AI_NAME = (os.getenv("AI_NAME") or "").strip()
_AI_REF = f"「{_AI_NAME}」" if _AI_NAME else "「AI」"   # 行内引用写法
_AI_SUBJECT = _AI_NAME if _AI_NAME else "AI"           # 归属者字段写法

# 共享子段：说话人判定 + 归属/合并规则（提取/蒸馏/整合统一语义）。
# AI 归属名动态化：配置 AI_NAME 后 subject/name 用名字（如 neuro），
# owner 保持 self（内部分组标识，图谱按它聚到 AI 名下）
_SUBJECT_OWNER_RULES = f"""【说话人判定】（决定 subject 与 owner）
- 「观众: [弹幕@xxx] ...」→ subject 填具体观众名（从 [弹幕@ 中提取，如「蓝奶」），owner=user
- 「主播: ...」→ subject=「主播」，owner=user
- 「{_AI_REF}: ...」→ subject={_AI_REF}，owner=self

【归属与合并】
- subject = 说话人/归属者，决定记忆记在谁名下
- owner = 事实大分类：self=AI 自身事实，user=用户侧事实（观众或主播）
- 不同归属者（不同观众/主播/{_AI_SUBJECT}）的条目不要合并，各自保留 subject
- 观众发言必须给出具体观众名，禁止用「观众/他/她」等笼统词代替
- 同一归属者的多条信息合并为一条完整记忆；剔除与其它条目高度重复者"""

_AI_NAMING_RULE = (
    f"\n【AI 自称】提到 AI 自己（自我认知/偏好/承诺）时一律用名字「{_AI_NAME}」"
    f"表述（如「{_AI_NAME} 喜欢收集蓝箱子」）。name/content/subject 三处"
    f"都禁止出现「AI」字样，全部用「{_AI_NAME}」"
    if _AI_NAME
    else ""
)

# 共享子段：五元组结构化输出 schema（用于 EXTRACT，8 字段）
_TRIPLE_OUTPUT_SCHEMA = f"""【输出格式】JSON 数组，每项字段：
- name：简短主题，≤20字（提到 AI 自己时含名字{_AI_REF}，禁止用「AI」）
- content：完整记忆，一句话
- subject：说话人/归属者（具体观众名/主播/{_AI_SUBJECT}），观众发言必填观众名，无则空
- subject_type：person / location / organization / item / concept / time / event / activity，无则空
- predicate：关系或行为描述，无则空
- object：客体名称，无则空
- object_type：同 subject_type，无则空
- owner：self=AI 自身事实，user=用户侧事实（由说话人判断）"""

# 共享子段：基础输出 schema（用于 DISTILL/INTEGRATE，4 字段）
_BASIC_OUTPUT_SCHEMA = f"""【输出格式】JSON 数组，每项字段：
- name：简短主题，≤20字（提到 AI 自己时含名字{_AI_REF}，禁止用「AI」）
- content：完整记忆，一句话
- subject：说话人/归属者（具体观众名/主播/{_AI_SUBJECT}），观众发言必填观众名，无则空
- owner：self=AI 自身事实，user=用户侧事实（由说话人判断）"""

# 提取的系统提示（要求纯 JSON 输出 + 五元组结构化，便于图谱按实体着色）。
# 同时提取双方事实：对方（用户）的信息 + AI 自己表达的观点/承诺/自我认知，
# 保证「AI 的回复」也能进长期记忆（否则只记得用户说了什么）。
# 去重要求收敛同主体碎片：同一主体/话题的多条说法合并为一条完整记忆，
# 避免把一句话的不同转述拆成多条近似重复条目（用户反馈的历史问题）。
_EXTRACT_SYSTEM = f"""你是长期记忆管家。从对话轮次中提取值得长期记住的实体事实。

【提取范围】
- 用户侧：人物、喜好、约定、情绪状态、关系细节
- AI 侧：自我认知、承诺、偏好、个人经历

【过滤原则】
- 保留：稳定偏好、重要关系、约定、身份、经历、持久状态
- 过滤：纯情绪宣泄、临时修辞、假设性内容、隐喻性表达

{_SUBJECT_OWNER_RULES}

{_AI_NAMING_RULE}

{_TRIPLE_OUTPUT_SCHEMA}

无值得记录信息时输出 []。只输出 JSON 数组，不要任何额外文字。"""

_DISTILL_SYSTEM = f"""你是长期记忆管家。从会话中蒸馏值得永久保留的条目。

【保留标准】长期身份信息 / 稳定偏好 / 重要关系 / 约定与承诺

{_SUBJECT_OWNER_RULES}

{_AI_NAMING_RULE}

{_BASIC_OUTPUT_SCHEMA}

无内容时输出 []。只输出 JSON 数组，不要任何额外文字。"""

# 整库整合蒸馏的系统提示（AI 自动蒸馏记忆库碎片用）：一次性处理多批旧碎片，
# 合并同主题、剔除近似重复。整合质量要求高（成功后会删除原碎片），因此走
# 主模型（LLM_* 配置）而非 BUTLER 管家模型——Qwen2.5-7B 曾出现 JSON 缺逗号/
# 幻觉导致整合失败的历史问题。要求单行紧凑 JSON（防 token 截断）。
_INTEGRATE_SYSTEM = f"""你是长期记忆整理管家。处理记忆库中的碎片条目（按行编号），存在重复/近似/分散问题。

每条以「序号. [归属者] 内容」给出，[] 内是归属者（具体观众名/主播/{_AI_SUBJECT}/self）。

【整合任务】
1. 同一主体/话题的多条合并为一条完整记忆，保留全部关键细节
2. 剔除与其他条目高度重复的条目
3. 保留长期价值：身份、偏好、关系、约定、经历、稳定状态
4. 过滤：纯情绪、临时修辞

{_SUBJECT_OWNER_RULES}

{_AI_NAMING_RULE}

{_BASIC_OUTPUT_SCHEMA}

【格式约束】单行紧凑 JSON 数组，无换行无缩进，元素间用逗号分隔

【示例】
输入：
1. [蓝奶] 观众蓝奶喜欢喝牛奶
2. [chao] 主播喜欢收集蓝箱子
输出：
[{{"name":"蓝奶喜好","content":"观众蓝奶喜欢喝牛奶。","subject":"蓝奶","owner":"user"}},{{"name":"主播喜好","content":"主播喜欢收集蓝箱子。","subject":"主播","owner":"user"}}]

只输出 JSON 数组，不要任何额外文字。"""

_SUMMARIZE_SYSTEM = """你是会话记录员。把对话压缩为 6 段中文摘要（无内容直接写「无」）。

【关键事实】对话中确认的事实性信息
【用户偏好】对方喜好与厌恶
【重要决定】双方约定或决定
【待办事项】未完成或计划之事
【背景信息】话题背景与上下文
【最近状态】双方当前情绪与状态

纯文本输出，无任何格式修饰（无 Markdown、无加粗）。"""

# 主动发言 prompt 模板（{period} 时段、{topic_hint} 话题、{memory_hint} 记忆线索）
# 模型输出 <SILENT> 表示此刻不想说，否则输出即主动发言
_PROACTIVE_PROMPT_TEMPLATE = (
    "【安静时刻 · 自主开口机会 · {period}】直播间暂时没有人说话，"
    "现在是你的自由时间。由你决定此刻想不想开口：\n"
    "1. 保持沉默（只输出 <SILENT>，不带任何其他文字）\n"
    "2. 说点此刻心里想说的话（感受、感慨、观察、吐槽，"
    "像自言自语一样简短真诚，不必等待回应）\n"
    "3. 主动开启一个话题（{topic_hint}）\n\n"
    "要求：想说就只输出最终要说的话（1~3 句，符合人设，简短自然），"
    "不要输出编号、标记或解释；不想说就只输出 <SILENT>。{memory_hint}"
)

# 记忆提取后台 worker 数量（密集对话时并行消费，避免提取堆积）
_EXTRACT_WORKERS = 3

# AI 发言的 role 取值：内部轮次用 ROLE_ASSISTANT/ROLE_AI_ALIAS（如 "Neuro"/"Neuro sama"），
# 而提取/蒸馏外部构造的轮次用字面量 "assistant"，统一转小写后对比，避免误把 AI 回复
# 当作观众/主播轮次，导致记忆归属全部塌缩成默认用户。
_AI_ROLES = {ROLE_ASSISTANT.lower(), ROLE_AI_ALIAS.lower(), "assistant", "ai"}

# ---------- 实时强信号捕获（正则先行） ----------
#
# 弱宾语：代词/泛指词（喜欢这个/那个人）不算稳定事实，命中即跳过；
# 这类表达由 LLM 批量提取自行判断是否值得记录。
_WEAK_OBJS = {
    "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里",
    "这个人", "那个人", "你", "你们", "他", "她", "它", "他们", "她们",
    "大家", "自己", "上", "一下", "人", "事", "东西", "玩",
}

# 稳定事实捕获规则（确定性，不依赖 LLM）。每组：
# (正则, 主题名, content 模板, 三元组模板)；正则 obj 组 = 事实主体，
# rel 组（仅关系规则） = 亲属/称谓。正则以「我/我的」口吻出现在弹幕或
# 主播发言中；「我喜欢」前置 (?<!不) 负向断言防止「我不喜欢」误捕获为喜好。
_INSTANT_PATTERNS: tuple = (
    (
        re.compile(
            r"(?<!不)(?:我最喜欢|我超喜欢|我好喜欢|我很喜欢|我特别喜欢|"
            r"我喜欢|我超爱|我爱吃|我爱喝|最爱|我爱|超爱)"
            r"\s*(?P<obj>[^，。！？!?；;、\s]{1,24})"
        ),
        "喜好", "{subject}喜欢{obj}", "{subject} 喜欢 {obj}",
    ),
    (
        re.compile(
            r"(?:我不太喜欢|我不喜欢|我讨厌|我不爱|我不吃)"
            r"\s*(?P<obj>[^，。！？!?；;、\s]{1,24})"
        ),
        "厌恶", "{subject}不喜欢{obj}", "{subject} 不喜欢 {obj}",
    ),
    (
        re.compile(
            r"(?P<obj>[^，。！？!?；;、\s]{1,16})\s*是我"
            r"(?P<rel>姐姐|妹妹|哥哥|弟弟|老婆|老公|女朋友|男朋友|对象|"
            r"室友|闺蜜|兄弟|师傅|师父|同学|朋友)"
        ),
        "关系", "{subject}的{rel}是{obj}", "{obj} 是 {subject} 的 {rel}",
    ),
    (
        re.compile(r"我今年?\s*(?P<obj>\d{1,3})\s*岁"),
        "年龄", "{subject}今年{obj}岁", "{subject} 年龄 {obj}岁",
    ),
    (
        re.compile(r"我(?:住在|住)\s*(?P<obj>[^，。！？!?；;、\s]{1,12})"),
        "所在地", "{subject}住在{obj}", "{subject} 住在 {obj}",
    ),
    (
        re.compile(
            r"我在(?P<obj>[^，。！？!?；;、\s]{1,12})(?:工作|上班|上学|读书|生活)"
        ),
        "所在地", "{subject}在{obj}工作/上学", "{subject} 在 {obj} 工作/上学",
    ),
    (
        re.compile(r"我养了(?:一只|一条|只|条)?\s*(?P<obj>[^，。！？!?；;、\s]{1,12})"),
        "宠物", "{subject}养了{obj}", "{subject} 养了 {obj}",
    ),
    (
        re.compile(r"我(?:的)?生日(?:是|在)?\s*(?P<obj>[^，。！？!?；;、\s]{1,16})"),
        "生日", "{subject}的生日是{obj}", "{subject} 生日 {obj}",
    ),
    (
        re.compile(r"(?:我叫|我的名字叫|本名|名字叫)\s*(?P<obj>[^，。！？!?；;、\s]{1,12})"),
        "名字", "{subject}的名字是{obj}", "{subject} 名字 {obj}",
    ),
)

# 蒸馏/整合条目的归属者判定：owner=self → AI 自己；否则优先模型标注的
# subject（蒸馏 prompt 要求按「观众: [弹幕@名]」给出具体观众名/主播/AI），
# 排除笼统词（观众/主播/用户/AI 等）避免角色塌缩，缺失才回退推断归属者。
_ABSTRACT_SUBJECTS = {"观众", "主播", "用户", "AI", "self", "我", "他", "她"}
# subject 常见前缀清洗：模型有时把「观众@名」「弹幕@名」整段当作 subject，
# 剥掉角色前缀只留纯用户名（如「观众@蓝奶」→「蓝奶」），保证图谱归属者干净。
_SUBJECT_PREFIX_RE = re.compile(r"^(观众|弹幕|主播)@")


def _period_phrase(hour: int) -> str:
    """把小时映射为时段描述（用于主动发言的语气调节）。"""
    if 5 <= hour < 9:
        return "清晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "午后"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"
