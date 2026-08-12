"""LLM 测试：llama.cpp 后端（OpenAI 兼容 /v1/chat/completions）流式对话，
统计每问「发送 → 首个回复字到达」的首字延迟与总耗时。

用法：
1. 先启动 llama.cpp 服务：双击 llamacpp启动.bat（监听 http://127.0.0.1:8080）
2. 运行本脚本（需在 runtime 环境）：
   - 交互模式：python tests\test_llm_llamacpp.py
   - 单次提问：python tests\test_llm_llamacpp.py "你的问题"
3. 交互模式下输入 quit / exit 或空行退出
"""

import os
import sys
import time

# 项目根目录注入 sys.path（本脚本位于 tests/ 下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import console  # noqa: E402
from openai import OpenAI  # noqa: E402

# llama.cpp OpenAI 兼容服务地址（与 llamacpp启动.bat 的端口一致）
_LLAMACPP_BASE_URL = "http://127.0.0.1:8080/v1"
# llama.cpp 不校验模型名，传任意占位即可
_MODEL = "local-llama"


def ask_once(client: OpenAI, question: str) -> None:
    """单次提问：流式打印回复，并统计首字延迟与总耗时。"""
    send_time = time.perf_counter()
    first_time = None
    reply_parts = []
    try:
        response = client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": question}],
            stream=True,
            max_tokens=2048,
            temperature=0.7,
        )
        for chunk in response:
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None) or ""
            if not content:
                continue
            if first_time is None:
                first_time = time.perf_counter()  # 首个回复字到达
            reply_parts.append(content)
            print(content, end="", flush=True)
    except Exception as e:
        print()
        console.error(f"llama.cpp 调用失败：{e}（请先运行 llamacpp启动.bat）")
        return

    reply = "".join(reply_parts)
    elapsed_ms = (time.perf_counter() - send_time) * 1000
    first_ms = (first_time - send_time) * 1000 if first_time is not None else 0.0
    print()
    print(f"[延迟] 首字 {first_ms:7.1f} ms | 总耗时 {elapsed_ms:7.1f} ms | "
          f"{len(reply)} 字")
    print()


def main() -> None:
    # 强制 UTF-8 输出，保证中文回复正常显示
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    client = OpenAI(
        api_key="not-needed",
        base_url=_LLAMACPP_BASE_URL,
        timeout=120.0,
        max_retries=1,
    )

    if len(sys.argv) > 1:
        ask_once(client, sys.argv[1])
        return

    print("交互对话（quit / exit / 空行退出）：")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question or question.lower() in ("quit", "exit", "q"):
            break
        ask_once(client, question)


if __name__ == "__main__":
    main()
