"""Config 根类 + 动态 flat property（拆自 src/utils/config.py §3.8）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .dataclasses import (
    LLMConfig, ButlerConfig, MemoryConfig, KnowledgeConfig, AgentConfig,
    PersonaConfig, FilterConfig, ToolConfig, EvolutionConfig, VoiceConfig,
    VTSConfig, PetConfig, EmotionConfig, ProactiveConfig, DanmakuConfig,
    MindcraftConfig, PathsConfig,
)
from .loaders import _LOADERS, _FIELD_GROUP
from .env_loader import _PROJECT_ROOT, _SKIP_YAML_FIELDS


@dataclass
class Config:
    """根配置：聚合所有分组子配置，旧字段名经动态 @property 转发保持兼容。

    字段加载发生在 __post_init__：从 _LOADERS 逐个读取 .env 填充子配置，
    再应用 config.yaml 覆盖层（环境变量优先）。reload 同样走 _load_selected，
    保证新增字段自动纳入热更新，无需再改 reload 函数。
    """
    llm: LLMConfig = field(default_factory=LLMConfig)
    butler: ButlerConfig = field(default_factory=ButlerConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    tool: ToolConfig = field(default_factory=ToolConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    vts: VTSConfig = field(default_factory=VTSConfig)
    pet: PetConfig = field(default_factory=PetConfig)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    danmaku: DanmakuConfig = field(default_factory=DanmakuConfig)
    mindcraft: MindcraftConfig = field(default_factory=MindcraftConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)

    # ===== 字段加载 =====
    def _load_selected(self, fields: tuple) -> None:
        """按字段清单重新加载（loader 逐条读 .env 写入所在子配置）。"""
        for attr in fields:
            group = _FIELD_GROUP[attr]
            setattr(getattr(self, group), attr, _LOADERS[group][attr]())

    def _load_all(self) -> None:
        self._load_selected(tuple(_FIELD_GROUP))

    def __post_init__(self) -> None:
        """初始化完成后：从 .env 加载全部字段 + 应用 config.yaml 覆盖层。"""
        self._load_all()
        self._apply_yaml_overrides()
        _attach_flat_properties(self.__class__)

    # ===== yaml 覆盖层（环境变量 > config.yaml > 默认值） =====
    def _apply_yaml_overrides(self) -> None:
        """从 config.yaml 读取配置，覆盖「环境变量未设置」的字段。

        优先级：环境变量 > config.yaml > 默认值（与 load_dotenv 语义一致）。
        纯增量能力：文件不存在 / 解析失败时静默跳过，行为与原来完全一致。
        """
        yaml_path = self.paths.CONFIG_YAML_PATH
        if not os.path.isfile(yaml_path):
            return
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            try:
                from ev.utils import console
                console.warn(f"[config] 读取 yaml 配置失败，忽略：{e}")
            except Exception:
                pass
            return
        for key, value in data.items():
            if value is None or key in _SKIP_YAML_FIELDS:
                continue
            if key not in _FIELD_GROUP:
                continue
            # 环境变量优先：已设置就不再被 yaml 覆盖
            if os.getenv(key) is not None:
                continue
            group_obj = getattr(self, _FIELD_GROUP[key])
            current = getattr(group_obj, key)
            if isinstance(current, bool):
                coerced = value if isinstance(value, bool) else (
                    str(value).strip().lower()
                    in ("1", "true", "yes", "y", "on"))
            elif isinstance(current, int):
                try:
                    coerced = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(current, float):
                try:
                    coerced = float(value)
                except (TypeError, ValueError):
                    continue
            else:
                coerced = value
            setattr(group_obj, key, coerced)

    def validate(self) -> None:
        """检查必填项；缺失时给出明确提示。"""
        if not self.LLM_API_KEY or self.LLM_API_KEY == "YOUR_API_KEY":
            raise RuntimeError(
                "未配置 LLM_API_KEY，请在 .env 中填入你的 LLM API Key\n"
                "  （旧配置 ZHIPU_API_KEY 仍兼容）。可在此获取：\n"
                "  智谱 https://open.bigmodel.cn/ | "
                "DeepSeek https://platform.deepseek.com/"
            )


def _attach_flat_properties(cls):
    """动态为 Config 挂 150+ flat @property（cfg.LLM_API_KEY → cfg.llm.LLM_API_KEY）。

    - 用标志位判重避免重复挂（__post_init__ 多次调用的场景）。
    - 所有属性都带 getter + 通用 setter（保证 MOUTH_PARAMETER / MOUTH_GAIN
      等原带 setter 的字段仍可写入；其他字段写操作等价于 self.group.attr = value）。
    """
    if hasattr(cls, "_flat_properties_attached"):
        return
    for group_name, loader_dict in _LOADERS.items():
        for attr in loader_dict:
            if attr in cls.__dict__:
                continue
            def _getter(self, _gn=group_name, _an=attr):
                return getattr(getattr(self, _gn), _an)
            def _setter(self, value, _gn=group_name, _an=attr):
                setattr(getattr(self, _gn), _an, value)
            setattr(cls, attr, property(_getter, _setter))
    cls._flat_properties_attached = True
