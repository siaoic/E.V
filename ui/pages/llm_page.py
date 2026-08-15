"""LLM 配置页（mixin）：LLM / Embedding / 管家模型字段回填。"""


class LLMPage:
    """LLM 配置页逻辑：启动时把当前 .env 值回填到各输入框。"""

    def _init_llm_page(self) -> None:
        # LLM 配置页回填
        self.ed_key.setText(self.cfg.LLM_API_KEY or "")
        self.ed_url.setText(self.cfg.LLM_BASE_URL or "")
        self.ed_model.setText(self.cfg.LLM_MODEL or "")
        self.ed_prompt.setPlainText(self.cfg.SYSTEM_PROMPT or "")
        # Embedding 配置回填（记忆检索/情绪嵌入，本地 llama.cpp 或云端）
        self.ed_emb_url.setText(self.cfg.EMBEDDING_BASE_URL or "")
        self.ed_emb_model.setText(self.cfg.EMBEDDING_MODEL or "")
        self.ed_emb_key.setText(self.cfg.EMBEDDING_API_KEY or "")
        # 管家模型（ButlerAgent 记忆管家）回填
        self.ed_butler_url.setText(self.cfg.BUTLER_BASE_URL or "")
        self.ed_butler_model.setText(self.cfg.BUTLER_MODEL or "")
        self.ed_butler_key.setText(self.cfg.BUTLER_API_KEY or "")
