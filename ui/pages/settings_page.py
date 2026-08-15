"""设置页（mixin）：开关 / TTS / STT / B站弹幕字段回填。"""


class SettingsPage:
    """设置页逻辑：启动时把当前 .env 值回填到各控件。"""

    def _init_settings_page(self) -> None:
        # 设置页回填
        self.cb_mcp.setChecked(bool(self.cfg.TOOLS_ENABLED))
        self.cb_proactive.setChecked(bool(self.cfg.PROACTIVE_ENABLED))
        self.cb_filter.setChecked(bool(self.cfg.PROFANITY_FILTER_ENABLED))
        self.cb_stt.setChecked(bool(self.cfg.STT_ENABLED))
        self.ed_tts_audio.setText(self.cfg.GPTSOVITS_REF_AUDIO or "")
        self.ed_tts_text.setText(self.cfg.GPTSOVITS_PROMPT_TEXT or "")
        self.ed_tts_audios.setText(self.cfg.GPTSOVITS_REF_AUDIOS or "")
        self.ed_stt_key.setText(self.cfg.STT_API_KEY or "")
        self.ed_stt_url.setText(self.cfg.STT_BASE_URL or "")
        self.ed_stt_model.setText(self.cfg.STT_MODEL or "")
        self.cb_emotion_actor.setChecked(bool(self.cfg.EMOTION_ACTOR_ENABLED))
        # B站直播弹幕配置回填
        self.cb_bili_enabled.setChecked(bool(self.cfg.BILI_ENABLED))
        self.ed_bili_room.setText(
            str(self.cfg.BILI_ROOM_ID) if self.cfg.BILI_ROOM_ID else "")
        self.ed_bili_sessdata.setText(self.cfg.BILI_SESSDATA or "")
        self.ed_bili_port.setText(str(self.cfg.BILI_SERVER_PORT))
