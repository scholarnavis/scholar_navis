import datetime
import json
import urllib.request
import xml.etree.ElementTree as ET

# ==========================================
# 1. 必须定义的 SCHEMA (OpenAI 格式)
# ==========================================
SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Get the current date and time.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}


# ==========================================
# 2. 必须定义的 execute 函数
# ==========================================
def execute() -> str:
    """
    实际执行逻辑。
    返回当前的日期和时间。
    """
    try:
        # 获取当前时间并格式化
        now = datetime.now()
        formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")

        # 构建给大模型的结果
        result = {
            "status": "success",
            "current_time": formatted_time
        }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Unexpected error during execution: {str(e)}"})