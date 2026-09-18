"""抓取 8001 页面内容、截图、关键 DOM 元素，用来对比新旧 UI 形态。"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        console_msgs = []
        page.on("console", lambda msg: console_msgs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: console_msgs.append(f"[pageerror] {err}"))

        await page.goto("http://127.0.0.1:8001/", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2500)

        # 截图保存
        out_dir = Path("E:/AI/MaiBot-main/直播/temp/ui_probe")
        out_dir.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out_dir / "home.png"), full_page=True)

        # 抓标题
        title = await page.title()

        # 抓页面中关键文本
        body_text = await page.evaluate("() => document.body.innerText.slice(0, 4000)")

        # 抓页面顶部 layout 类名 + 关键链接
        nav_summary = await page.evaluate(
            """() => {
                const out = {h1: [], navLinks: [], buttons: [], cards: [], sidebar: null};
                document.querySelectorAll('h1,h2').forEach(el => out.h1.push(el.tagName + ':' + el.innerText.trim().slice(0,80)));
                document.querySelectorAll('a').forEach(a => {
                    const t = a.innerText.trim();
                    if (t) out.navLinks.push(t.slice(0,60));
                });
                document.querySelectorAll('button').forEach(b => {
                    const t = b.innerText.trim();
                    if (t) out.buttons.push(t.slice(0,60));
                });
                const sidebar = document.querySelector('[data-slot=sidebar], aside, nav');
                if (sidebar) out.sidebar = sidebar.outerHTML.slice(0, 1500);
                return out;
            }"""
        )

        # 抓路由相关标记（新版会有 router-C3qF9KlH 之类的 preload）
        scripts = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)"
        )

        # 检查关键标签 / data 属性
        dashboard_attrs = await page.evaluate(
            """() => {
                const root = document.documentElement;
                return {
                    dashboardStyle: root.dataset.dashboardStyle,
                    classes: root.className,
                    bodyClass: document.body.className,
                    hasFocusTab: !!document.querySelector('[href*="focus"], [data-route*="focus"]'),
                    hasBilibiliTab: !!document.querySelector('[href*="bilibili"], [data-route*="bilibili"]'),
                    hasPluginConfig: !!document.querySelector('[href*="plugin-config"]'),
                };
            }"""
        )

        result = {
            "title": title,
            "scripts": scripts,
            "dashboard_attrs": dashboard_attrs,
            "nav_summary": nav_summary,
            "body_text_head": body_text[:2000],
            "console_first_30": console_msgs[:30],
        }
        (out_dir / "probe.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
