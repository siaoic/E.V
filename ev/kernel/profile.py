"""Profile 加载：多层 extends 深合并 + ${ENV_VAR} 展开 + slot 绑定校验。

A extends builtin:B  表示 A 继承自 builtins_root/B/profile.yaml
合并规则：dict 递归并子覆盖父，list 按首次出现去重拼接，标量子覆盖父。
Profile(dict) 支持直传，不需要 yaml 文件。
"""
from __future__ import annotations
import copy
import os
import re
from pathlib import Path
from typing import Any, Optional
import yaml


class Profile:
    _DEFAULT_PLUGINS: dict[str, list] = {"builtin": [], "pypi": [], "git": []}

    def __init__(self, profile_path_or_dict: str | dict[str, Any]) -> None:
        if isinstance(profile_path_or_dict, dict):
            self._raw = copy.deepcopy(profile_path_or_dict)
            self._path: Optional[str] = None
        else:
            p = Path(profile_path_or_dict)
            if not p.exists():
                raise FileNotFoundError(f"Profile not found: {p}")
            with open(p, "r", encoding="utf-8") as f:
                self._raw = yaml.safe_load(f) or {}
            self._path = str(p.resolve())
        self._resolved: Optional[dict] = None

    def resolve(self, builtins_root: str = "") -> dict:
        stack: list[str] = []
        merged = self._resolve_one(self._raw, self._path, builtins_root, stack)
        merged.setdefault("plugins", copy.deepcopy(self._DEFAULT_PLUGINS))
        for k, v in self._DEFAULT_PLUGINS.items():
            merged["plugins"].setdefault(k, copy.copy(v))
        merged.setdefault("slots", {})
        merged.setdefault("plugin_config", {})
        merged = self._expand_env(merged)
        self._resolved = merged
        return merged

    def _resolve_one(self, data: dict, cur_path, builtins_root: str, stack: list[str]) -> dict:
        name = data.get("name") or (Path(cur_path).parent.name if cur_path else "anon")
        if name in stack:
            raise RuntimeError(f"循环依赖 Profile extends: {' -> '.join(stack + [name])}")
        stack.append(name)
        extends = data.get("extends")
        base: dict = {}
        if extends:
            if not isinstance(extends, str):
                raise ValueError(f"extends 必须字符串: {extends!r}")
            # —— 形式 1："scope:name"（scope 支持 builtin / profile / 本地别名）
            if ":" in extends:
                scope, nm = extends.split(":", 1)
                if scope == "builtin":
                    parent_yml = Path(builtins_root) / nm / "profile.yaml"
                elif scope in {"profile", "profiles", "scope"}:
                    parent_yml = (
                        Path(builtins_root) / nm
                        if not nm.endswith((".yml", ".yaml"))
                        else Path(builtins_root) / nm
                    )
                else:
                    raise ValueError(f"未知 extends scope {scope!r}: {extends!r}")
            else:
                # —— 形式 2："foo.yaml" / "foo"（直接从 builtins_root 同目录找）
                fname = extends if (extends.endswith((".yml", ".yaml"))) else f"{extends}.yaml"
                candidate = Path(builtins_root) / fname
                if candidate.exists():
                    parent_yml = candidate
                else:
                    # 退化到 builtin: 风格（兼容老写法）
                    parent_yml = Path(builtins_root) / extends / "profile.yaml"
            if not parent_yml.exists():
                raise FileNotFoundError(f"extends={extends!r} 找不到: {parent_yml}")
            with open(parent_yml, "r", encoding="utf-8") as f:
                parent_data = yaml.safe_load(f) or {}
            base = self._resolve_one(parent_data, str(parent_yml), builtins_root, stack)
        merged = self._deep_merge(base, data)
        merged.pop("extends", None)
        stack.pop()
        return merged

    def _deep_merge(self, base: dict, override: dict) -> dict:
        out = copy.deepcopy(base)
        for k, v in override.items():
            if k == "extends":
                out[k] = v
                continue
            if k not in out:
                out[k] = copy.deepcopy(v)
                continue
            old = out[k]
            if isinstance(old, dict) and isinstance(v, dict):
                out[k] = self._deep_merge(old, v)
            elif isinstance(old, list) and isinstance(v, list):
                seen: list = []
                for item in old + v:
                    if item not in seen:
                        seen.append(item)
                out[k] = seen
            else:
                out[k] = copy.deepcopy(v)
        return out

    def _expand_env(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {k: self._expand_env(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._expand_env(x) for x in node]
        if isinstance(node, str):
            def repl1(m):
                # ${VAR:-default} 或 ${VAR-default}
                var = m.group(1)
                default = m.group(2) or ""
                use_colon = m.group(3) == ":-"
                val = os.environ.get(var)
                if val is None or (use_colon and val == ""):
                    return default
                return val

            def repl2(m):
                # ${VAR} (无默认)
                var = m.group(1)
                return os.environ.get(var, m.group(0))

            # 先处理带默认的，避免被 ${VAR} 先匹配掉
            out = re.sub(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-|-)((?:[^}]|\}(?!$))*)\}",
                repl1,
                node,
            )
            out = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl2, out)
            return out
        return node

    def validate_slot_bindings(self, registered: dict[str, set[str]]) -> None:
        if self._resolved is None:
            raise RuntimeError("Profile 未 resolve 不能调用 validate_slot_bindings。先 .resolve()")
        slots = self._resolved.get("slots", {})
        missing: list[str] = []
        for slot_name, impl_name in slots.items():
            if impl_name is None:
                continue
            if slot_name not in registered:
                missing.append(f"{slot_name}={impl_name!r}(未知 slot)")
                continue
            if impl_name not in registered[slot_name]:
                missing.append(f"{slot_name}={impl_name!r}")
        if missing:
            raise KeyError(f"以下 slot 绑定找不到已注册 impl: {', '.join(missing)}")

    def _check(self):
        if self._resolved is None:
            raise RuntimeError("Profile 未 resolve，不能访问属性。请先 .resolve()")

    @property
    def name(self) -> str:
        self._check()
        return self._resolved.get("name", "")

    @property
    def raw(self) -> dict:
        return copy.deepcopy(self._raw)

    @property
    def data(self) -> dict:
        self._check()
        return copy.deepcopy(self._resolved)


__all__ = ["Profile"]
