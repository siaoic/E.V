"""自我进化引擎包（原 src/llm/evolution.py 拆分）。

对外保持 `from src.llm.evolution import EvolutionEngine` 兼容；
模块内部按职责拆分：engine（调度）/ skills / topics / advice / profile
/ prompts / _utils / metrics / prompt_evo（GEPA 策略段）/ skill_eval（技能评估）。
"""

from .engine import EvolutionEngine

__all__ = ["EvolutionEngine"]
