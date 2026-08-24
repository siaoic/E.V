"""Profile 单元测试（TR 3.1 / 3.2 / 3.3 + 循环依赖 + 直接 dict）。"""
from __future__ import annotations

import os

import pytest
import yaml

from ev.kernel.profile import Profile


def _write_yaml(path, data) -> None:
    """写入 yaml 文件到指定 path（自动创建父目录）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


# =====================================================================
# TR 3.1 extends 三级继承（C → B → A）
# =====================================================================
def test_three_level_inheritance(tmp_path):
    profiles_dir = tmp_path / "profiles"

    a_dir = profiles_dir / "A"
    _write_yaml(
        str(a_dir / "profile.yaml"),
        {
            "name": "A",
            "plugins": {"builtin": []},
            "slots": {"model": "a"},
            "plugin_config": {
                "llm": {"k1": "v1", "nested": {"a": 1}}
            },
        },
    )

    b_dir = profiles_dir / "B"
    _write_yaml(
        str(b_dir / "profile.yaml"),
        {
            "name": "B",
            "extends": "builtin:A",
            "plugins": {"builtin": ["tools"]},
            "slots": {"tts": "b"},
            "plugin_config": {
                "llm": {"k2": "v2", "nested": {"b": 2}}
            },
        },
    )

    c_dir = profiles_dir / "C"
    _write_yaml(
        str(c_dir / "profile.yaml"),
        {
            "name": "C",
            "extends": "builtin:B",
            "plugins": {"builtin": ["tools", "mindcraft"]},
            "slots": {"model": "c-ovr"},
            "plugin_config": {
                "llm": {"k1": "c-v1", "newk": "new"}
            },
        },
    )

    p = Profile(str(c_dir / "profile.yaml"))
    merged = p.resolve(builtins_root=str(profiles_dir))

    # plugins.builtin: 按首次出现去重 → B 的 tools 在前，C 重复 tools 跳过，追加 mindcraft
    assert merged["plugins"]["builtin"] == ["tools", "mindcraft"]

    # slots: A 的 model=a 被 C 覆盖为 c-ovr；B 的 tts=b 保留
    assert merged["slots"] == {"model": "c-ovr", "tts": "b"}

    # plugin_config 深合并
    assert merged["plugin_config"]["llm"] == {
        "k1": "c-v1",
        "k2": "v2",
        "nested": {"a": 1, "b": 2},
        "newk": "new",
    }


# =====================================================================
# TR 3.2 env var 替换
# =====================================================================
def test_env_var_replacement_set(tmp_path, monkeypatch):
    monkeypatch.setenv("EV5_TEST_VAR", "hello")
    profile_path = tmp_path / "env_profile.yaml"
    _write_yaml(
        str(profile_path),
        {
            "name": "env-test",
            "plugin_config": {"foo": {"base_url": "${EV5_TEST_VAR}"}},
        },
    )
    p = Profile(str(profile_path))
    merged = p.resolve(builtins_root=str(tmp_path))
    assert merged["plugin_config"]["foo"]["base_url"] == "hello"


def test_env_var_replacement_unset(tmp_path):
    """未设置的 EV5_TEST_UNSET：值保持占位符不变，不 crash。"""
    profile_path = tmp_path / "env_unset.yaml"
    _write_yaml(
        str(profile_path),
        {
            "name": "env-unset",
            "plugin_config": {"x": {"url": "${EV5_TEST_UNSET}"}},
        },
    )
    p = Profile(str(profile_path))
    merged = p.resolve(builtins_root=str(tmp_path))
    # 未设置 → 占位符保留原样
    assert merged["plugin_config"]["x"]["url"] == "${EV5_TEST_UNSET}"


# =====================================================================
# TR 3.3 validate_slot_bindings
# =====================================================================
def test_validate_slot_bindings_missing():
    p = Profile({"name": "v", "slots": {"model": "x", "tts": "y"}})
    p.resolve(builtins_root="")
    registered = {"model": {"a"}, "tts": {"b"}}
    with pytest.raises(KeyError) as exc:
        p.validate_slot_bindings(registered)
    msg = str(exc.value)
    assert "x" in msg
    assert "y" in msg


def test_validate_slot_bindings_ok():
    p = Profile({"name": "v", "slots": {"model": "x", "tts": "y"}})
    p.resolve(builtins_root="")
    registered = {"model": {"x"}, "tts": {"y"}}
    # 不抛异常
    p.validate_slot_bindings(registered)


def test_validate_slot_bindings_null_is_valid():
    """slots 中 impl_name 为 None/null 是显式禁用，不应报错。"""
    p = Profile({"name": "v", "slots": {"model": None, "tts": "y"}})
    p.resolve(builtins_root="")
    registered = {"tts": {"y"}}
    p.validate_slot_bindings(registered)  # 不抛


# =====================================================================
# 循环依赖测试（TR 15.3）
# =====================================================================
def test_circular_dependency(tmp_path):
    profiles_dir = tmp_path / "profiles"
    _write_yaml(
        str(profiles_dir / "A" / "profile.yaml"),
        {"name": "A", "extends": "builtin:B"},
    )
    _write_yaml(
        str(profiles_dir / "B" / "profile.yaml"),
        {"name": "B", "extends": "builtin:A"},
    )
    p = Profile(str(profiles_dir / "A" / "profile.yaml"))
    with pytest.raises(RuntimeError) as exc:
        p.resolve(builtins_root=str(profiles_dir))
    assert "循环依赖" in str(exc.value)


# =====================================================================
# 直接 dict Profile（不依赖文件）
# =====================================================================
def test_dict_profile_resolve(tmp_path):
    p = Profile({"name": "x", "slots": {"model": "abc"}})
    merged = p.resolve(builtins_root=str(tmp_path))
    assert merged["name"] == "x"
    assert merged["slots"] == {"model": "abc"}
    assert merged["plugins"] == {"builtin": [], "pypi": [], "git": []}


def test_dict_with_extends_uses_builtins_root(tmp_path):
    """直接 dict 形式也能 resolve extends（走 builtins_root）。"""
    profiles_dir = tmp_path / "profiles"
    _write_yaml(
        str(profiles_dir / "base" / "profile.yaml"),
        {
            "name": "base",
            "plugins": {"builtin": ["tools"]},
            "slots": {"model": "m1"},
        },
    )
    p = Profile({
        "name": "child",
        "extends": "builtin:base",
        "slots": {"tts": "t1"},
    })
    merged = p.resolve(builtins_root=str(profiles_dir))
    assert merged["plugins"]["builtin"] == ["tools"]
    assert merged["slots"] == {"model": "m1", "tts": "t1"}


# =====================================================================
# 属性访问未 resolve 抛错
# =====================================================================
def test_properties_require_resolve():
    p = Profile({"name": "x"})
    with pytest.raises(RuntimeError, match="未 resolve"):
        _ = p.name
