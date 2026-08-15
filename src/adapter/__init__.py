"""统一适配器层：外部服务（LLM / TTS / 形象 / 输入源）的标准契约。

设计目标：上层（主循环 / LLM 大脑 / stream）只依赖抽象基类，切换具体
实现（换模型 / 换 TTS / 换数字人）只需新增一个实现类，业务代码不改。

当前项目实现（均为真实实现，非空壳）：
  - BaseLLMAdapter      ← src/llm/llm_brain.py 的 LLMBrain
  - BaseTTSAdapter      ← src/tts/engine.py 的 TTSEngine
  - BaseAvatarAdapter   ← src/vts/controller.py 的 VTSController
  - BaseInputAdapter    ← src/asr/stt.py 的 STTEngine

基类抽象方法 = 现有实现的既有公共方法（签名一致），继承不改变任何行为。
"""
