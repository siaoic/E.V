"""Agent 任务 DAG：把多步任务按依赖关系显式编排，自动拓扑排序执行。

scheduler 是 cron 式定时，复杂任务要靠插件手动串。DAG 提供：
- 显式依赖：add(name, fn, depends_on=[...])
- 自动排序：run(entry) 按拓扑序执行 entry 及其所有下游
- 状态传递：每个节点 fn(state) -> result，存入 state[name]，下游可读
- 环检测：DFS on_stack 检测环依赖，抛 DAGCycleError
- 失败策略：fail_fast（默认，节点异常终止 DAG）/ fail_open（记录错误继续下游）

不强制接入主流程——butler/evolution/proactive 各自 cron/事件触发不变。
DAG 是可选编排工具，供插件/技能把多步任务串成可观测的依赖图。

用法：
    dag = AgentDAG()
    async def extract(state): return ["fact1"]
    async def review(state): return f"复盘 {state['extract']}"
    dag.add("extract", extract, depends_on=[])
    dag.add("review", review, depends_on=["extract"])
    result = await dag.run("extract")  # state = {"extract": [...], "review": "..."}
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

DagNode = Callable[[dict], Awaitable[Any]]


class DAGCycleError(Exception):
    """DAG 存在环依赖（A→B→A）。"""


class DAGNodeError(Exception):
    """节点执行失败（fail_fast 模式下抛出）。"""

    def __init__(self, node: str, cause: Exception) -> None:
        self.node = node
        self.cause = cause
        super().__init__(f"节点 {node} 执行失败：{cause}")


class AgentDAG:
    """Agent 任务 DAG：定义节点依赖，自动拓扑排序执行。"""

    def __init__(self, *, fail_fast: bool = True) -> None:
        self.nodes: dict[str, DagNode] = {}
        # edges: node -> 依赖此节点的下游列表（forward 遍历用）
        self.edges: dict[str, list[str]] = {}
        # depends: node -> 此节点依赖的上游列表（拓扑排序用）
        self.depends: dict[str, list[str]] = {}
        self._fail_fast = fail_fast

    def add(self, name: str, fn: DagNode,
            depends_on: list[str] | None = None) -> "AgentDAG":
        """添加节点 + 依赖；返回 self（链式调用）。

        fn 签名：async def fn(state: dict) -> Any；产出存入 state[name]。
        """
        self.nodes[name] = fn
        self.edges.setdefault(name, [])
        deps = list(depends_on or [])
        self.depends[name] = deps
        for dep in deps:
            self.edges.setdefault(dep, []).append(name)
        return self

    async def run(self, entry: str) -> dict[str, Any]:
        """从 entry 出发，按拓扑序执行 entry 及其所有下游；返回 state。

        fail_fast=True（默认）：节点抛异常 → DAG 立即终止，抛 DAGNodeError。
        fail_fast=False：节点抛异常 → 记录 state[node]={"_error": str}，
        继续执行不依赖该节点的下游（依赖该节点的下游会读到 _error）。
        """
        order = self._topo_sort(entry)
        state: dict[str, Any] = {}
        for node in order:
            fn = self.nodes[node]
            try:
                result = await fn(state)
                state[node] = result
            except Exception as e:
                if self._fail_fast:
                    raise DAGNodeError(node, e) from e
                state[node] = {"_error": str(e)}
        return state

    def _topo_sort(self, entry: str) -> list[str]:
        """从 entry 出发，收集所有 forward 可达下游，按拓扑序（依赖先）排序。

        环检测用 DFS on_stack：若回访当前路径上的节点，抛 DAGCycleError。
        """
        if entry not in self.nodes:
            raise KeyError(f"入口节点不存在：{entry}")
        # forward 可达：entry 及其所有下游
        reachable: set[str] = set()
        stack = [entry]
        while stack:
            node = stack.pop()
            if node in reachable:
                continue
            reachable.add(node)
            for downstream in self.edges.get(node, []):
                stack.append(downstream)
        # 在 reachable 子图内做拓扑排序（依赖先执行）
        visited: set[str] = set()
        on_stack: set[str] = set()
        order: list[str] = []

        def visit(node: str) -> None:
            if node in visited or node not in reachable:
                return
            if node in on_stack:
                raise DAGCycleError(f"环依赖：{node}")
            on_stack.add(node)
            for dep in self.depends.get(node, []):
                visit(dep)
            on_stack.discard(node)
            visited.add(node)
            order.append(node)

        for node in reachable:
            visit(node)
        return order

    def to_dot(self) -> str:
        """生成 Graphviz DOT 描述（可视化用，可选）。

        可用 `dot -Tpng dag.dot -o dag.png` 渲染。
        """
        lines = ["digraph agent_dag {"]
        for node in self.nodes:
            lines.append(f'  "{node}";')
        for node, deps in self.depends.items():
            for dep in deps:
                lines.append(f'  "{dep}" -> "{node}";')
        lines.append("}")
        return "\n".join(lines)
