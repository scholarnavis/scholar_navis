import re

from src.core.theme_manager import ThemeManager


class TextFormatter:
    @staticmethod
    def format_chat_text(text, index, expanded_indices, user_toggled_thinks):
        tm = ThemeManager()
        primary_color = tm.color('primary')   # 替换原来的 #05B8CC
        think_bg = tm.color('bg_card')        # 替换原来的 #222
        think_border = tm.color('border')     # 替换原来的 #555
        text_muted = tm.color('text_muted')   # 替换原来的 #aaa / #999

        think_content = ""
        main_text = text
        is_closed = True

        # 只要碰到 [FINAL_ANSWER]，不管外面有没有 <think> 或 </think>，直接一刀劈开！
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)

        if final_answer_match:
            raw_think = text[:final_answer_match.start()]
            raw_main = text[final_answer_match.end():]

            # 无情剥离两边残留的任何 think 标签碎片
            think_content = re.sub(r'</?think\s*>', '', raw_think, flags=re.IGNORECASE).strip()
            main_text = re.sub(r'</?think\s*>', '', raw_main, flags=re.IGNORECASE).strip()
            is_closed = True

        else:
            # 2. 如果还没输出到 [FINAL_ANSWER]，走常规解析
            text = re.sub(r'<\s*/\s*think\s*>', '</think>', text, flags=re.IGNORECASE)
            think_match = re.search(r'<think>(.*?)</think>', text, flags=re.DOTALL | re.IGNORECASE)

            if think_match:
                think_content = think_match.group(1).strip()
                main_text = text.replace(think_match.group(0), "").strip()
                is_closed = True
            elif "<think>" in text:
                parts = text.split("<think>", 1)
                raw_after = parts[1]

                # 备用断点：防万一它连锚点都忘了吐
                split_match = re.search(r'\n{2,}(?=\*\*|#|- |\d+\.|Yes,|Certainly,|Here |Based on)', raw_after,
                                        flags=re.IGNORECASE)
                if split_match:
                    split_idx = split_match.start()
                    think_content = raw_after[:split_idx].strip()
                    main_text = parts[0].strip() + "\n\n" + raw_after[split_idx:].strip()
                    is_closed = True
                else:
                    think_content = raw_after.strip()
                    main_text = parts[0].strip()
                    is_closed = False
            else:
                return text

        # 3. 构造 HTML 思考块
        final_html = ""
        if think_content or not is_closed:
            if index in user_toggled_thinks:
                is_expanded = index in expanded_indices
            else:
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon = "🔽" if is_expanded else "▶️"
            status = "AI Thinking Process" if is_closed else "AI Thinking..."

            link = f"<a href='think://{action}?index={index}' style='color:#05B8CC; text-decoration:none;'><nobr>{icon} <b>{status}</b></nobr></a>"

            if not is_expanded:
                final_html += f"<div style='background:#222; border-left: 3px solid #555; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px;'>{link}</div>"
            else:
                safe_content = think_content.replace('\n', '<br>')
                safe_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_content)
                safe_content = re.sub(r'\*(.*?)\*', r'<i>\1</i>', safe_content)

                suffix = "" if is_closed else " <span style='color:#05B8CC;'><i>...</i></span>"
                final_html += (f"<div style='background:#222; border-left: 3px solid #05B8CC; padding: 8px 12px; "
                               f"margin: 10px 0; border-radius: 4px; font-size: 13px; color: #aaa;'>"
                               f"{link}<br><br><div style='color:#999;'>{safe_content}{suffix}</div></div>")

        # 4. 正文拼接到外部，完美释放 Markdown
        if main_text:
            main_text = re.sub(r'\[FINAL_ANSWER\]\s*', '', main_text, flags=re.IGNORECASE)
            main_text = re.sub(r'\[FOLLOW_UPS\]\s*', '', main_text, flags=re.IGNORECASE)  # 👇 新增这行

            final_html += f"\n\n{main_text}"

        return final_html

    @staticmethod
    def hide_think_tags(text):
        """翻译窗口专用：配合锚点逻辑，无情剔除所有思考过程"""
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_answer_match:
            cleaned = text[final_answer_match.end():]
            return re.sub(r'</?think\s*>', '', cleaned, flags=re.IGNORECASE).strip()

        cleaned = re.sub(r'<think>.*?(</think>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
        if '<think>' in text and not cleaned.strip():
            return "<span style='color:#888; font-style:italic;'>[🤔 AI is reasoning in the background...]</span>"
        return cleaned.lstrip()