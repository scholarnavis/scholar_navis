import re
import markdown
from src.core.theme_manager import ThemeManager


class TextFormatter:

    @staticmethod
    def _render_simple_latex(text):

        def replacer(match):
            formula = match.group(1)
            formula = re.sub(r'\\text\{([^}]+)\}', r'\1', formula)
            formula = re.sub(r'\^\{([^}]+)\}', r'<sup>\1</sup>', formula)
            formula = re.sub(r'\^([a-zA-Z0-9])', r'<sup>\1</sup>', formula)
            formula = re.sub(r'_\{([^}]+)\}', r'<sub>\1</sub>', formula)
            formula = re.sub(r'_([a-zA-Z0-9])', r'<sub>\1</sub>', formula)
            formula = formula.replace('{}', '')
            return f"<i>{formula}</i>"

        text = re.sub(r'\$\$(.*?)\$\$', replacer, text, flags=re.DOTALL)
        text = re.sub(r'\$(.*?)\$', replacer, text)
        return text


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

        # 兼容旧版本可能残留的 [FINAL_ANSWER] 标签
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)

        if final_answer_match:
            raw_think = text[:final_answer_match.start()]
            raw_main = text[final_answer_match.end():]

            think_content = re.sub(r'</?think\s*>', '', raw_think, flags=re.IGNORECASE).strip()
            main_text = re.sub(r'</?think\s*>', '', raw_main, flags=re.IGNORECASE).strip()
            is_closed = True
        else:
            # 标准化标签
            text = re.sub(r'<\s*think\s*>', '<think>', text, flags=re.IGNORECASE)
            text = re.sub(r'<\s*/\s*think\s*>', '</think>', text, flags=re.IGNORECASE)

            # 1. 尝试匹配已闭合的完整思考块
            think_match = re.search(r'<think>(.*?)</think>', text, flags=re.DOTALL | re.IGNORECASE)

            if think_match:
                think_content = think_match.group(1).strip()
                # 从原文中剔除思考块，剩下的就是正文
                main_text = text.replace(think_match.group(0), "").strip()
                is_closed = True
            elif "<think>" in text:
                # 2. 未闭合状态 (流式生成中)：<think> 之后的所有内容绝对都是思考内容
                parts = text.split("<think>", 1)
                main_text = parts[0].strip()
                think_content = parts[1].strip()
                is_closed = False

        final_html = ""

        # --- 渲染思考块 ---
        if think_content or not is_closed:
            if index in user_toggled_thinks:
                is_expanded = index in expanded_indices
            else:
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon_name = "chevron-down" if is_expanded else "chevron-right"
            icon_html = f"<img src='assets/icons/{icon_name}.svg' width='14' height='14' style='vertical-align: middle;' />"
            status = "AI 深度思考 (已完成)" if is_closed else "AI 正在思考..."

            link = f"<a href='think://{action}?index={index}' style='color:{accent_color}; text-decoration:none;'><nobr>{icon_html} <b>{status}</b></nobr></a>"

            if not is_expanded:
                final_html += f"<div style='background:{bg_color}; border-left: 3px solid {border_color}; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px;'>{link}</div>"
            else:
                safe_content = think_content.replace('\n', '<br>')
                safe_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_content)
                safe_content = re.sub(r'\*(.*?)\*', r'<i>\1</i>', safe_content)

                suffix = "" if is_closed else f" <span style='color:{accent_color};'><i>...</i></span>"
                final_html += (
                    f"<div style='background:{bg_color}; border-left: 3px solid {accent_color}; padding: 8px 12px; "
                    f"margin: 10px 0; border-radius: 4px; font-size: 13px; color: {text_muted};'>"
                    f"{link}<br><br><div style='color:{text_muted};'>{safe_content}{suffix}</div></div>")

        # --- 渲染主文本 ---
        if main_text:
            # 清理可能的杂乱系统尾缀
            main_text = re.sub(r'\[FINAL_ANSWER\]\s*', '', main_text, flags=re.IGNORECASE)
            main_text = re.sub(r'\[FOLLOW_UPS\]\s*', '', main_text, flags=re.IGNORECASE)
            final_html += f"\n\n{main_text}"

        return final_html

    @staticmethod
    def markdown_to_html(text):
        tm = ThemeManager()
        processed_text = text
        processed_text = TextFormatter._render_simple_latex(processed_text)
        processed_text = re.sub(r'(?<![="\'/])\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b',
                                r'<a href="https://doi.org/\1">\1</a>', processed_text)
        processed_text = re.sub(r'(?<![="\'/\[\(])\b(https?://[^\s<>\)\]]+)\b', r'<a href="\1">\1</a>',
                                processed_text)

        html = markdown.markdown(processed_text, extensions=['extra', 'nl2br', 'sane_lists', 'tables'])

        html = html.replace("<a href=", "<a style='color: #4daafc; text-decoration: none; font-weight: bold;' href=")
        final_html = f"<div style='font-family: {tm.font_family()}; line-height: 1.5;'>{html}</div>"

        return final_html

    @staticmethod
    def clean_text_for_export(text, include_citations=True):
        final_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_match:
            text = text[final_match.end():]
        else:
            text = re.sub(r'<think>.*?(?:</think>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)

        text = re.sub(r"\[CLEAR_SEARCH\]|\[START_LLM_NETWORK\]|\[FOLLOW_UPS\]", "", text)
        text = re.sub(r"\[AI is reasoning in the background\.\.\.\]", "", text, flags=re.IGNORECASE)

        if include_citations and "<b>📚 Cited Sources:</b>" in text:
            parts = text.split("<b>📚 Cited Sources:</b><br>")
            main_text = re.sub(r"<[^>]+>", "", parts[0].replace("<br>", "\n")).strip()
            citations_text = "\n\n📚 Reference:\n"
            if len(parts) > 1:
                raw_cites = parts[1]
                matches = re.findall(r"<b>\[(\d+)\]</b>\s*(.*?)\s*\(Page (\d+)\)", raw_cites)
                for m in matches:
                    idx, name, page = m
                    citations_text += f"[{idx}] {name.strip()} (第 {page} 页)\n"
            text = main_text + citations_text
        else:
            text = re.sub(r"<[^>]+>", "", text.replace("<br>", "\n")).strip()

        return text.strip()

    @staticmethod
    def hide_think_tags(text, for_display=False):
        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_answer_match:
            cleaned = text[final_answer_match.end():]
            return re.sub(r'</?think\s*>', '', cleaned, flags=re.IGNORECASE).strip()

        cleaned = re.sub(r'<think>.*?(?:</think>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
        if '<think>' in text and not cleaned.strip():
            if for_display:
                from src.core.theme_manager import ThemeManager
                tm = ThemeManager()
                return f"<span style='color:{tm.color('text_muted')}; font-style:italic;'>[AI is reasoning in the background...]</span>"
            return ""
        return cleaned.lstrip()

    @staticmethod
    def clean_text_for_copy(text):
        return TextFormatter.clean_text_for_export(text, include_citations=False)