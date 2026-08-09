"""memU 官方 test_agentic.py 核心用例的 1:1 复刻。

绕过 memU conftest.py 对 memu.hosts.templates 的依赖（hosts 层不适用于 vtuber），
仅验证三个 agentic 入口（commit_results → list_all_recall_files → progressive_retrieve）
在 inmemory / sqlite 两个 backend 下的行为契约。
"""

from __future__ import annotations

import os
import sys
from typing import Any

# memU 以顶层包 `memu` 导入（本体在 src/memory/memu/），需把 src/memory 加入 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MEMU_DIR = os.path.join(_PROJECT_ROOT, "src", "memory")
if _MEMU_DIR not in sys.path:
    sys.path.insert(0, _MEMU_DIR)

import pytest

from memu.app import MemoryService


class FakeEmbeddingClient:
    """Deterministic embeddings: similar strings share a prefix dimension.

    Returns ``(vectors, raw_response)`` like every real client.
    """

    embed_model = "fake"

    async def embed(self, inputs: list[str]) -> tuple[list[list[float]], None]:
        vectors = []
        for text in inputs:
            lowered = text.lower()
            vectors.append([
                1.0 if "coffee" in lowered else 0.0,
                1.0 if "deploy" in lowered else 0.0,
                1.0 if "notes" in lowered else 0.0,
                float(len(lowered) % 5) / 10.0,
            ])
        return vectors, None


def make_service(database_config: dict[str, Any]) -> MemoryService:
    service = MemoryService(database_config=database_config)
    fake = FakeEmbeddingClient()
    service._embedding_pool._cache["default"] = fake
    service._embedding_pool._cache["embedding"] = fake
    return service


@pytest.fixture(params=["inmemory", "sqlite"])
def service(request: pytest.FixtureRequest, tmp_path: Any) -> MemoryService:
    if request.param == "inmemory":
        return make_service({"metadata_store": {"provider": "inmemory"}})
    return make_service({"metadata_store": {"provider": "sqlite", "dsn": f"sqlite:///{tmp_path}/memu.sqlite3"}})


async def _seed(svc: MemoryService) -> dict[str, Any]:
    return await svc.commit_results(
        recall_files=[
            {"name": "Profile", "track": "memory", "description": "who the user is", "content": "# P\nlikes coffee"},
            {"name": "deploy-checklist", "track": "skill", "description": "how to deploy", "content": "step 1"},
        ],
        resource=[{"path": "/workspace/notes.md", "description": "meeting notes"}],
    )


async def test_commit_then_list_covers_both_tracks(service: MemoryService) -> None:
    result = await _seed(service)
    assert len(result["recall_files"]) == 2
    assert len(result["resources"]) == 1
    assert all("embedding" not in f for f in result["recall_files"])

    listed = await service.list_all_recall_files()
    by_track = sorted((f["track"], f["name"]) for f in listed["recall_files"])
    assert by_track == [("memory", "Profile"), ("skill", "deploy-checklist")]


async def test_list_all_recall_files_paginates_by_track_name_id(service: MemoryService) -> None:
    limit = 3
    committed = await service.commit_results(
        recall_files=[
            {"name": f"m{i:02d}", "track": "memory", "description": "d", "content": f"line {i}"} for i in range(4)
        ]
        + [{"name": f"s{i:02d}", "track": "skill", "description": "d", "content": f"step {i}"} for i in range(4)],
    )
    assert len(committed["recall_files"]) == 8

    seen: list[tuple[str, str]] = []
    pages = 0
    cursor: str | None = None
    while True:
        page = await service.list_all_recall_files(cursor=cursor, limit=limit)
        assert len(page["recall_files"]) <= limit
        seen.extend((f["track"], f["name"]) for f in page["recall_files"])
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break

    expected = sorted([("memory", f"m{i:02d}") for i in range(4)] + [("skill", f"s{i:02d}") for i in range(4)])
    assert seen == expected
    assert pages == 3


async def test_progressive_retrieve_ranks_all_three_layers(service: MemoryService) -> None:
    await _seed(service)
    result = await service.progressive_retrieve("coffee")

    assert next(seg["text"] for seg in result["segments"]) == "likes coffee"
    file_names = [f["name"] for f in result["files"]]
    assert file_names[0] == "Profile"
    assert [r["url"] for r in result["resources"]] == ["/workspace/notes.md"]


async def test_recommit_updates_content_and_segments(service: MemoryService) -> None:
    await _seed(service)
    await service.commit_results(
        recall_files=[{"name": "Profile", "track": "memory", "description": "who", "content": "# P\nlikes tea"}]
    )

    listed = await service.list_all_recall_files()
    profile = next(f for f in listed["recall_files"] if f["name"] == "Profile")
    assert profile["content"] == "# P\nlikes tea"

    result = await service.progressive_retrieve("tea time")
    assert "likes coffee" not in [seg["text"] for seg in result["segments"]]


class CountingEmbeddingClient(FakeEmbeddingClient):
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, inputs: list[str]) -> tuple[list[list[float]], None]:
        self.calls += 1
        return await super().embed(inputs)


async def test_recommit_reembeds_description_only_when_changed(service: MemoryService) -> None:
    counter = CountingEmbeddingClient()
    service._embedding_pool._cache["default"] = counter
    service._embedding_pool._cache["embedding"] = counter

    file = {"name": "Profile", "track": "memory", "description": "who the user is", "content": "# P\nlikes coffee"}
    await service.commit_results(recall_files=[file])

    counter.calls = 0
    await service.commit_results(recall_files=[dict(file)])
    assert counter.calls == 0

    counter.calls = 0
    await service.commit_results(recall_files=[{**file, "description": "the user profile"}])
    assert counter.calls == 1

    listed = await service.list_all_recall_files()
    profile = next(f for f in listed["recall_files"] if f["name"] == "Profile")
    assert profile["description"] == "the user profile"


async def test_recommit_updates_skill_description_and_segment(service: MemoryService) -> None:
    file = {"name": "deploy-checklist", "track": "skill", "description": "how to deploy", "content": "step 1"}
    await service.commit_results(recall_files=[file])
    await service.commit_results(recall_files=[{**file, "description": "deploy the app"}])

    listed = await service.list_all_recall_files()
    skill = next(f for f in listed["recall_files"] if f["name"] == "deploy-checklist")
    assert skill["description"] == "deploy the app"

    result = await service.progressive_retrieve("deploy")
    seg_texts = [seg["text"] for seg in result["segments"]]
    assert "name: deploy-checklist\ndescription: deploy the app" in seg_texts
    assert "name: deploy-checklist\ndescription: how to deploy" not in seg_texts


async def test_where_scope_filters_and_rejects_unknown_fields(service: MemoryService) -> None:
    await service.commit_results(
        recall_files=[{"name": "A", "track": "memory", "description": "d", "content": "alpha"}],
        user={"user_id": "u1"},
    )
    await service.commit_results(
        recall_files=[{"name": "B", "track": "memory", "description": "d", "content": "beta"}],
        user={"user_id": "u2"},
    )

    listed = await service.list_all_recall_files(where={"user_id": "u1"})
    assert [f["name"] for f in listed["recall_files"]] == ["A"]

    with pytest.raises(ValueError, match="Unknown filter field"):
        await service.list_all_recall_files(where={"nope": "x"})


async def test_progressive_retrieve_rejects_empty_query(service: MemoryService) -> None:
    with pytest.raises(ValueError, match="empty_query"):
        await service.progressive_retrieve("   ")
