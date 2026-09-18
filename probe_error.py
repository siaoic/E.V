"""捕获 pageerror 完整堆栈，定位 forwardRef 报错位置。"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        events = []

        def on_pageerror(err):
            try:
                events.append({"type": "pageerror", "message": err.message, "stack": err.stack})
            except Exception:
                events.append({"type": "pageerror", "message": str(err)})

        page.on("pageerror", on_pageerror)

        page.on(
            "console",
            lambda msg: events.append(
                {"type": "console", "level": msg.type, "text": msg.text, "location": msg.location}
            ),
        )

        await page.goto("http://127.0.0.1:8001/", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        out_dir = Path("E:/AI/MaiBot-main/直播/temp/ui_probe")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"events={len(events)}")
        for e in events:
            if e["type"] == "pageerror":
                print("\n=== pageerror ===")
                print(e.get("message"))
                print(e.get("stack", "")[:3000])
            elif e["type"] == "console" and e.get("level") in ("error", "warning"):
                print(f"\n=== console {e['level']} ===")
                print(e.get("text")[:500])

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
