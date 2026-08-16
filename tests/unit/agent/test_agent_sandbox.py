"""Agent 沙箱单元测试：路径越界 / 高风险门禁 / 文件名清理。"""
import pytest

from src.agent.sandbox import Sandbox, SandboxViolation, HIGH_RISK_TOOLS


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(root=str(tmp_path), allow_shell=False)


@pytest.fixture
def sandbox_shell(tmp_path) -> Sandbox:
    return Sandbox(root=str(tmp_path), allow_shell=True)


class TestResolve:
    def test_relative_inside(self, sandbox, tmp_path):
        p = sandbox.resolve("sub/file.txt")
        assert p == (tmp_path / "sub" / "file.txt").resolve()

    def test_absolute_inside(self, sandbox, tmp_path):
        target = tmp_path / "a.txt"
        assert sandbox.resolve(str(target)) == target.resolve()

    def test_traversal_rejected(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.resolve("../outside.txt")

    def test_absolute_escape_rejected(self, sandbox):
        with pytest.raises(SandboxViolation):
            sandbox.resolve("C:/Windows/system32/evil.exe")

    def test_dot_dot_inside_normalized(self, sandbox, tmp_path):
        # ./sub/../file.txt 归一化后仍在工作区内 → 允许
        p = sandbox.resolve("sub/../file.txt")
        assert p == (tmp_path / "file.txt").resolve()


class TestHighRiskGate:
    def test_high_risk_rejected_by_default(self, sandbox):
        for tool in HIGH_RISK_TOOLS:
            assert not sandbox.check(tool)

    def test_high_risk_allowed_with_shell(self, sandbox_shell):
        for tool in HIGH_RISK_TOOLS:
            assert sandbox_shell.check(tool)

    def test_safe_tool_allowed(self, sandbox):
        assert sandbox.check("read_file")
        assert sandbox.check("list_dir")

    def test_unknown_tool_allowed(self, sandbox):
        # 未知工具不属高风险 → 放行（沙箱只管已知高风险）
        assert sandbox.check("web_search")


class TestSanitizeFilename:
    def test_invalid_chars_replaced(self):
        assert Sandbox.sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_empty_fallback(self):
        assert Sandbox.sanitize_filename("") == "default"
        assert Sandbox.sanitize_filename("   ") == "default"

    def test_normal_name_kept(self):
        assert Sandbox.sanitize_filename("周记.md") == "周记.md"
