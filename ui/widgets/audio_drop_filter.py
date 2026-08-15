"""TTS 参考音频输入框拖拽过滤器：接受本地音频文件（可多选）。"""

from PySide6.QtCore import QEvent, QMimeData, QObject


class _AudioDropFilter(QObject):
    """TTS 参考音频输入框拖拽过滤器：接受本地音频文件（可多选）。

    主参考（单条，append=False）拖入 = 替换当前值；
    辅助参考（多条，append=True）拖入 = 以 | 连接追加、重复去重。
    与 src/tts/engine.py 的多参考解析（| 分隔）保持一致。
    """

    _AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".amr")

    def __init__(self, line_edit, append: bool = True) -> None:
        super().__init__(line_edit)
        self._edit = line_edit
        self._append = append

    def _dropped_audios(self, mime: QMimeData) -> list:
        """从拖拽数据里挑出本地音频文件路径（忽略目录/非音频）。"""
        if not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(self._AUDIO_EXTS):
                paths.append(p)
        return paths

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if self._dropped_audios(event.mimeData()):
                event.acceptProposedAction()
                return True
            return False
        if t == QEvent.Type.Drop:
            paths = self._dropped_audios(event.mimeData())
            if not paths:
                return False
            if self._append:
                parts = [p.strip() for p in self._edit.text().split("|") if p.strip()]
                for p in paths:
                    if p not in parts:
                        parts.append(p)
                self._edit.setText("|".join(parts))
            else:
                # 单条参考：拖入替换当前值（取第一个文件）
                self._edit.setText(paths[0])
            event.acceptProposedAction()
            return True
        return False
