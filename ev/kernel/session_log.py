"""会话日志：按会话分 JSONL 文件持久化（TR 2.x 覆盖）。

自动开首次会话；append(type, payload, context) 写入 JSONL 行；new_session() 切换会话文件。
"""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional


class SessionLog:
    _SUB_DIR = "sessions"

    def __init__(self, data_root: str) -> None:
        self._root = Path(data_root) / self._SUB_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        self._session_id: Optional[str] = None
        self._fp = None
        self._count = 0
        self.new_session()

    def new_session(self) -> str:
        self.close()
        sid = uuid.uuid4().hex
        self._session_id = sid
        path = self._root / f"{sid}.jsonl"
        self._fp = open(path, "a", encoding="utf-8", buffering=1)
        self._count = 0
        return sid

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.flush()
            finally:
                self._fp.close()
            self._fp = None
        self._session_id = None

    def append(
        self,
        type_: str,
        payload: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> None:
        if self._fp is None or self._session_id is None:
            self.new_session()
        record = {
            "timestamp": time.time(),
            "type": type_,
            "payload": payload if payload is not None else {},
            "context": context if context is not None else {},
        }
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._count += 1

    def entries(self) -> list[dict]:
        if self._session_id is None:
            return []
        if self._fp is not None:
            self._fp.flush()
        path = self._root / f"{self._session_id}.jsonl"
        if not path.exists():
            return []
        rows: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def data_root(self) -> str:
        return str(self._root.parent)

    def all_sessions(self) -> list[str]:
        files = sorted(
            self._root.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [f.stem for f in files]


__all__ = ["SessionLog"]
