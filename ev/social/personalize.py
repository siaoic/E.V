"""ev.social.personalize — 读 SKILL.md 拟人化参数

每个 SKILL.md 末尾追加的「## 拟人化参数」一节被本模块解析成 PersonaParams,
供 5 个模块共用。

如果 SKILL.md 没写,使用默认值(中性人设)。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ev.social.personalize")


@dataclass
class PersonaParams:
    """拟人化参数。从 SKILL.md 末尾的「## 拟人化参数」节解析。"""
    social_level: float = 0.5           # 0=社恐, 1=社牛
    reply_threshold_base: float = 0.5   # 回复阈值基准
    silence_rate_target: float = 0.20   # 目标 [SILENT] 率
    proactive_chattiness: float = 0.5   # 主动说话频率
    interest_keywords: list = field(default_factory=list)
    ignore_keywords: list = field(default_factory=list)
    conversation_close_style: str = "话题性收尾"
    name_aliases: list = field(default_factory=list)
    
    # 内部:是否已加载
    _loaded_from: str = ""


_PARAM_RE = re.compile(
    r'##\s*拟人化参数.*?\n(.*?)(?=\n##\s|\Z)',
    re.DOTALL | re.IGNORECASE
)


def _parse_kv_block(text: str) -> dict:
    """解析 `key: value` 块,支持列表(`[a, b, c]`)和字符串。"""
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        key, _, val = line.partition(':')
        key = key.strip()
        val = val.strip()
        
        # 列表
        if val.startswith('[') and val.endswith(']'):
            items = []
            for item in val[1:-1].split(','):
                item = item.strip().strip('"').strip("'")
                if item:
                    items.append(item)
            result[key] = items
        # 数字
        elif val.replace('.', '').replace('-', '').isdigit():
            if '.' in val:
                result[key] = float(val)
            else:
                result[key] = int(val)
        # 字符串
        else:
            result[key] = val
    return result


def load_persona_params(skill_path: Optional[str] = None) -> PersonaParams:
    """从 SKILL.md 加载拟人化参数。失败时返回默认 PersonaParams。"""
    p = PersonaParams()
    
    # 解析路径
    if not skill_path:
        try:
            from ev.utils import config as ev_config
            skill_path = getattr(ev_config.cfg, "SYSTEM_PROMPT_FILE", "") or ""
        except Exception:
            skill_path = ""
    
    if not skill_path:
        for candidate in [
            Path("configs/personas/default/SKILL.md"),
            Path("configs/personas/SKILL.md"),
        ]:
            if candidate.exists():
                skill_path = str(candidate)
                break
    
    if not skill_path or not Path(skill_path).exists():
        logger.info("[personalize] no SKILL.md found, using default PersonaParams")
        return p
    
    try:
        text = Path(skill_path).read_text(encoding="utf-8")
        m = _PARAM_RE.search(text)
        if not m:
            logger.info(f"[personalize] no 拟人化参数 section in {skill_path}, using defaults")
            return p
        
        kv = _parse_kv_block(m.group(1))
        for k, v in kv.items():
            if hasattr(p, k):
                setattr(p, k, v)
        
        p._loaded_from = skill_path
        logger.info(
            f"[personalize] loaded from {skill_path}: "
            f"social_level={p.social_level}, "
            f"silence_rate_target={p.silence_rate_target}, "
            f"interests={p.interest_keywords[:3]}..."
        )
    except Exception as e:
        logger.warning(f"[personalize] load failed: {e}")
    
    return p


# ===== 单例 =====
_cached_params: Optional[PersonaParams] = None


def get_persona_params() -> PersonaParams:
    """获取已加载的 PersonaParams(单例)。"""
    global _cached_params
    if _cached_params is None:
        _cached_params = load_persona_params()
    return _cached_params


def reload_persona_params(skill_path: Optional[str] = None) -> PersonaParams:
    """强制重载(供 self-evolution 调参后使用)。"""
    global _cached_params
    _cached_params = load_persona_params(skill_path=skill_path)
    return _cached_params
