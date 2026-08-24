"""Handler 基类：所有处理器持有 RuntimeContext 引用。"""


class BaseHandler:
    """处理器基类：按职责封装原 Application 的协作逻辑。

    Handler 只访问 runtime 的组件/共享能力，不持有 Application 编排层，
    保证拆分后仍可用（逻辑零改动，self 引用改为 self.runtime）。
    """

    def __init__(self, runtime) -> None:
        self.runtime = runtime
