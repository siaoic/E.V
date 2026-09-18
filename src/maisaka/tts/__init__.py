# -*- coding: utf-8 -*-
"""TTS 流式朗读：把 AI 回复流式合成并播放到音频输出设备。"""

from src.maisaka.tts.player import TtsPlayer, load_config

# 全局朗读播放器单例，供发送链路非阻塞调用
tts_player = TtsPlayer(load_config())

__all__ = ["TtsPlayer", "tts_player", "load_config"]