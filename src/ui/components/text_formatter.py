import re


class TextFormatter:
    @staticmethod
    def format_chat_text(text, index, expanded_indices, user_toggled_thinks):
        """为聊天窗口生成带有交互折叠功能的 Think 块"""

        def replacer(match):
            content = match.group(1).strip()
            is_closed = match.group(2) == "</think>"

            if index in user_toggled_thinks:
                is_expanded = index in expanded_indices
            else:
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon = "🔽" if is_expanded else "▶️"
            status = "AI Thinking Process" if is_closed else "AI Thinking..."

            link = f"<a href='think://{action}?index={index}' style='color:#05B8CC; text-decoration:none;'><nobr>{icon} <b>{status}</b></nobr></a>"

            if not is_expanded:
                return f"<div style='background:#222; border-left: 3px solid #555; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px;'>{link}</div>"
            else:
                safe_content = content.replace('\n', '<br>')
                suffix = "" if is_closed else " <span style='color:#05B8CC;'><i>...</i></span>"
                return (f"<div style='background:#222; border-left: 3px solid #05B8CC; padding: 8px 12px; "
                        f"margin: 10px 0; border-radius: 4px; font-size: 13px; color: #aaa;'>"
                        f"{link}<br><br><div style='color:#999;'>{safe_content}{suffix}</div></div>")

        return re.sub(r'<think>(.*?)(</think>|$)', replacer, text, flags=re.DOTALL)

    @staticmethod
    def hide_think_tags(text):
        """为翻译窗口专用：无情剔除所有思考过程，只保留正文"""
        # 实时剥离 <think> 及其内部内容
        cleaned = re.sub(r'<think>.*?(</think>|$)', '', text, flags=re.DOTALL)

        # 如果模型还在输出 <think> 没进入正文阶段，给一个轻量提示
        if '<think>' in text and not cleaned.strip():
            return "<span style='color:#888; font-style:italic;'>[🤔 AI is reasoning in the background...]</span>"

        return cleaned.lstrip()