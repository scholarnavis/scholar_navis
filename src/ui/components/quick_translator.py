import locale
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
                               QTextBrowser, QPushButton, QLabel, QApplication, QComboBox, QCheckBox, QSizeGrip)
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

            from src.core.llm_impl import _TRANSLATION_CACHE
            cache_key = f"{self.target_lang}_{hash(self.text)}"

            if cache_key in _TRANSLATION_CACHE:
                self.sig_token.emit(_TRANSLATION_CACHE[cache_key])
                self.sig_finished.emit()
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

            kwargs = {
                "is_translation": True,
            }
            full_result = ""
            for token in self.llm.stream_chat(messages, **kwargs):
                if self._is_cancelled: break
                full_result += token  # 收集流式 token
                self.sig_token.emit(token)

            if full_result and not self._is_cancelled:
                _TRANSLATION_CACHE[cache_key] = full_result

        except Exception as e:
            if "timeout" in str(e).lower() or "connect" in str(e).lower():
                self.sig_error.emit("Network timeout. Please check your proxy or API connection.")
            else:
                self.sig_error.emit(str(e))
        finally:
            self.sig_finished.emit()


class QuickTranslatorWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(None)

        self.cfg_mgr = ConfigManager()

        self.is_pinned = self.cfg_mgr.user_settings.get("quick_trans_is_pinned", True)

        flags = Qt.Window | Qt.FramelessWindowHint | Qt.Tool
        if self.is_pinned:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(800, 500)
        self.setMinimumSize(400, 300)

        # 为自定义边缘拉伸开启鼠标追踪
        self.setMouseTracking(True)
        self.EDGE_MARGIN = 10
        self._resize_dir = None
        self.start_geometry = None
        self.worker_thread = None
        self.worker = None
        self.drag_pos = None

        self._setup_ui()
        self._update_pin_ui()
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
        self.main_frame.setMouseTracking(True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.addWidget(self.main_frame)
        frame_layout = QVBoxLayout(self.main_frame)

        # --- Top Drag Bar ---
        top_bar = QHBoxLayout()
        title = QLabel("Scholar Translator")
        title.setStyleSheet("color: #05B8CC; font-weight: bold; border: none;")

        self.btn_pin = QPushButton()
        self.btn_pin.setFixedSize(24, 24)
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
        # 1. 第一行：模型选择器
        model_bar = QHBoxLayout()
        self.lbl_trans_icon = QLabel()
        self.lbl_trans_icon.setStyleSheet("background: transparent; border: none;")
        self.lbl_trans_icon.setFixedWidth(24)
        model_bar.addWidget(self.lbl_trans_icon)

        self.model_selector = ModelSelectorWidget(label_text="Translator:", config_key="quick_trans_llm_id",enable_vision=False,
                                                  model_key="quick_trans_model_name")
        model_bar.addWidget(self.model_selector, stretch=1)
        frame_layout.addLayout(model_bar)

        # 2. 第二行：语言配置 (From -> To)
        lang_bar = QHBoxLayout()
        lang_bar.addSpacing(30)

        langs = ["Auto Detect", "English", "Chinese", "Japanese", "French", "German"]
        sys_lang = get_system_language()

        saved_src = self.cfg_mgr.user_settings.get("trans_source_lang", "Auto Detect")
        saved_tgt = self.cfg_mgr.user_settings.get("trans_target_lang", sys_lang)

        self.combo_src = QComboBox()
        self.combo_src.addItems(langs)
        self.combo_src.setCurrentText(saved_src)
        self.combo_src.setStyleSheet(
            "background: #1e1e1e; color: white; border: 1px solid #444; border-radius: 4px; padding: 4px;")

        self.combo_tgt = QComboBox()
        self.combo_tgt.addItems([l for l in langs if l != "Auto Detect"] + ["Academic Polish"])
        self.combo_tgt.setCurrentText(saved_tgt)
        self.combo_tgt.setStyleSheet(
            "background: #1e1e1e; color: white; border: 1px solid #444; border-radius: 4px; padding: 4px;")

        self.combo_src.currentTextChanged.connect(lambda t: self._save_lang("trans_source_lang", t))
        self.combo_tgt.currentTextChanged.connect(lambda t: self._save_lang("trans_target_lang", t))

        lang_bar.addWidget(QLabel("From:"))
        lang_bar.addWidget(self.combo_src, stretch=1)
        lang_bar.addWidget(QLabel("To:"))
        lang_bar.addWidget(self.combo_tgt, stretch=1)

        frame_layout.addLayout(lang_bar)

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
        self.btn_copy = QPushButton("Copy")

        saved_md = self.cfg_mgr.user_settings.get("quick_trans_markdown", True)
        self.chk_markdown = QCheckBox("Markdown Render")
        self.chk_markdown.setChecked(saved_md)
        self.chk_markdown.toggled.connect(self._re_render_output)
        self.chk_markdown.toggled.connect(self._save_markdown_setting)

        self.btn_trans.setStyleSheet(
            "background-color: #007acc; color: white; border-radius: 6px; padding: 6px; font-weight: bold;")
        self.btn_stop.setStyleSheet(
            "background-color: #c42b1c; color: white; border-radius: 6px; padding: 6px; font-weight: bold;")
        self.btn_stop.setVisible(False)
        self.btn_clear.setStyleSheet("background-color: #333; color: white; border-radius: 6px; padding: 6px;")

        self.btn_trans.clicked.connect(self._start_translation)
        self.btn_stop.clicked.connect(self._stop_translation)
        self.btn_clear.clicked.connect(self._clear_all)
        self.btn_copy.clicked.connect(self._copy_result)

        ctrl_bar.addWidget(self.btn_trans)
        ctrl_bar.addWidget(self.btn_stop)
        ctrl_bar.addWidget(self.btn_copy)
        ctrl_bar.addWidget(self.btn_clear)
        frame_layout.addLayout(ctrl_bar)
        ctrl_bar.addWidget(self.chk_markdown)

        self.output_box = QTextBrowser()
        self.output_box.setStyleSheet(
            "background-color: #1e1e1e; color: #fff; border: 1px solid #333; border-radius: 6px; padding: 10px; font-size: 14px;")
        frame_layout.addWidget(self.output_box)

        grip_layout = QHBoxLayout()
        grip_layout.setContentsMargins(0, 0, 0, 0)
        grip_layout.addStretch()
        self.size_grip = QSizeGrip(self.main_frame)
        self.size_grip.setFixedSize(16, 16)
        self.size_grip.setStyleSheet("background: transparent;")
        grip_layout.addWidget(self.size_grip)
        frame_layout.addLayout(grip_layout)

        self.current_out_text = ""

    def _save_markdown_setting(self, checked):
        """保存 Markdown 复选框的状态"""
        self.cfg_mgr.user_settings["quick_trans_markdown"] = checked
        self.cfg_mgr.save_settings()

    def _update_pin_ui(self):
        """仅更新置顶按钮的图标和 ToolTip"""
        if self.is_pinned:
            self.btn_pin.setIcon(ThemeManager().icon("keep_off", "accent"))
            self.btn_pin.setToolTip("Unpin Window")
            self.btn_pin.setStyleSheet(
                "QPushButton { background: transparent; color: #05B8CC; border: none; font-size: 15px; } QPushButton:hover { color: #fff; }")
        else:
            self.btn_pin.setIcon(ThemeManager().icon("keep", "text_muted"))
            self.btn_pin.setToolTip("Pin to Top")
            self.btn_pin.setStyleSheet(
                "QPushButton { background: transparent; color: #888; border: none; font-size: 15px; opacity: 0.6; } QPushButton:hover { color: #ccc; }")



    def _stop_translation(self):
        if getattr(self, 'worker_thread', None):
            try:
                if self.worker_thread.isRunning():
                    if getattr(self, 'worker', None):
                        self.worker.cancel()
                        try:
                            self.worker.sig_token.disconnect()
                            self.worker.sig_finished.disconnect()
                            self.worker.sig_error.disconnect()
                        except Exception:
                            pass

                    if not hasattr(self, '_orphaned_threads'): self._orphaned_threads = []
                    old_t, old_w = self.worker_thread, self.worker
                    old_t.quit()
                    self._orphaned_threads.append((old_t, old_w))
                    old_t.finished.connect(
                        lambda t=old_t, w=old_w: self._orphaned_threads.remove((t, w)) if (t, w) in getattr(self,
                                                                                                            '_orphaned_threads',
                                                                                                            []) else None)
            except RuntimeError:
                pass

            self.worker_thread = None
            self.worker = None

            self.output_box.append("<br><span style='color:#e6a23c;'><b>[Stopped by User]</b></span>")
            self._on_translation_finished()


    def _re_render_output(self, checked):
        if not hasattr(self, 'current_out_text') or not self.current_out_text:
            return

        clean_text = TextFormatter.hide_think_tags(self.current_out_text, for_display=True)
        if checked:
            html_str = TextFormatter.markdown_to_html(clean_text)
            self.output_box.setHtml(html_str)
        else:
            self.output_box.setHtml(clean_text.replace('\n', '<br>'))
        self.output_box.verticalScrollBar().setValue(self.output_box.verticalScrollBar().maximum())

    def _toggle_pin(self):
        self.is_pinned = not self.is_pinned
        self.cfg_mgr.user_settings["quick_trans_is_pinned"] = self.is_pinned
        self.cfg_mgr.save_settings()

        flags = self.windowFlags()
        if self.is_pinned:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint

        self.setWindowFlags(flags)
        self._update_pin_ui()
        self.show()

    def _get_resize_dir(self, pos):
        """判断鼠标位置属于哪个边缘"""
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        m = self.EDGE_MARGIN
        dir_ = ""
        if y < m:
            dir_ += "top"
        elif y > h - m:
            dir_ += "bottom"
        if x < m:
            dir_ += "left"
        elif x > w - m:
            dir_ += "right"
        return dir_

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._resize_dir = self._get_resize_dir(event.pos())
            if self._resize_dir:
                self.drag_pos = event.globalPos()
                self.start_geometry = self.geometry()
            else:
                self.drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()




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
        if getattr(self, 'worker_thread', None) and self.worker_thread.isRunning():
            self._stop_translation()

        self.input_box.clear()
        self.output_box.clear()

    def _start_translation(self):
        text = self.input_box.toPlainText().strip()
        if not text: return

        if getattr(self, 'worker_thread', None) is not None:
            try:
                if getattr(self, 'worker', None):
                    self.worker.cancel()
                    try:
                        self.worker.sig_token.disconnect()
                        self.worker.sig_finished.disconnect()
                        self.worker.sig_error.disconnect()
                    except Exception:
                        pass

                if self.worker_thread.isRunning():
                    if not hasattr(self, '_orphaned_threads'): self._orphaned_threads = []
                    old_t, old_w = self.worker_thread, self.worker
                    old_t.quit()
                    self._orphaned_threads.append((old_t, old_w))
                    old_t.finished.connect(
                        lambda t=old_t, w=old_w: self._orphaned_threads.remove((t, w)) if (t, w) in getattr(self,
                                                                                                            '_orphaned_threads',
                                                                                                            []) else None)
            except RuntimeError:
                pass

            self.worker_thread = None
            self.worker = None

        self.output_box.clear()
        self.output_box.setHtml("<span style='color:#05B8CC;'><i>AI is preparing...</i></span>")

        self.btn_trans.setVisible(False)
        self.btn_stop.setVisible(True)

        trans_config = self.model_selector.get_current_config()
        if trans_config:
            trans_config = trans_config.copy()

            from PySide6.QtWidgets import QComboBox
            combos = self.model_selector.findChildren(QComboBox)
            if len(combos) >= 2:
                raw_ui_trans = combos[1].currentText()
            else:
                raw_ui_trans = self.cfg_mgr.user_settings.get("quick_trans_model_name", "")

            ui_selected_trans = raw_ui_trans

            # 清理后缀，防止发给 API 导致 404
            for suffix in [" (⚙️ Custom)", " (🚫 Closed)"]:
                if ui_selected_trans.endswith(suffix):
                    ui_selected_trans = ui_selected_trans[:-len(suffix)]

            if ui_selected_trans:
                trans_config["model_name"] = ui_selected_trans
                self.cfg_mgr.user_settings["quick_trans_model_name"] = raw_ui_trans
                self.cfg_mgr.save_settings()

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
        self.btn_clear.setStyleSheet(
            f"background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border-radius: 6px; padding: 6px;")

        self.btn_copy.setText(" Copy")
        self.btn_copy.setIcon(tm.icon("copy", "text_main"))
        self.btn_copy.setStyleSheet(
            f"background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; border-radius: 6px; padding: 6px;")


    def _copy_result(self):
        if not self.current_out_text: return
        clean_text = TextFormatter.hide_think_tags(self.current_out_text, for_display=False)
        import re
        clean_text = re.sub(r"<[^>]+>", "", clean_text).strip()
        QApplication.clipboard().setText(clean_text)
        from src.ui.components.toast import ToastManager
        ToastManager().show("Translated text copied to clipboard.", "success")



    def _on_token(self, token):
        self.current_out_text += token
        clean_text = TextFormatter.hide_think_tags(self.current_out_text, for_display=True)

        if getattr(self, 'chk_markdown', None) and self.chk_markdown.isChecked():
            html = TextFormatter.markdown_to_html(clean_text)
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
        pos = event.pos()

        # 1. 鼠标悬停边缘时改变光标状态 (未按下按钮时)
        if not event.buttons() & Qt.LeftButton:
            dir_ = self._get_resize_dir(pos)
            if dir_ in ["topleft", "bottomright"]:
                self.setCursor(Qt.SizeFDiagCursor)
            elif dir_ in ["topright", "bottomleft"]:
                self.setCursor(Qt.SizeBDiagCursor)
            elif dir_ in ["left", "right"]:
                self.setCursor(Qt.SizeHorCursor)
            elif dir_ in ["top", "bottom"]:
                self.setCursor(Qt.SizeVerCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

        # 2. 鼠标拖动处理
        if event.buttons() == Qt.LeftButton and self.drag_pos is not None:
            if getattr(self, '_resize_dir', None):
                # 拉伸窗口逻辑
                delta = event.globalPos() - self.drag_pos
                rect = self.start_geometry

                new_left = rect.left()
                new_top = rect.top()
                new_right = rect.right()
                new_bottom = rect.bottom()

                if "left" in self._resize_dir: new_left += delta.x()
                if "right" in self._resize_dir: new_right += delta.x()
                if "top" in self._resize_dir: new_top += delta.y()
                if "bottom" in self._resize_dir: new_bottom += delta.y()

                # 约束最小窗口限制
                if new_right - new_left < self.minimumWidth():
                    if "left" in self._resize_dir:
                        new_left = new_right - self.minimumWidth()
                    else:
                        new_right = new_left + self.minimumWidth()
                if new_bottom - new_top < self.minimumHeight():
                    if "top" in self._resize_dir:
                        new_top = new_bottom - self.minimumHeight()
                    else:
                        new_bottom = new_top + self.minimumHeight()

                self.setGeometry(new_left, new_top, new_right - new_left, new_bottom - new_top)
            else:
                # 移动窗口逻辑
                self.move(event.globalPos() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        self._resize_dir = None
        self.setCursor(Qt.ArrowCursor)


    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide_with_fade()
        else:
            super().keyPressEvent(event)