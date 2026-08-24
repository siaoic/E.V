"""配置读写（mixin）：保存全部配置到 .env / 人设文件，含保存反馈。"""

import os

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QMessageBox

from ev.utils import config, console
from ui.utils import env_helpers


def _save_ui_system_prompt(text: str) -> None:
    """保存控制中心 UI 人设到 ui/data/system_prompt.txt。

    SYSTEM_PROMPT_FILE 未配置时，src/utils/config.py 会自动读取该文件作为人设
    （路径与 config._UI_SYSTEM_PROMPT_FILE 保持一致，含 PyInstaller frozen 模式）。
    编辑框只显示正文（config 加载时剥离 frontmatter）；若原文件带 YAML
    frontmatter（--- name/description ---），保存时原样保留，避免覆盖保存
    把 skill 元数据静默清掉。
    """
    path = config._UI_SYSTEM_PROMPT_FILE
    frontmatter = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                frontmatter = raw[: end + 4] + "\n"
    except OSError:
        pass
    if frontmatter + text == raw:
        return  # 内容未变，不重写文件（增量保存：只写修改过的选项）
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(frontmatter + text)
    except OSError as e:
        console.error(f"保存 UI 人设失败：{e}")


class ConfigHandler:
    """配置保存：一次保存 LLM + 设置页全部字段，并热通知运行中的主程序。"""

    # 保存基准字段：控制中心启动时的 .env 值（首次保存时固化）。
    # 判断「用户是否改过该字段」的对比基准必须用快照，不能用 self.cfg
    # （=config.cfg 单例）——reload_config() 每次保存后会原地刷新单例，
    # 第二次保存基准就变成「当前 .env」，而 UI 字段仍是启动时的旧值 →
    # 未动过的字段被误判为「已变化」→ 把外部手动改的 .env 用旧 UI 值
    # 回写覆盖（曾覆盖 STT_BASE_URL/STT_MODEL 的云端配置）。
    _ENV_SNAPSHOT_FIELDS = (
        "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
        "EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY",
        "BUTLER_BASE_URL", "BUTLER_MODEL", "BUTLER_API_KEY",
        "TOOLS_ENABLED", "PROACTIVE_ENABLED", "PROFANITY_FILTER_ENABLED",
        "STT_ENABLED", "STT_API_KEY", "STT_BASE_URL", "STT_MODEL",
        "EMOTION_ACTOR_ENABLED",
        "GPTSOVITS_REF_AUDIO", "GPTSOVITS_PROMPT_TEXT", "GPTSOVITS_REF_AUDIOS",
        "BILI_ENABLED", "BILI_ROOM_ID", "BILI_SESSDATA", "BILI_SERVER_PORT",
        "PET_IDLE_MOTION", "SYSTEM_PROMPT_FILE",
    )

    def _env_base(self) -> dict:
        """保存对比基准（懒固化一次）：控制中心启动时的 .env 字段值。"""
        if getattr(self, "_env_base_snapshot", None) is None:
            self._env_base_snapshot = {
                k: getattr(config.cfg, k) for k in self._ENV_SNAPSHOT_FIELDS}
        return self._env_base_snapshot

    # —— 增量保存辅助：只写「被修改过」的配置项 ——
    def _env_save_if_changed(self, key: str, ui_value: str) -> None:
        """UI 值 ≠ 启动时快照值（用户改过）才写 .env；未修改跳过，
        避免点「更新配置」把全部行无条件重写（覆盖手动改动/刷新写盘）。"""
        v = (ui_value or "").strip()
        if v == (str(self._env_base().get(key) or "") or "").strip():
            return
        env_helpers._update_env(key, v)

    def _env_bool_save_if_changed(self, key: str, ui_checked: bool) -> None:
        """布尔开关版本：勾选状态没变不写。"""
        if ui_checked != bool(self._env_base().get(key)):
            env_helpers._update_env(key, "true" if ui_checked else "false")

    def _env_save_skip_default_if_changed(
            self, key: str, ui_value: str, default: str) -> None:
        """skip_default 版本：未修改跳过；修改过按默认值语义写（=默认移除行）。"""
        if (ui_value or "").strip() == (str(self._env_base().get(key) or "") or "").strip():
            return
        env_helpers._update_env_skip_default(key, ui_value, default)

    def _save_config(self) -> None:
        """底部「更新配置」：一次保存 LLM 配置 + 设置页全部字段到 .env，
        并把表情绑定映射一并写入当前模式的映射文件。
        """

        # B站直播弹幕：房间号/端口必须是纯数字。非数字会被写进 .env，主程序
        # reload_config 里 int() 解析直接抛 ValueError（_dispatch 未捕获 → 崩溃
        # 整个主程序、连会话归档都丢）。在写入边界拦截并阻止保存。
        for key, v in (("BILI_ROOM_ID", self.ed_bili_room.text().strip()),
                       ("BILI_SERVER_PORT", self.ed_bili_port.text().strip())):
            if v and not v.isdigit():
                QMessageBox.warning(
                    self.ui, "配置错误", f"{key} 必须是纯数字，当前输入：{v}")
                return

        _saved = False   # .env 全部写盘成功后才发热更新命令（避免主程序读到旧值）
        _stt_changed = False   # 语音识别开关/Key 变化才发 !stt 热重启引擎
        _tools_changed = False   # 工具总开关变化才发 !tools 热启停/重启工具
        _emb_changed = False   # Embedding/管家模型变化：无法热更新，需重启主程序
        try:
            # LLM 配置（未修改跳过，避免全量重写 .env）
            self._env_save_if_changed("LLM_API_KEY", self.ed_key.text().strip())
            self._env_save_if_changed("LLM_BASE_URL", self.ed_url.text().strip())
            self._env_save_if_changed("LLM_MODEL", self.ed_model.text().strip())
            # Embedding 配置（记忆检索/情绪嵌入）
            self._env_save_if_changed(
                "EMBEDDING_BASE_URL", self.ed_emb_url.text().strip())
            self._env_save_if_changed(
                "EMBEDDING_MODEL", self.ed_emb_model.text().strip())
            self._env_save_if_changed(
                "EMBEDDING_API_KEY", self.ed_emb_key.text().strip())
            # 管家模型（ButlerAgent 记忆管家）
            self._env_save_if_changed(
                "BUTLER_BASE_URL", self.ed_butler_url.text().strip())
            self._env_save_if_changed(
                "BUTLER_MODEL", self.ed_butler_model.text().strip())
            self._env_save_if_changed(
                "BUTLER_API_KEY", self.ed_butler_key.text().strip())
            _emb_changed = (
                self.ed_emb_url.text().strip() != (self.cfg.EMBEDDING_BASE_URL or "")
                or self.ed_emb_model.text().strip() != (self.cfg.EMBEDDING_MODEL or "")
                or self.ed_emb_key.text().strip() != (self.cfg.EMBEDDING_API_KEY or "")
                or self.ed_butler_url.text().strip() != (self.cfg.BUTLER_BASE_URL or "")
                or self.ed_butler_model.text().strip() != (self.cfg.BUTLER_MODEL or "")
                or self.ed_butler_key.text().strip() != (self.cfg.BUTLER_API_KEY or ""))
            # 人设：SYSTEM_PROMPT_FILE 配置时人设来自 skill 文件夹（ed_prompt 只是预览）。
            # 未配置时，人设保存到 ui/data/system_prompt.txt（config 自动读取），
            # 不再写入 .env——多行未加引号会把 .env 撑成 5000+ 行无法解析
            # （python-dotenv 每次启动刷几百条 could not parse statement）。
            if not str(self._env_base().get("SYSTEM_PROMPT_FILE") or "").strip():
                _save_ui_system_prompt(self.ed_prompt.toPlainText().strip())
            # 设置（未修改跳过）
            self._env_bool_save_if_changed("TOOLS_ENABLED", self.cb_mcp.isChecked())
            self._env_bool_save_if_changed(
                "PROACTIVE_ENABLED", self.cb_proactive.isChecked())
            self._env_bool_save_if_changed(
                "PROFANITY_FILTER_ENABLED", self.cb_filter.isChecked())
            self._env_bool_save_if_changed("STT_ENABLED", self.cb_stt.isChecked())
            # 语音识别相关变化（开关 / Key / URL / 模型）：变化才发 !stt 热重启引擎。
            # 空字段 = 回退代码默认（Key 空 = 回退共用 SiliconFlow；URL/模型空 =
            # 用代码默认），比较前先归一化为默认值——否则「留空恒不等于 cfg 里的
            # 默认字符串」会导致每次点保存都误判变化、反复热重启 STT 引擎。
            _defaults = env_helpers._env_defaults()
            _stt_changed = (
                self.cb_stt.isChecked() != bool(self.cfg.STT_ENABLED)
                or (self.ed_stt_key.text().strip() or "")
                != (self.cfg.STT_API_KEY or "")
                or (self.ed_stt_url.text().strip() or _defaults["STT_BASE_URL"])
                != (self.cfg.STT_BASE_URL or "")
                or (self.ed_stt_model.text().strip() or _defaults["STT_MODEL"])
                != (self.cfg.STT_MODEL or ""))
            # Key 留空 = 回退共用 SiliconFlow：等于默认（空）时不写入 .env
            self._env_save_skip_default_if_changed(
                "STT_API_KEY", self.ed_stt_key.text().strip(), "")
            # 语音识别 URL / 模型：默认值不写入 .env（.env 只保留自定义配置）
            self._env_save_skip_default_if_changed(
                "STT_BASE_URL", self.ed_stt_url.text().strip(),
                _defaults["STT_BASE_URL"])
            self._env_save_skip_default_if_changed(
                "STT_MODEL", self.ed_stt_model.text().strip(),
                _defaults["STT_MODEL"])
            self._env_bool_save_if_changed(
                "EMOTION_ACTOR_ENABLED", self.cb_emotion_actor.isChecked())
            self._env_save_if_changed(
                "GPTSOVITS_REF_AUDIO", self.ed_tts_audio.text().strip())
            self._env_save_if_changed(
                "GPTSOVITS_PROMPT_TEXT", self.ed_tts_text.text().strip())
            self._env_save_if_changed(
                "GPTSOVITS_REF_AUDIOS", self.ed_tts_audios.text().strip())
            # B站直播弹幕：值等于代码默认 → 不写入 .env；未修改跳过
            if self.cb_bili_enabled.isChecked() != bool(
                    self._env_base().get("BILI_ENABLED")):
                env_helpers._update_env_skip_default(
                    "BILI_ENABLED",
                    "true" if self.cb_bili_enabled.isChecked() else "false",
                    _defaults["BILI_ENABLED"])
            self._env_save_skip_default_if_changed(
                "BILI_ROOM_ID", self.ed_bili_room.text().strip(),
                _defaults["BILI_ROOM_ID"])
            self._env_save_skip_default_if_changed(
                "BILI_SESSDATA", self.ed_bili_sessdata.text().strip(),
                _defaults["BILI_SESSDATA"])
            self._env_save_skip_default_if_changed(
                "BILI_SERVER_PORT", self.ed_bili_port.text().strip(),
                _defaults["BILI_SERVER_PORT"])
            # 工具总开关变化（!tools 热启停/重启 MCP 服务器并重新合并工具）
            _tools_changed = (
                self.cb_mcp.isChecked() != bool(self.cfg.TOOLS_ENABLED))
            # TTS 参考音频/文本/辅助参考变化：!tts_audio 等热更新会重载参考并打断
            # 当前合成/播放，未变化时不发命令，避免点保存无故打断 TTS 播放
            _tts_changed = (
                self.ed_tts_audio.text().strip()
                != (self.cfg.GPTSOVITS_REF_AUDIO or "")
                or self.ed_tts_text.text().strip()
                != (self.cfg.GPTSOVITS_PROMPT_TEXT or "")
                or self.ed_tts_audios.text().strip()
                != (self.cfg.GPTSOVITS_REF_AUDIOS or ""))
            _saved = True
        except OSError as e:
            console.error(f"保存失败：{e}")
        else:
            console.ok("配置已更新到 .env")
        # 默认待机动作 → .env（PET_IDLE_MOTION，随 !config 热更新立即生效）。
        # 空 = 自动（智能匹配待机）：等于默认值时不写入 .env（.env 只保留自定义配置）。
        # 情绪 → 表情/动作绑定已实时写入 _map_data（拖拽即更新），随映射一并保存。
        try:
            self._env_save_skip_default_if_changed(
                "PET_IDLE_MOTION",
                str(self.combo_idle_motion.currentData() or "").strip(), "")
        except OSError as e:
            console.error(f"保存默认待机动作失败：{e}")
        # 表情/动作绑定映射（与 .env 一起保存，点底部「更新配置」即可热生效）
        self._save_map()
        # —— 所有字段已写盘后才发热更新命令（先落盘后推送，避免主程序读到旧值） ——
        if not _saved:
            return
        # 刷新本进程 cfg：下次保存的「变化检测」（_stt_changed / _emb_changed /
        # _tools_changed）基于最新值，避免同一字段每次保存都被判为「已变化」
        # 而重复热更新（如 !stt 反复重启语音引擎）。
        config.reload_config()
        if self.proc is not None and self.proc.state() == QProcess.ProcessState.Running:
            # 统一热更新命令 !config：重读 .env 并热重建 LLM client / 主动对话 /
            # 内容过滤 / 记忆 / 桌宠窗口与待机 / 情绪映射，全部立即生效，无需重启。
            self.proc.write(b"!config\n")
            self._log("[控制中心] 配置已全部热更新（立即生效，无需重启）\n")
            # Embedding/管家模型：嵌入器与 ButlerAgent 仅在启动时构建，无法热更新
            if _emb_changed:
                self._log("[控制中心] Embedding/管家模型已保存，重启主程序后生效\n")
            # TTS 参考音频/文本/辅助参考：变化才热更新（未变化跳过，避免打断播放）
            if _tts_changed:
                self.proc.write(
                    f"!tts_audio {self.ed_tts_audio.text().strip()}\n".encode("utf-8"))
                self.proc.write(
                    f"!tts_text {self.ed_tts_text.text().strip()}\n".encode("utf-8"))
                self.proc.write(
                    f"!tts_audios {self.ed_tts_audios.text().strip()}\n".encode("utf-8"))
                self._log("[控制中心] TTS 参考音频/文本/辅助参考已热更新（立即生效）\n")
            else:
                self._log("[控制中心] TTS 参考音频未变化，跳过热更新\n")
            # 语音识别开关（!stt）：开关或 Key 变化时热启停/重启 STT 引擎
            if _stt_changed:
                self.proc.write(b"!stt\n")
                self._log("[控制中心] 语音识别配置已热更新（立即生效）\n")
            else:
                self._log("[控制中心] 语音识别配置未变化，跳过热更新\n")
            # 工具总开关（!tools）：变化时热启停/重启 MCP 服务器并重新合并工具，
            # 并同步刷新插件卡片（本地工具行随总开关显示「总开关已关」）
            if _tools_changed:
                self.proc.write(b"!tools\n")
                self._log("[控制中心] 工具配置已热更新（立即生效）\n")
                config.reload_tool_runtime()
                self._fill_plugin_cards()
        # —— 保存确认反馈：按钮短暂切玫红（不弹 toast），随后还原 ——
        self._dim_save_btn()

    def _dim_save_btn(self) -> None:
        """保存成功：按钮文字变「已保存」+ 切换为侧边栏选中项玫红（确认感），
        1.2s 后还原。不用绿色。"""
        self.btn_save_config.setText("💾 已保存")
        self.btn_save_config.setStyleSheet(
            "QPushButton#btn_save_config{background:qlineargradient("
            "x1:0,y1:0,x2:1,y2:0,stop:0 rgba(230,104,122,255),"
            "stop:1 rgba(220,90,105,255));"
            "border:1px solid rgba(180,69,83,220);color:#fcfaf5;"
            "font-size:14px;padding:12px 20px;border-radius:10px;}")
        QTimer.singleShot(1200, self._restore_save_btn)

    def _restore_save_btn(self) -> None:
        """还原「更新配置」按钮文字与样式（回到 .ui 全局 QSS）。"""
        if self._closing:  # 窗口已关闭：控件已销毁，跳过还原
            return
        self.btn_save_config.setText("💾 更新配置")
        self.btn_save_config.setStyleSheet("")
