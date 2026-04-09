import ctypes
import locale
import logging
import re
import sys

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
                               QTextBrowser, QPushButton, QLabel, QApplication,
                               QComboBox, QCheckBox, QSizeGrip)
from PySide6.QtCore import Qt, QPropertyAnimation, QTimer, Signal, QSettings

from src.core.config_manager import ConfigManager
from src.core.theme_manager import ThemeManager
from src.ui.components.model_selector import ModelSelectorWidget
from src.core.signals import GlobalSignals
from src.ui.components.text_formatter import TextFormatter
from src.task.quick_translator_task import TranslatorTaskManager


def get_system_language():
    """获取系统语言"""
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
        if event.key() == Qt.Key_Return and not event.modifiers() & Qt.ShiftModifier:
            self.sig_send.emit()
            event.accept()
        elif event.key() == Qt.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)


class QuickTranslatorWindow(QWidget):
    """快捷翻译窗口"""

    def __init__(self, parent=None):
        super().__init__(None)

        self.cfg_mgr = ConfigManager()
        self.logger = logging.getLogger("QuickTranslator")

        self.settings = QSettings("ScholarNavis", "QuickTranslator")
        pinned_val = self.settings.value("is_pinned", True)
        self.is_pinned = pinned_val if isinstance(pinned_val, bool) else str(pinned_val).lower() == 'true'

        flags = Qt.Window | Qt.FramelessWindowHint
        if self.is_pinned:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(800, 500)
        self.setMinimumSize(400, 300)

        # 鼠标追踪和拉伸
        self.setMouseTracking(True)
        self.EDGE_MARGIN = 10
        self._resize_dir = None
        self.start_geometry = None
        self.drag_pos = None

        # 翻译任务管理器
        self.translator_manager = TranslatorTaskManager()
        self._setup_translator_callbacks()

        # UI相关
        self.current_out_text = ""

        self._is_render_dirty = False
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._throttled_render)

        # 设置UI
        self._setup_ui()
        self._update_pin_ui()

        # 恢复窗口大小与位置
        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        else:
            self._center_on_screen()

        # 主题和配置
        ThemeManager().theme_changed.connect(self._apply_theme)
        self._apply_theme()
        self.reload_and_restore_configs()

        # 全局信号连接
        if hasattr(GlobalSignals(), 'sig_invoke_translator'):
            GlobalSignals().sig_invoke_translator.connect(self.receive_and_translate)

        if hasattr(GlobalSignals(), 'llm_config_changed'):
            GlobalSignals().llm_config_changed.connect(self.model_selector.load_llm_configs)

    def _setup_translator_callbacks(self):
        """设置翻译任务管理器的回调"""
        self.translator_manager.set_callbacks(
            token_callback=self._on_token,
            finished_callback=self._on_translation_finished,
            error_callback=self._on_error
        )

    def reload_and_restore_configs(self):
        self.model_selector.load_llm_configs()


    def _setup_ui(self):
        """设置UI布局"""
        self.main_frame = QWidget(self)
        self.main_frame.setStyleSheet(
            "QWidget { background-color: #252526; border: 1px solid #3e3e42; border-radius: 12px; }")
        self.main_frame.setMouseTracking(True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.addWidget(self.main_frame)
        frame_layout = QVBoxLayout(self.main_frame)

        # --- 顶部拖动栏 ---
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
        model_bar = QHBoxLayout()
        self.lbl_trans_icon = QLabel()
        self.lbl_trans_icon.setStyleSheet("background: transparent; border: none;")
        self.lbl_trans_icon.setFixedWidth(24)
        model_bar.addWidget(self.lbl_trans_icon)

        self.model_selector = ModelSelectorWidget(
            label_text="Translator:",
            config_key="quick_trans_llm_id",
            enable_vision=False,
            model_key="quick_trans_model_name"
        )
        model_bar.addWidget(self.model_selector, stretch=1)
        frame_layout.addLayout(model_bar)

        # 语言配置
        lang_bar = QHBoxLayout()
        lang_bar.addSpacing(30)

        langs = ["Auto Detect", "English", "Simplified Chinese", "Traditional Chinese", "Japanese", "French", "German",
                 "Russian"]
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

        # 控制按钮栏
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
        ctrl_bar.addWidget(self.chk_markdown)
        frame_layout.addLayout(ctrl_bar)

        # 输出框
        self.output_box = QTextBrowser()
        self.output_box.setStyleSheet(
            "background-color: #1e1e1e; color: #fff; border: 1px solid #333; border-radius: 6px; padding: 10px; font-size: 14px;")
        frame_layout.addWidget(self.output_box)

        # 右下角拉伸手柄
        grip_layout = QHBoxLayout()
        grip_layout.setContentsMargins(0, 0, 0, 0)
        grip_layout.addStretch()
        self.size_grip = QSizeGrip(self.main_frame)
        self.size_grip.setFixedSize(16, 16)
        self.size_grip.setStyleSheet("background: transparent;")
        grip_layout.addWidget(self.size_grip)
        frame_layout.addLayout(grip_layout)

        # 定时器
        self.copy_timer = QTimer(self)
        self.copy_timer.setSingleShot(True)
        self.copy_timer.timeout.connect(self._reset_copy_btn)

        self.clear_timer = QTimer(self)
        self.clear_timer.setSingleShot(True)
        self.clear_timer.timeout.connect(self._reset_clear_btn)


    def _start_translation(self):
        """开始翻译"""
        text = self.input_box.toPlainText().strip()
        if not text:
            return

        # 取消现有任务
        if self.translator_manager.is_running():
            self.translator_manager.cancel_translation()

        self.output_box.clear()
        self.output_box.setHtml("<span style='color:#05B8CC;'><i>AI is preparing...</i></span>")

        self.btn_trans.setVisible(False)
        self.btn_stop.setVisible(True)

        trans_config = self.model_selector.get_current_config()
        if not trans_config:
            self._on_error("Translation provider disabled or not selected.")
            return

        # 启动翻译任务
        self.current_out_text = ""
        self.translator_manager.start_translation(
            text=text,
            source_lang=self.combo_src.currentText(),
            target_lang=self.combo_tgt.currentText(),
            llm_config=trans_config
        )

    def _stop_translation(self):
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("Stopping...")

        if self.translator_manager.is_running():
            self.translator_manager.cancel_translation()

        if hasattr(self, '_render_timer'):
            self._render_timer.stop()

    def _on_token(self, token: str):
        """处理接收到的token"""
        self.current_out_text += token
        self._is_render_dirty = True

        # 如果定时器没跑，就启动它
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _throttled_render(self):
        """实际执行渲染的函数，由定时器触发"""
        if not self._is_render_dirty:
            self._render_timer.stop()
            return

        clean_text = TextFormatter.hide_think_tags(self.current_out_text, for_display=True)

        if self.chk_markdown.isChecked():
            html = TextFormatter.markdown_to_html(clean_text)
            self.output_box.setHtml(html)
        else:
            # 非 Markdown 模式下，直接替换换行符比 setHtml 快得多
            self.output_box.setPlainText(clean_text)

        # 自动滚动到底部
        self.output_box.verticalScrollBar().setValue(self.output_box.verticalScrollBar().maximum())

        # 重置标记
        self._is_render_dirty = False

    def _on_translation_finished(self, result: dict = None):
        """翻译完成回调"""
        self._reset_buttons()

        if result and not result.get("success", True):
            error_msg = result.get("msg", "Unknown error")
            self.logger.error(f"Translation failed: {error_msg}")

    def _on_error(self, error_msg: str):
        """错误回调"""
        self.output_box.setHtml(f"<span style='color:#ff5555;'><b>Error:</b> {error_msg}</span>")
        self._reset_buttons()

    def _reset_buttons(self):
        """重置按钮状态"""
        self.btn_stop.setVisible(False)
        self.btn_trans.setVisible(True)
        self.btn_trans.setEnabled(True)

    def _save_markdown_setting(self, checked):
        """保存Markdown渲染设置"""
        self.cfg_mgr.user_settings["quick_trans_markdown"] = checked
        self.cfg_mgr.save_settings()

    def _update_pin_ui(self):
        """更新置顶按钮图标"""
        if self.is_pinned:
            self.btn_pin.setIcon(ThemeManager().icon("keep", "accent"))
            self.btn_pin.setToolTip("Unpin Window")
            self.btn_pin.setStyleSheet(
                "QPushButton { background: transparent; color: #05B8CC; border: none; font-size: 15px; } "
                "QPushButton:hover { color: #fff; }")
        else:
            self.btn_pin.setIcon(ThemeManager().icon("keep_off", "text_muted"))
            self.btn_pin.setToolTip("Pin to Top")
            self.btn_pin.setStyleSheet(
                "QPushButton { background: transparent; color: #888; border: none; font-size: 15px; opacity: 0.6; } "
                "QPushButton:hover { color: #ccc; }")

    def _re_render_output(self, checked):
        """重新渲染输出"""
        if not self.current_out_text:
            return

        clean_text = TextFormatter.hide_think_tags(self.current_out_text, for_display=True)
        if checked:
            html_str = TextFormatter.markdown_to_html(clean_text)
            self.output_box.setHtml(html_str)
        else:
            self.output_box.setHtml(clean_text.replace('\n', '<br>'))
        self.output_box.verticalScrollBar().setValue(self.output_box.verticalScrollBar().maximum())

    def _toggle_pin(self):
        """切换窗口置顶状态"""
        self.is_pinned = not self.is_pinned
        self.settings.setValue("is_pinned", self.is_pinned)

        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.is_pinned)

        self.show()

        if sys.platform == "win32":
            hwnd = int(self.winId())
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            insert_after = HWND_TOPMOST if self.is_pinned else HWND_NOTOPMOST
            flags = 0x0002 | 0x0001 | 0x0010  # SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            ctypes.windll.user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)

        self._update_pin_ui()

    def _save_lang(self, key, val):
        """保存语言设置"""
        self.cfg_mgr.user_settings[key] = val
        self.cfg_mgr.save_settings()

    def receive_and_translate(self, text):
        """接收外部调用并翻译"""
        self.fade_in()
        self.input_box.setPlainText(text)
        self._start_translation()

    def fade_in(self):
        """淡入动画"""
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
        self.settings.setValue("geometry", self.saveGeometry())

        self.anim = QPropertyAnimation(self, b"windowOpacity")
        self.anim.setDuration(200)
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(0.0)
        self.anim.finished.connect(self.hide)
        self.anim.start()

    def _center_on_screen(self):
        """窗口居中"""
        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2, int((screen.height() - self.height()) // 2))

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)

    def _clear_all(self):
        """清空所有内容"""
        if self.translator_manager.is_running():
            self.translator_manager.cancel_translation()

        self.input_box.clear()
        self.output_box.clear()
        self.current_out_text = ""

        self.btn_clear.setText(" Cleared!")
        self.clear_timer.start(2000)

    def _reset_clear_btn(self):
        """重置清除按钮"""
        self.btn_clear.setText(" Clear")
        if hasattr(ThemeManager(), 'icon'):
            self.btn_clear.setIcon(ThemeManager().icon("clear", "text_main"))

    def _copy_result(self):
        """复制翻译结果"""
        if not self.current_out_text:
            return
        clean_text = TextFormatter.hide_think_tags(self.current_out_text, for_display=False)
        clean_text = re.sub(r"<[^>]+>", "", clean_text).strip()
        QApplication.clipboard().setText(clean_text)

        from src.ui.components.toast import ToastManager
        ToastManager().show("Translated text copied to clipboard.", "success")

        self.btn_copy.setText(" Copied!")
        self.copy_timer.start(2000)

    def _reset_copy_btn(self):
        """重置复制按钮"""
        self.btn_copy.setText(" Copy")
        if hasattr(ThemeManager(), 'icon'):
            self.btn_copy.setIcon(ThemeManager().icon("copy", "text_main"))


    def _apply_theme(self):
        """应用主题"""
        tm = ThemeManager()

        self.main_frame.setStyleSheet(
            f"QWidget {{ background-color: {tm.color('bg_card')}; border: 1px solid {tm.color('border')}; border-radius: 12px; }}")

        input_style = (f"background-color: {tm.color('bg_input')}; color: {tm.color('text_main')}; "
                       f"border: 1px solid {tm.color('border')}; border-radius: 6px; padding: 8px;")
        self.input_box.setStyleSheet(input_style)
        self.output_box.setStyleSheet(input_style + " font-size: 14px;")

        combo_style = f"""
            QComboBox {{
                background: {tm.color('bg_input')}; 
                color: {tm.color('text_main')}; 
                border: 1px solid {tm.color('border')}; 
                border-radius: 4px;
            }}
            QComboBox:hover {{
                border: 1px solid {tm.color('accent')};
            }}
        """
        self.combo_src.setStyleSheet(combo_style)
        self.combo_tgt.setStyleSheet(combo_style)

        self.chk_markdown.setStyleSheet(f"""
            QCheckBox {{
                background-color: transparent; 
            }}
            QCheckBox:hover {{
                color: {tm.color('accent')};
            }}
        """)

        if hasattr(self, 'lbl_trans_icon'):
            self.lbl_trans_icon.setPixmap(tm.icon("language", "text_main").pixmap(16, 16))

        self.btn_trans.setText(" Translate / Polish")
        self.btn_trans.setIcon(tm.icon("send", "bg_main"))
        self.btn_trans.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('accent')}; color: {tm.color('bg_main')}; 
                         border-radius: 6px; padding: 6px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {tm.color('title_blue')}; }}
        """)

        self.btn_stop.setText(" Stop")
        self.btn_stop.setIcon(tm.icon("close", "bg_main"))
        self.btn_stop.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('danger')}; color: {tm.color('bg_main')}; 
                         border-radius: 6px; padding: 6px; font-weight: bold; }}
            QPushButton:hover {{ background-color: #a32418; }}
        """)

        self.btn_clear.setText(" Clear")
        self.btn_clear.setIcon(tm.icon("clear", "text_main"))
        self.btn_clear.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; 
                         border-radius: 6px; padding: 6px; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """)

        self.btn_copy.setText(" Copy")
        self.btn_copy.setIcon(tm.icon("copy", "text_main"))
        self.btn_copy.setStyleSheet(f"""
            QPushButton {{ background-color: {tm.color('btn_bg')}; color: {tm.color('text_main')}; 
                         border-radius: 6px; padding: 6px; }}
            QPushButton:hover {{ background-color: {tm.color('btn_hover')}; }}
        """)


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

    def mouseMoveEvent(self, event):
        pos = event.pos()

        # 鼠标悬停边缘时改变光标
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

        # 鼠标拖动处理
        if event.buttons() == Qt.LeftButton and self.drag_pos is not None:
            if getattr(self, '_resize_dir', None):
                # 拉伸窗口
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

                # 约束最小窗口
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
                # 移动窗口
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