"""临时脚本：把《沃尔夫将军之死》的构图渲染成查看页（用完即删）。

LLM 直接产出这幅油画的 SVG 一直是抽象色块，所以这里由作者手写构图几何，
仅用来验证查看页的「一笔一画」渲染效果。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))

from plugin import DrawingStore, ViewerRenderer, _extract_svg, _normalize_svg  # noqa: E402

OUT_DIR = Path(r"E:\AI\MaiBot-main\直播\data\plugins\maibot-team.ai-drawing-plugin\drawings")
SPEED = 3.0
HINT = "本杰明·韦斯特《沃尔夫将军之死》"

# 画布 480×324（贴近原画 800×540 的横幅比例），坐标按原画位置等比换算。
# 约定：背景大色块的 stroke 与 fill 同色，动画时像在铺色而不是画卡通描边；
# 人物统一用深色描边勾轮廓。
SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 324" fill="none">
  <defs>
    <linearGradient id="skyGrad" x1="0" y1="0" x2="0.8" y2="0.8">
      <stop offset="0%" stop-color="#ccd7de"/>
      <stop offset="40%" stop-color="#a9b3b5"/>
      <stop offset="70%" stop-color="#8a7b64"/>
      <stop offset="100%" stop-color="#5b4632"/>
    </linearGradient>
    <linearGradient id="groundGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#7e7a4c"/>
      <stop offset="35%" stop-color="#6a6039"/>
      <stop offset="100%" stop-color="#37251a"/>
    </linearGradient>
    <linearGradient id="flagGrad" x1="0" y1="0" x2="0.35" y2="1">
      <stop offset="0%" stop-color="#a8332b"/>
      <stop offset="52%" stop-color="#e6dfd2"/>
      <stop offset="100%" stop-color="#35507c"/>
    </linearGradient>
    <linearGradient id="coatGrad" x1="0" y1="0" x2="0.3" y2="1">
      <stop offset="0%" stop-color="#bd3d31"/>
      <stop offset="100%" stop-color="#82261e"/>
    </linearGradient>
  </defs>
  <g stroke="#2a2622" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">

    <!-- 天空：占画面上半部，左上灰蓝，右下压向暗褐 -->
    <path d="M -20 -20 L 500 -20 L 500 174 C 320 182 -20 178 -20 178 Z" fill="url(#skyGrad)" stroke="#c6cfd6"/>

    <!-- 右上翻滚的暗褐云团：压出画外，只留翻卷的边缘 -->
    <path d="M 206 -20 L 496 -20 L 496 92 C 428 118 328 106 258 74 C 210 50 190 4 206 -20 Z" fill="#4a392c" stroke="#4a392c"/>
    <path d="M 118 34 C 158 22 196 32 198 52 C 200 72 168 82 140 76 C 116 70 104 46 118 34 Z" fill="#6b6155" opacity="0.85" stroke="#6b6155"/>

    <!-- 中右上透光的亮云 -->
    <path d="M 228 96 C 250 74 292 66 330 72 C 356 62 400 66 424 80 C 452 78 470 92 462 108 C 448 128 396 134 344 128 C 296 122 250 118 232 110 C 224 104 224 100 228 96 Z" fill="#e0e3e2" stroke="#e0e3e2"/>
    <path d="M 12 136 C 26 116 66 106 104 110 C 130 104 164 112 172 128 C 186 130 190 142 178 150 C 152 166 92 164 52 154 C 26 148 12 144 12 136 Z" fill="#ccd3d8" opacity="0.8" stroke="#ccd3d8"/>

    <!-- 远景：地平线上的士兵剪影与硝烟 -->
    <path d="M 146 176 C 148 164 158 158 164 166 C 170 158 182 160 184 176 Z" fill="#55492f" stroke="#55492f"/>
    <path d="M 192 176 C 194 166 202 160 208 168 C 214 160 224 162 226 176 Z" fill="#4b4029" stroke="#4b4029"/>
    <path d="M 158 156 C 176 136 208 136 220 150 C 230 162 218 176 196 176 C 174 176 158 168 158 156 Z" fill="#cfccc2" opacity="0.85" stroke="#cfccc2"/>
    <path d="M 226 166 C 240 152 262 152 272 162 C 280 172 270 182 252 182 C 236 182 226 176 226 166 Z" fill="#c8c5ba" opacity="0.8" stroke="#c8c5ba"/>

    <!-- 远景：舰队桅杆与斜帆 -->
    <path d="M 356 176 L 356 132" stroke="#4a3b2b" stroke-width="2"/>
    <path d="M 356 134 C 376 142 386 158 380 176 L 356 176 Z" fill="#dedbd2"/>
    <path d="M 386 176 L 386 144 C 404 150 410 164 406 176 Z" fill="#d3cfc5"/>

    <!-- 地面：从地平线一直铺到画外 -->
    <path d="M -20 176 L 500 176 L 500 344 L -20 344 Z" fill="url(#groundGrad)" stroke="#5a4a2e"/>
    <path d="M -20 206 C 120 198 340 200 500 208 L 500 240 C 340 232 120 230 -20 238 Z" fill="#5f6a3c" opacity="0.5" stroke="#5f6a3c"/>

    <!-- 军旗：旗杆 + 从杆顶垂下、底边翻卷的旗面 -->
    <path d="M 312 54 L 318 206" stroke="#3f2f1e" stroke-width="3.6"/>
    <path d="M 314 58 L 472 64 C 466 92 472 116 456 128 C 420 146 352 138 322 120 C 310 106 308 74 314 58 Z" fill="url(#flagGrad)"/>
    <path d="M 340 122 C 388 116 436 104 466 88" stroke="#8c2f28" stroke-width="1.4"/>
    <path d="M 330 76 C 372 74 420 82 460 96" stroke="#eee7da" stroke-width="1.2" opacity="0.7"/>

    <!-- 将军背后左侧的军官群（后排蓝衣、前排红衣） -->
    <circle cx="72" cy="148" r="10.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="72" cy="151" r="9" fill="#c08a5c"/>
    <path d="M 58 166 C 52 188 52 210 57 230 L 89 230 C 94 208 92 186 85 166 Z" fill="#2c3e6b"/>
    <path d="M 86 184 C 98 192 106 200 108 212" stroke="#2c3e6b" stroke-width="6"/>

    <circle cx="102" cy="141" r="11.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="102" cy="144" r="10" fill="#c08a5c"/>
    <path d="M 86 159 C 80 183 80 208 85 230 L 121 230 C 126 206 124 181 117 159 Z" fill="url(#coatGrad)"/>
    <path d="M 84 204 L 122 204" stroke="#2a2622" stroke-width="2"/>

    <circle cx="134" cy="145" r="11.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="134" cy="148" r="10" fill="#c08a5c"/>
    <path d="M 118 163 C 112 185 113 206 118 228 L 156 228 C 161 204 158 183 152 163 Z" fill="url(#coatGrad)"/>
    <path d="M 150 178 C 168 186 180 196 186 208" stroke="#c0392b" stroke-width="6"/>

    <!-- 最左：张开双臂的绿衣军官 -->
    <circle cx="40" cy="151" r="11.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="40" cy="154" r="10" fill="#c08a5c"/>
    <path d="M 24 169 C 16 192 16 216 22 238 L 58 238 C 64 214 62 191 54 169 Z" fill="#4a6b3a"/>
    <path d="M 28 178 C 10 180 -2 172 0 158 L 10 156 C 12 168 22 172 34 170 Z" fill="#4a6b3a"/>
    <path d="M 56 178 C 76 182 90 174 88 158 L 78 156 C 76 168 66 174 52 170 Z" fill="#4a6b3a"/>

    <!-- 将军：地面投影 → 向左侧伸出的腿 → 鲜红军装、金黄马甲、白领巾 -->
    <path d="M 168 250 C 220 240 286 238 322 244 L 318 254 C 276 250 212 256 174 260 Z" fill="#2f2317" opacity="0.45" stroke="#2f2317"/>
    <path d="M 292 214 C 254 224 208 232 172 244 C 158 248 158 258 174 254 C 212 244 258 236 294 228 Z" fill="#9c2f24"/>
    <path d="M 168 244 C 154 246 146 252 148 258 C 152 264 166 262 176 256 Z" fill="#2a2622"/>
    <path d="M 262 210 C 274 216 288 218 300 214" stroke="#a93226" stroke-width="5"/>
    <path d="M 194 198 C 214 182 252 175 286 179 C 312 183 328 196 328 210 C 328 224 310 234 280 236 C 244 238 208 230 194 218 C 188 210 189 204 194 198 Z" fill="url(#coatGrad)"/>
    <path d="M 236 202 C 250 197 270 197 280 203 C 287 209 285 217 275 220 C 262 224 246 222 238 216 C 232 212 232 206 236 202 Z" fill="#a8762a"/>
    <path d="M 278 187 C 288 183 296 187 296 195 C 296 203 288 207 280 205 C 272 203 270 191 278 187 Z" fill="#e9e6dd"/>
    <circle cx="290" cy="173" r="11.5" fill="#4a3524" stroke="#4a3524"/>
    <circle cx="290" cy="176" r="10" fill="#d7a878"/>

    <!-- 将军右侧：半跪的深蓝外套军官，正伸手托住将军 -->
    <circle cx="336" cy="165" r="11.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="336" cy="168" r="10" fill="#c08a5c"/>
    <path d="M 322 183 C 314 204 314 222 318 238 L 354 238 C 359 220 356 197 350 183 Z" fill="#2c3e6b"/>
    <path d="M 316 236 C 306 250 308 264 322 268 L 356 268 C 362 254 358 242 352 236 Z" fill="#22305a"/>
    <path d="M 322 194 C 310 198 300 194 294 186" stroke="#2c3e6b" stroke-width="5"/>

    <!-- 右侧持枪的绿衣军官 -->
    <circle cx="378" cy="163" r="11.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="378" cy="166" r="10" fill="#c08a5c"/>
    <path d="M 364 181 C 358 202 358 224 362 244 L 396 244 C 401 222 398 201 392 181 Z" fill="#4a6b3a"/>
    <path d="M 356 146 L 348 248" stroke="#4a3b2b" stroke-width="2.4"/>

    <!-- 最右：红衣军官与白色马裤 -->
    <circle cx="434" cy="159" r="12.5" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="434" cy="162" r="11" fill="#c08a5c"/>
    <path d="M 416 179 C 408 205 406 234 411 260 L 455 260 C 460 232 457 202 449 179 Z" fill="url(#coatGrad)"/>
    <path d="M 409 236 L 458 236" stroke="#2a2622" stroke-width="2"/>
    <path d="M 419 192 C 404 198 396 208 396 218" stroke="#c0392b" stroke-width="5"/>
    <path d="M 414 260 L 408 318 L 426 318 L 432 260 Z" fill="#e0dcd0"/>
    <path d="M 434 260 L 436 318 L 454 318 L 452 260 Z" fill="#d3cfc2"/>

    <!-- 左前方的原住民武士：蹲坐、赤裸上身、蓝色织物、羽饰头巾 -->
    <path d="M 78 244 C 66 276 70 310 86 326 L 152 326 C 162 300 154 266 138 244 Z" fill="#2f4a72"/>
    <path d="M 96 234 C 90 254 92 276 98 292 L 132 292 C 138 274 136 252 130 234 Z" fill="#b07a4e"/>
    <path d="M 104 212 C 96 198 100 184 110 180" stroke="#b8402f" stroke-width="2"/>
    <path d="M 102 214 C 106 202 118 200 122 210 C 118 208 108 210 106 216 Z" fill="#b8402f"/>
    <circle cx="110" cy="217" r="10" fill="#3a2c22" stroke="#3a2c22"/>
    <circle cx="110" cy="220" r="9" fill="#b07a4e"/>
    <path d="M 100 238 C 96 228 100 218 110 218" stroke="#b07a4e" stroke-width="4"/>

    <!-- 前景左侧铺开的蓝色织物 -->
    <path d="M -20 252 C 16 240 58 246 82 262 C 94 288 90 314 74 330 L -20 330 Z" fill="#2f4a72"/>

    <!-- 前景散落的武器与军鼓 -->
    <path d="M 216 300 L 278 292 L 280 300 L 218 308 Z" fill="#5a4327"/>
    <path d="M 308 300 C 302 290 304 280 314 276" stroke="#b8402f" stroke-width="2"/>
    <path d="M 302 304 C 318 294 342 294 354 304 C 342 314 314 314 302 304 Z" fill="#2f2a24"/>
    <path d="M 368 298 L 400 298 L 404 324 L 364 324 Z" fill="#b08a33"/>
    <path d="M 366 306 L 402 306" stroke="#8c6a2b" stroke-width="1.6"/>
  </g>
</svg>
"""


def main() -> int:
    svg = _extract_svg(SVG)
    svg, info = _normalize_svg(svg)

    html = ViewerRenderer(PLUGIN_DIR / "renderer" / "drawing_viewer.html").render(svg, SPEED, HINT)
    page = DrawingStore(OUT_DIR).save(html, HINT)

    print(f"path 数 = {svg.lower().count('<path')}，元素数 = {len(svg.split('<')) - 1}，{info}")
    print(f"PAGE={page}")
    print(f"URL=http://127.0.0.1:8126/{page.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())