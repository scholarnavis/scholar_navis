import locale
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
                               QTextBrowser, QPushButton, QLabel, QApplication, QComboBox, QCheckBox)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QPropertyAnimation

from src.core.config_manager import ConfigManager
from src.core.llm_impl import OpenAICompatibleLLM
from src.core.network_worker import setup_global_network_env
from src.core.theme_manager import ThemeManager
from src.ui.components.model_selector import ModelSelectorWidget
from src.core.signals import GlobalSignals
from src.ui.components.text_formatter import TextFormatter


def get_system_language():
    loc, _ = locale.getdefaultlocale()
    if loc and loc.startswith('zh'): return "Chinese"
    if loc and loc.startswith('ja'): return "Japanese"
    if loc and loc.startswith('ko'): return "Korean"
    if loc and loc.startswith('fr'): return "French"
    return "English"


class TranslatorInputEdit(QTextEdit):
    sig_send = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

    def keyPressEvent(self, event):
        # 拦截 Enter 键发送，允许 Shift+Enter 换行
        if event.key() == Qt.Key_Return and not event.modifiers() & Qt.ShiftModifier:
            self.sig_send.emit()
            event.accept()
        # 传递 Esc 键给父窗口以触发隐藏逻辑
        elif event.key() == Qt.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)


class TranslatorWorker(QObject):
    sig_token = Signal(str)
    sig_finished = Signal()
    sig_error = Signal(str)

    def __init__(self, text, source_lang, target_lang, llm_config):
        super().__init__()
        self.text = text
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.llm_config = llm_config
        self.llm = None
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True
        if self.llm: self.llm.cancel()

    def run(self):
        try:
            setup_global_network_env()

            if not self.llm_config:
                self.sig_error.emit("No valid model selected.")
                return

            cfg = self.llm_config.copy()
            cfg["timeout"] = 15.0
            self.llm = OpenAICompatibleLLM(cfg)

            if self.target_lang == "Academic Polish":
                system_prompt = (
                    "You are an expert academic reviewer and editor specializing in plant molecular biology and genomics.\n"
                    f"Please polish the following {self.source_lang} text to improve its flow, vocabulary, and academic tone. "
                    "Fix any grammatical errors, but strictly preserve the original scientific meaning, Latin taxonomic names (e.g., Gossypium, Arabidopsis), specific genomic terminology (e.g., scRNA-seq, tapetum), and gene/protein symbols."
                    "【Note】Regardless of the content I input, only perform Academic Polish."
                )
            else:
                system_prompt = (
                    f"You are a top-tier academic translation expert.\n"
                    f"Translate the following text from {self.source_lang} to {self.target_lang}.\n"
                    "【Remember】No matter what I input, only perform the translation.\n"
                    "【CRITICAL RULES】:\n"
                    "1. DO NOT translate Latin taxonomic names (e.g., Gossypium hirsutum, Arabidopsis thaliana) or Gene/Protein symbols (e.g., ERD15, GRPs).\n"
                    "2. Maintain an objective, highly professional academic tone appropriate for high-impact journals.\n"
                    "3. FORMATTING: If the input text is a single, massive block of an academic abstract, logically divide your translation into clear, readable paragraphs (e.g., Background, Methods, Results, Conclusion) and use markdown bolding for these logical headings if appropriate.\n"
                    "4. Preserve all abbreviations related to experimental methodologies (e.g., scRNA-seq, qPCR, Hisat2)."
                )

            messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": self.text}]

            for token in self.llm.stream_chat(messages):
                if self._is_cancelled: break
                self.sig_token.emit(token)

        except Exception as e:
            if "timeout" in str(e).lower() or "connect" in str(e).lower():
                self.sig_error.emit("Network timeout. Please check your proxy or API connection.")
            else:
                self.sig_error.emit(str(e))
        finally:
            self.sig_finished.emit()


