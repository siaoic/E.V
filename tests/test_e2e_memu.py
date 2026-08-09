"""vtuber MemoryManager 端到端集成测试（memU 后端）。"""
import asyncio
import sys
sys.path.insert(0, r'e:\AI\vtuber\src')

from src.memory.memory import get_manager, retrieve, _USER_SELF, _USER_DEFAULT


async def test_e2e():
    mm = get_manager()

    # 1. commit_recall_files
    files = [
        {"name": "user-profile", "track": "memory", "description": "用户喜好",
         "content": "喜欢咖啡和深夜聊天", "user": "chao"},
        {"name": "deploy-skill", "track": "skill", "description": "部署技能",
         "content": "step 1: build\nstep 2: deploy", "user": "self"},
        {"name": "mood-log", "track": "memory", "description": "心情记录",
         "content": "今天心情不错，聊了很多有趣的话题", "user": "chao"},
    ]
    results = await mm.commit_recall_files(files)
    print(f"commit: {len(results)} results, events: {[r.get('event') for r in results]}")

    # 2. list_files
    listed = mm.list_files(limit=100)
    print(f"list_files: {len(listed)} files")
    for f in listed:
        print(f"  - {f['name']} (user={f.get('user')}, track={f.get('track')})")

    # 3. count
    print(f"count: {mm.count()}")

    # 4. retrieve
    prompt = await retrieve("咖啡", top_k=5, user="chao")
    print(f"retrieve(coffee): {len(prompt)} chars")
    if prompt:
        for line in prompt.split("\n")[:5]:
            print(f"  {line}")

    # 5. re-commit
    updated = [
        {"name": "user-profile", "track": "memory", "description": "用户喜好",
         "content": "喜欢茶和清晨散步", "user": "chao"},
    ]
    results2 = await mm.commit_recall_files(updated)
    print(f"re-commit: {len(results2)} results, events: {[r.get('event') for r in results2]}")

    # 6. verify old content gone
    prompt2 = await retrieve("咖啡", top_k=5, user="chao")
    print(f"retrieve(coffee) after update: {len(prompt2)} chars")

    prompt3 = await retrieve("茶", top_k=5, user="chao")
    print(f"retrieve(tea) after update: {len(prompt3)} chars")

    # 7. delete_memories
    if listed:
        del_count = mm.delete_memories([listed[0]["id"]])
        print(f"delete_memories: {del_count} deleted")
        print(f"count after delete: {mm.count()}")

    # 8. clear_all
    mm.clear_all()
    print(f"count after clear_all: {mm.count()}")

    print("\n=== E2E PASSED ===")


if __name__ == "__main__":
    asyncio.run(test_e2e())
