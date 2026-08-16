"""pytest 公共配置：保证 `src` 与 `tools` 可导入，无需安装包。

E.V 未采用 pip 安装式布局（src 是源码目录，tools 是脚本目录），
因此 conftest 在收集测试前把项目根加入 sys.path，统一复用。
"""

import os
import sys
from pathlib import Path

# 项目根 = tests/ 的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (_PROJECT_ROOT, _PROJECT_ROOT / "src", _PROJECT_ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 固定测试环境变量，避免依赖机器上的 .env（config 读取时会优先真实 .env，
# 这里不覆盖；仅确保关键开关关闭，测试不触网、不启服务）
os.environ.setdefault("MEMORY_ENABLED", "false")