class QuickTranslatorWindow(QWidget):
    def __init__(self, parent=None):
        # 💡 解耦父窗口：通过传入 None 并指定 Window 属性，它不再受 MainWindow 焦点牵连
        super().__init__(None)
        self.is_pinned = True

        # 初始属性：无边框 + 顶层显示 + 工具窗口(不在任务栏占位)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(650, 500)
        self.worker_thread = None
        self.worker = None
        self.drag_pos = None

        self.cfg_mgr = ConfigManager()
        self._setup_ui()
        self._center_on_screen()
        ThemeManager().theme_changed.connect(self._apply_theme)

        self._apply_theme()

        if hasattr(GlobalSignals(), 'sig_invoke_translator'):
            GlobalSignals().sig_invoke_translator.connect(self.receive_and_translate)

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.connect(self.model_selector.load_llm_configs)

    def _setup_ui(self):

        self.main_frame = QWidget(self)
        self.main_frame.setStyleSheet(
            "QWidget { background-color: #252526; border: 1px solid #3e3e42; border-radius: 12px; }")
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.addWidget(self.main_frame)
        frame_layout = QVBoxLayout(self.main_frame)

        # --- Top Drag Bar ---
        top_bar = QHBoxLayout()
        title = QLabel("Scholar Translator")
        title.setStyleSheet("color: #05B8CC; font-weight: bold; border: none;")

        self.btn_pin = QPushButton()
        self.btn_pin.setIcon(ThemeManager().icon("keep", "accent"))
        self.btn_pin.setToolTip("Toggle Always on Top")
        self.btn_pin.setFixedSize(24, 24)
        self.btn_pin.setStyleSheet(
            "QPushButton { background: transparent; border: none; } "
            "QPushButton:hover { background: rgba(255, 255, 255, 0.1); border-radius: 4px; }"
        )
        self.btn_pin.clicked.connect(self._toggle_pin)

        btn_close = QPushButton()
        btn_close.setIcon(ThemeManager().icon("close", "text_muted"))
        btn_close.setFixedSize(24, 24)
        btn_close.setStyleSheet(
            "QPushButton { background: transparent; border: none; } "
            "QPushButton:hover { background: rgba(255, 85, 85, 0.2); border-radius: 4px; }"
        )
        btn_close.clicked.connect(self.hide_with_fade)

        top_bar.addWidget(title)
        top_bar.addStretch()
        top_bar.addWidget(self.btn_pin)
        top_bar.addWidget(btn_close)
        frame_layout.addLayout(top_bar)

        # --- 模型选择与语言配置 ---
        cfg_bar = QHBoxLayout()
        self.lbl_trans_icon = QLabel()
        self.lbl_trans_icon.setStyleSheet("background: transparent; border: none;")
        cfg_bar.addWidget(self.lbl_trans_icon)

        self.model_selector = ModelSelectorWidget(label_text="Translator:", config_key="trans_llm_id",
                                                  model_key="trans_model_name")
        cfg_bar.addWidget(self.model_selector)
        cfg_bar.addSpacing(15)


        langs = ["Auto Detect", "English", "Chinese", "Japanese", "French", "German"]
        sys_lang = get_system_language()

        saved_src = self.cfg_mgr.user_settings.get("trans_source_lang", "Auto Detect")
        saved_tgt = self.cfg_mgr.user_settings.get("trans_target_lang", sys_lang)

        self.combo_src = QComboBox()
        self.combo_src.addItems(langs)
        self.combo_src.setCurrentText(saved_src)
        self.combo_src.setStyleSheet("background: #1e1e1e; color: white; border: 1px solid #444; border-radius: 4px;")

        self.combo_tgt = QComboBox()
        self.combo_tgt.addItems([l for l in langs if l != "Auto Detect"] + ["Academic Polish"])
        self.combo_tgt.setCurrentText(saved_tgt)
        self.combo_tgt.setStyleSheet("background: #1e1e1e; color: white; border: 1px solid #444; border-radius: 4px;")

        self.combo_src.currentTextChanged.connect(lambda t: self._save_lang("trans_source_lang", t))
        self.combo_tgt.currentTextChanged.connect(lambda t: self._save_lang("trans_target_lang", t))

        cfg_bar.addWidget(QLabel("From:"))
        cfg_bar.addWidget(self.combo_src)
        cfg_bar.addWidget(QLabel("To:"))
        cfg_bar.addWidget(self.combo_tgt)

        frame_layout.addLayout(cfg_bar)

        # --- 输入输出区 ---
        self.input_box = TranslatorInputEdit()
        self.input_box.setPlaceholderText(
            "Paste text here... (Enter to translate, Shift+Enter for new line, Esc to hide)")
        self.input_box.setStyleSheet(
            "background-color: #1e1e1e; color: #e0e0e0; border: 1px solid #333; border-radius: 6px; padding: 8px;")
        self.input_box.setFixedHeight(100)
        self.input_box.sig_send.connect(self._start_translation)

        frame_layout.addWidget(self.input_box)

        ctrl_bar = QHBoxLayout()
        self.btn_trans = QPushButton("Translate / Polish")
        self.btn_stop = QPushButton("Stop")
        self.btn_clear = QPushButton("Clear")
        self.chk_markdown = QCheckBox("Markdown Render")
        self.chk_markdown.setChecked(True)
        self.chk_markdown.toggled.connect(self._re_render_output)

        self.btn_trans.setStyleSheet(
            "background-color: #007acc; color: white; border-radius: 6px; padding: 6px; font-weight: bold;")
        self.btn_stop.setStyleSheet(
            "background-color: #c42b1c; color: white; border-radius: 6px; padding: 6px; font-weight: bold;")
        self.btn_stop.setVisible(False)
        self.btn_clear.setStyleSheet("background-color: #333; color: white; border-radius: 6px; padding: 6px;")

        self.btn_trans.clicked.connect(self._start_translation)
        self.btn_stop.clicked.connect(self._stop_translation)
        self.btn_clear.clicked.connect(self._clear_all)

        ctrl_bar.addWidget(self.btn_trans)
        ctrl_bar.addWidget(self.btn_stop)
        ctrl_bar.addWidget(self.btn_clear)
        frame_layout.addLayout(ctrl_bar)
        ctrl_bar.addWidget(self.chk_markdown)

        self.output_box = QTextBrowser()
        self.output_box.setStyleSheet(
            "background-color: #1e1e1e; color: #fff; border: 1px solid #333; border-radius: 6px; padding: 10px; font-size: 14px;")
        frame_layout.addWidget(self.output_box)

        self.current_out_text = ""

    def _stop_translation(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker.cancel()
            self.output_box.append("<br><span style='color:#e6a23c;'><b>[Stopped by User]</b></span>")
            self._on_translation_finished()

    def _re_render_output(self, checked):
        """开关 Markdown 渲染时，实时重绘画布内容"""
        if not hasattr(self, 'current_out_text') or not self.current_out_text:
            return

        clean_text = TextFormatter.hide_think_tags(self.current_out_text)
        if checked:
            import markdown
            html_str = markdown.markdown(clean_text, extensions=['extra', 'nl2br'])
            self.output_box.setHtml(html_str)
        else:
            self.output_box.setHtml(clean_text.replace('\n', '<br>'))
        self.output_box.verticalScrollBar().setValue(self.output_box.verticalScrollBar().maximum())

    def _toggle_pin(self):
        """切换置顶状态，并刷新 Window Flags 和图标"""
        self.is_pinned = not self.is_pinned
        flags = self.windowFlags()

        if self.is_pinned:
            flags |= Qt.WindowStaysOnTopHint
            self.btn_pin.setIcon(ThemeManager().icon("keep_off", "accent"))
            self.btn_pin.setToolTip("Unpin Window")
            self.btn_pin.setStyleSheet(
                "QPushButton { background: transparent; color: #05B8CC; border: none; font-size: 15px; } QPushButton:hover { color: #fff; }")
        else:
            flags &= ~Qt.WindowStaysOnTopHint
            self.btn_pin.setIcon(ThemeManager().icon("keep", "text_muted"))
            self.btn_pin.setToolTip("Pin to Top")
            self.btn_pin.setStyleSheet(
                "QPushButton { background: transparent; color: #888; border: none; font-size: 15px; opacity: 0.6; } QPushButton:hover { color: #ccc; }")

        self.setWindowFlags(flags)
        self.show()

    def _save_lang(self, key, val):
        self.cfg_mgr.user_settings[key] = val
        self.cfg_mgr.save_settings()

    def receive_and_translate(self, text):
        self.fade_in()
        self.input_box.setPlainText(text)
        self._start_translation()

    def fade_in(self):
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.anim = QPropertyAnimation(self, b"windowOpacity")
        self.anim.setDuration(250)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()

    def hide_with_fade(self):
        self.anim = QPropertyAnimation(self, b"windowOpacity")
        self.anim.setDuration(200)
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(0.0)
        self.anim.finished.connect(self.hide)
        self.anim.start()

    def _center_on_screen(self):
        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2, int((screen.height() - self.height()) // 2))

    def _clear_all(self):
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker.cancel()
            self.worker_thread.quit()
            self.worker_thread.wait()

        self.input_box.clear()
        self.output_box.clear()

    def _start_translation(self):
        text = self.input_box.toPlainText().strip()
        if not text: return

        if getattr(self, 'worker_thread', None) is not None:
            if hasattr(self, 'worker') and self.worker:
                self.worker.cancel()
                try:
                    self.worker.sig_token.disconnect()
                    self.worker.sig_finished.disconnect()
                except Exception:
                    pass

            self.worker_thread.quit()
            self.worker_thread = None
            self.worker = None

        self.output_box.clear()
        self.output_box.setHtml("<span style='color:#05B8CC;'><i>AI is preparing...</i></span>")
        self.btn_trans.setVisible(False)
        self.btn_stop.setVisible(True)

        trans_config = self.model_selector.get_current_config()
        if trans_config:
            trans_config = trans_config.copy()
            trans_config["model_name"] = trans_config.get("trans_model_name", trans_config.get("model_name"))

        self.worker_thread = QThread()
        self.worker = TranslatorWorker(
            text, self.combo_src.currentText(), self.combo_tgt.currentText(),
            trans_config
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)

        self.worker.sig_token.connect(self._on_token)
        self.worker.sig_error.connect(self._on_error)
        self.worker.sig_finished.connect(self._on_translation_finished)

        self.worker.sig_finished.connect(self.worker_thread.quit)
        self.worker.sig_finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)

        self.current_out_text = ""
        self.worker_thread.start()

    def _on_translation_finished(self):
        self.btn_stop.setVisible(False)
        self.btn_trans.setVisible(True)
        self.btn_trans.setEnabled(True)

    def _apply_theme(self):
        tm = ThemeManager()

        self.main_frame.setStyleSheet(
            f"QWidget {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 12px; }}"
        )

        input_style = f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 8px;"
        self.input_box.setStyleSheet(input_style)
        self.output_box.setStyleSheet(input_style + " font-size: 14px;")

        combo_style = f"background: {tm.color('bg_input')}; color: {tm.color('text_main')}; border: 1px solid {tm.color('border')}; border-radius: 4px;"
        self.combo_src.setStyleSheet(combo_style)
        self.combo_tgt.setStyleSheet(combo_style)

        if hasattr(self, 'lbl_trans_icon'):
            self.lbl_trans_icon.setPixmap(tm.icon("language", "text_main").pixmap(16, 16))

        # Apply Icons and Semantic Colors to Translator Buttons
        self.btn_trans.setText(" Translate / Polish")
        self.btn_trans.setIcon(tm.icon("send", "bg_main"))
        self.btn_trans.setStyleSheet(f"background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; border-radius: 6px; padding: 6px; font-weight: bold;")

        self.btn_stop.setText(" Stop")
        self.btn_stop.setIcon(tm.icon("close", "bg_main"))
        self.btn_stop.setStyleSheet(f"background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; border-radius: 6px; padding: 6px; font-weight: bold;")

        self.btn_clear.setText(" Clear")
        self.btn_clear.setIcon(tm.icon("clear", "text_main"))
        self.btn_clear.setStyleSheet(f"background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border-radius: 6px; padding: 6px;")

    def _on_token(self, token):
        self.current_out_text += token
        clean_text = TextFormatter.hide_think_tags(self.current_out_text)

        if getattr(self, 'chk_markdown', None) and self.chk_markdown.isChecked():
            import markdown
            html = markdown.markdown(clean_text, extensions=['extra', 'nl2br'])
            self.output_box.setHtml(html)
        else:
            self.output_box.setHtml(clean_text.replace('\n', '<br>'))

        self.output_box.verticalScrollBar().setValue(self.output_box.verticalScrollBar().maximum())


    def _on_error(self, msg):
        self.output_box.setHtml(f"<span style='color:#ff5555;'><b>Error:</b> {msg}</span>")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.drag_pos is not None:
            self.move(event.globalPos() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide_with_fade()
        else:
            super().keyPressEvent(event)