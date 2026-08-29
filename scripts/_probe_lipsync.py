"""VTS 口型监视器（临时调试脚本，排查完删除）。

连接 VTS 后轮询当前模型 Live2D 输出参数值，实时观察嘴部参数是否被驱动。
用法：先运行本脚本，再让 AI 说一句话，观察输出。
结束按 Ctrl+C，脚本打印整个窗口期内各参数的最小/最大/变化幅度。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ev.vts.controller import VTSController

POLL_HZ = 10
DURATION_S = 60


async def main() -> None:
    vts = VTSController()
    ok = await vts.connect()
    if not ok:
        print("连接 VTS 失败")
        return
    model = await vts.get_current_model()
    print(f"当前模型: {model.get('modelName')!r}")
    params = await vts.get_output_parameters()
    print(f"输出参数共 {len(params)} 个，嘴部相关：")
    mouth_names = [p.get("name") for p in params
                   if any(k in p.get("name", "").lower()
                          for k in ("mouth", "parammouth", "eyeopen", "eye"))]
    if not mouth_names:
        print("  （未找到嘴/眼参数，将跟踪全部参数的变化）")
        mouth_names = [p.get("name") for p in params]
    else:
        print(f"  跟踪参数（嘴+眼眨眼）: {mouth_names}")

    stats = {n: [float("inf"), float("-inf")] for n in mouth_names}
    print(f"\n开始监视 {DURATION_S} 秒（{POLL_HZ}Hz）——现在让 AI 说句话！")
    print("（每个 5 秒打印一次嘴部参数当前值）\n")
    n_poll = 0
    for i in range(DURATION_S * POLL_HZ):
        try:
            params = await vts.get_output_parameters()
        except Exception as e:
            print(f"轮询失败：{e}")
            break
        n_poll += 1
        for p in params:
            n, v = p.get("name"), p.get("value")
            if n in stats and isinstance(v, (int, float)):
                s = stats[n]
                s[0] = min(s[0], v)
                s[1] = max(s[1], v)
        if i % (5 * POLL_HZ) == 0:
            cur = {p.get("name"): p.get("value") for p in params
                   if p.get("name") in mouth_names}
            print(f"[{i // POLL_HZ:3d}s] 当前值: {cur}")
        await asyncio.sleep(1 / POLL_HZ)

    print(f"\n===== 监视结束（共 {n_poll} 次轮询）=====")
    for n, (lo, hi) in stats.items():
        if lo <= hi:
            span = hi - lo
            mark = " <-- 有变化" if span > 0.01 else ""
            print(f"  {n}: min={lo:.3f} max={hi:.3f} span={span:.3f}{mark}")
    await vts.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n（手动中断）")
