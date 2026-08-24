"""桌宠 widget 子包：对外导出 PetWidget 与 BubbleSub 两个类。

旧路径 src/pet/widget.py 通过 `from ev.pet.widget import PetWidget, BubbleSub`
做 forward，保证调用方 import 零改动。
"""

from .core import PetWidget  # noqa: F401
from .bubble import BubbleSub  # noqa: F401

__all__ = ["PetWidget", "BubbleSub"]
