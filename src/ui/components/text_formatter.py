import re
from src.core.theme_manager import ThemeManager

class TextFormatter:
    @staticmethod
    def format_chat_text(text, index, expanded_indices, user_toggled_thinks):
        tm = ThemeManager()
        accent_color = tm.color('accent')
        bg_color = tm.color('bg_input')
        border_color = tm.color('border')
        text_muted = tm.color('text_muted')

        think_content = ""
        main_text = text
        is_closed = True

        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)

        if final_answer_match:
            raw_think = text[:final_answer_match.start()]
            raw_main = text[final_answer_match.end():]

            think_content = re.sub(r'</?think\s*>', '', raw_think, flags=re.IGNORECASE).strip()
            main_text = re.sub(r'</?think\s*>', '', raw_main, flags=re.IGNORECASE).strip()
            is_closed = True
        else:
            text = re.sub(r'<\s*/\s*think\s*>', '</think>', text, flags=re.IGNORECASE)
            think_match = re.search(r'<think>(.*?)</think>', text, flags=re.DOTALL | re.IGNORECASE)

            if think_match:
                think_content = think_match.group(1).strip()
                main_text = text.replace(think_match.group(0), "").strip()
                is_closed = True
            elif "<think>" in text:
                parts = text.split("<think>", 1)
                raw_after = parts[1]

                split_match = re.search(r'\n{2,}(?=\*\*|#|- |\d+\.|Yes,|Certainly,|Here )', raw_after, flags=re.IGNORECASE)
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

        final_html = ""
        if think_content or not is_closed:
            if index in user_toggled_thinks:
                is_expanded = index in expanded_indices
            else:
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon_name = "chevron-down" if is_expanded else "chevron-right"
            icon_html = f"<img src='assets/icons/{icon_name}.svg' width='14' height='14' style='vertical-align: middle;' />"
            status = "AI Thinking Process" if is_closed else "AI Thinking..."

            link = f"<a href='think://{action}?index={index}' style='color:{accent_color}; text-decoration:none;'><nobr>{icon_html} <b>{status}</b></nobr></a>"

            if not is_expanded:
                final_html += f"<div style='background:{bg_color}; border-left: 3px solid {border_color}; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px;'>{link}</div>"
            else:
                safe_content = think_content.replace('\n', '<br>')
                safe_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_content)
                safe_content = re.sub(r'\*(.*?)\*', r'<i>\1</i>', safe_content)

                suffix = "" if is_closed else f" <span style='color:{accent_color};'><i>...</i></span>"
                final_html += (f"<div style='background:{bg_color}; border-left: 3px solid {accent_color}; padding: 8px 12px; "
                               f"margin: 10px 0; border-radius: 4px; font-size: 13px; color: {text_muted};'>"
                               f"{link}<br><br><div style='color:{text_muted};'>{safe_content}{suffix}</div></div>")

        if main_text:
            main_text = re.sub(r'\[FINAL_ANSWER\]\s*', '', main_text, flags=re.IGNORECASE)
            main_text = re.sub(r'\[FOLLOW_UPS\]\s*', '', main_text, flags=re.IGNORECASE)
            final_html += f"\n\n{main_text}"

        return final_html

    @staticmethod
    def hide_think_tags(text):
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_answer_match:
            cleaned = text[final_answer_match.end():]
            return re.sub(r'</?think\s*>', '', cleaned, flags=re.IGNORECASE).strip()

        cleaned = re.sub(r'<think>.*?(</think>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
        if '<think>' in text and not cleaned.strip():
            tm = ThemeManager()
            return f"<span style='color:{tm.color('text_muted')}; font-style:italic;'>[AI is reasoning in the background...]</span>"
        return cleaned.lstrip()