from PySide6.QtCore import QObject, Signal


class GlobalSignals(QObject):
    _instance = None


    kb_list_changed = Signal()  # 知识库列表变动
    kb_switched = Signal(str)  # 切换库
    kb_modified = Signal(str)  # 知识库发生了实质性更改 (参数: kb_id)
    file_queue_updated = Signal()  # 下载队列变动
    llm_config_changed = Signal() # LLM 配置变更信号
    sig_invoke_translator = Signal(str)  # 唤醒全局翻译器并传入文本
    navigate_to_tool = Signal(str)  # 触发主窗口左侧导航切换 (参数: tool_name)
    request_model_download = Signal(str, str)  # 触发设置页面的下载逻辑 (参数: model_id, model_type)
    sig_send_to_chat = Signal(str, str)
    sig_route_to_chat_with_mcp = Signal(str, str, str)
    sig_token = Signal(str)
    sig_finished = Signal()
    sig_error = Signal(str)
    sig_toast = Signal(str, str)
    mcp_status_changed = Signal()
    theme_changed = Signal()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GlobalSignals, cls).__new__(cls)
            super(GlobalSignals, cls._instance).__init__()
        return cls._instance

    def __init__(self):
        pass