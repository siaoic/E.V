"""桌宠模式（RUN_MODE=pet）：live2d-py + PySide6 本地渲染，不依赖 VTubeStudio。

- widget.py：PetWidget 透明无边框置顶窗口（渲染 / 拖拽 / 点击 / 气泡字幕）+ BubbleSub
- driver.py：PetFaceDriver，复用 FaceDriver 的口型/眨眼/节拍逻辑驱动本地模型
- emotion_actor.py：PetEmotionActor，Embedding 情绪分类 → 播放表情/动作
- pet_app.py：run_pet_app，live2d 生命周期 + Qt 定时器泵桥 asyncio 入口
"""
