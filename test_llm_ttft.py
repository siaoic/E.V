"""GLM 首字延迟（TTFT）测试：验证流式输出从请求发出到首个 token 到达的耗时。

用法：runtime\\Scripts\\python.exe test_llm_ttft.py

- system prompt 使用 ui/data/system_prompt.txt（与运行环境同一份人设）
- 模型/端点/密钥取自 .env（LLM_BASE_URL / LLM_MODEL / LLM_API_KEY）
- 输出每轮：首字延迟、总耗时、总字符、生成速率；最后汇总均值
"""

import os
import time

from openai import OpenAI

# system prompt 固定使用 UI 人设文件（与运行时保持一致）
_SYSTEM_PROMPT_FILE = os.path.join("ui", "data", "system_prompt.txt")

# 测试问题集：覆盖短回复、需要一点推理、需要一点长度的三类
_TEST_QUESTIONS = [
    "Hello, Neuro. Say hi.",
    "What do you think about blue milk?",
    "Tell me a short story about stealing Vedal's milk, three sentences max.",
]

# 每轮允许的最大生成字符（流式提前截断，节省 token 同时不影响 TTFT 测量）
_MAX_CHARS = 400


def load_system_prompt() -> str:
    """读取 UI 人设文件内容（UTF-8），失败时给明确报错。"""
    with open(_SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def build_client() -> OpenAI:
    """按 .env 配置构造 OpenAI 兼容客户端（与 LLMBrain 同款参数）。"""
    from src.utils import config
    return OpenAI(
        api_key=config.cfg.LLM_API_KEY or "not-needed",
        base_url=config.cfg.LLM_BASE_URL or None,
        timeout=120.0,
        max_retries=2,
    )


def run_one(client: OpenAI, system_prompt: str, question: str, idx: int) -> dict:
    """跑一轮流式对话，返回 TTFT / 总耗时 / 字符数等统计。"""
    from src.utils import config
    print(f"\n--- 第 {idx} 轮：{question} ---")
    t0 = time.perf_counter()          # 请求发起时刻
    first_token_t = None              # 首个 token 到达时刻
    chars = 0
    stream = client.chat.completions.create(
        model=config.cfg.LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        stream=True,
    )
    collected = []
    for chunk in stream:
        now = time.perf_counter()
        if first_token_t is None:
            delta = chunk.choices[0].delta if chunk.choices else None
            piece = (delta.content or "") if delta else ""
            if piece:
                first_token_t = now
                print(f"[TTFT] 首字延迟 {1000 * (now - t0):.0f} ms")
        if chunk.choices and chunk.choices[0].delta:
            piece = chunk.choices[0].delta.content or ""
            if piece:
                collected.append(piece)
                chars += len(piece)
                if chars >= _MAX_CHARS:
                    break
    t1 = time.perf_counter()
    elapsed = t1 - t0
    text = "".join(collected)
    print(f"[OK] 总耗时 {elapsed:.2f} s | 字符 {chars} | 速率 {chars / elapsed:.0f} 字符/s")
    print(f"[预览] {text[:120]!r}")
    return {
        "ttft_ms": 1000 * (first_token_t - t0) if first_token_t else None,
        "elapsed_s": elapsed,
        "chars": chars,
        "preview": text[:120],
    }


def main() -> None:
    system_prompt = load_system_prompt()
    print(f"system prompt: {_SYSTEM_PROMPT_FILE}（{len(system_prompt)} 字符）")
    client = build_client()
    from src.utils import config
    # 预热：先发一个最小请求建立 TLS 连接，消除首轮握手冷启动，
    # 让所有测试轮都命中热连接（与运行时的 warmup 行为一致）
    try:
        t0 = time.perf_counter()
        client.chat.completions.create(
            model=config.cfg.LLM_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        print(f"[预热] 连接建立完成（{1000 * (time.perf_counter() - t0):.0f} ms）")
    except Exception as e:
        print(f"[预热] 跳过（{e}）")
    results = []
    for i, q in enumerate(_TEST_QUESTIONS, 1):
        results.append(run_one(client, system_prompt, q, i))
    print("\n===== 汇总 =====")
    ok = [r for r in results if r["ttft_ms"] is not None]
    for r in results:
        ttft = f"{r['ttft_ms']:.0f} ms" if r["ttft_ms"] is not None else "失败"
        print(f"  {ttft:>10} | 总 {r['elapsed_s']:.2f}s | {r['chars']} 字符 | {r['preview']!r}")
    if ok:
        avg = sum(r["ttft_ms"] for r in ok) / len(ok)
        print(f"\n平均首字延迟：{avg:.0f} ms（{len(ok)}/{len(results)} 轮成功）")


if __name__ == "__main__":
    main()
