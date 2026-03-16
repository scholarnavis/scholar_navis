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
    def _render_chemistry(text):
        """识别并格式化化学分子式"""

        def formula_replacer(match):
            prefix = match.group(1)
            formula = match.group(2)
            subscripted = re.sub(r'(?<=[A-Za-z\)\]])(\d+)', r'<sub>\1</sub>', formula)
            return f"{prefix}{subscripted}"

        text = re.sub(
            r'(?i)((?:Molecular|Chemical|Empirical)[\s\*_]*formula[\s\*_:]*)([A-Za-z0-9\(\)\[\]]+)',
            formula_replacer,
            text
        )

        return text

    @staticmethod
    def format_chat_text(text, index, expanded_indices, user_toggled_thinks):
        tm = ThemeManager()
        accent_color = tm.color('accent')
        bg_color = tm.color('bg_input')
        border_color = tm.color('border')
        text_muted = tm.color('text_muted')

        think_contents = []
        mcp_contents = []
        is_closed = True

        final_answer_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)

        # 统一规范化标签
        text = re.sub(r'<\s*think\s*>', '<think>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*/\s*think\s*>', '</think>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*mcp_process\s*>', '<mcp_process>', text, flags=re.IGNORECASE)
        text = re.sub(r'<\s*/\s*mcp_process\s*>', '</mcp_process>', text, flags=re.IGNORECASE)

        if final_answer_match:
            raw_hidden = text[:final_answer_match.start()]
            main_text = text[final_answer_match.end():].strip()
            is_closed = True

            # 提取已闭合的所有标签内容
            for t_match in re.finditer(r'<think>(.*?)</think>', raw_hidden, flags=re.DOTALL | re.IGNORECASE):
                think_contents.append(t_match.group(1).strip())
            for m_match in re.finditer(r'<mcp_process>(.*?)</mcp_process>', raw_hidden,
                                       flags=re.DOTALL | re.IGNORECASE):
                mcp_contents.append(m_match.group(1).strip())
        else:
            def think_repl(match):
                think_contents.append(match.group(1).strip())
                return ""

            main_text = re.sub(r'<think>(.*?)</think>', think_repl, text, flags=re.DOTALL | re.IGNORECASE)

            def mcp_repl(match):
                mcp_contents.append(match.group(1).strip())
                return ""

            main_text = re.sub(r'<mcp_process>(.*?)</mcp_process>', mcp_repl, main_text,
                               flags=re.DOTALL | re.IGNORECASE)

            unclosed_think = re.search(r'<think>(.*)$', main_text, flags=re.DOTALL | re.IGNORECASE)
            if unclosed_think:
                think_contents.append(unclosed_think.group(1).strip())
                main_text = main_text[:unclosed_think.start()].strip()
                is_closed = False

            unclosed_mcp = re.search(r'<mcp_process>(.*)$', main_text, flags=re.DOTALL | re.IGNORECASE)
            if unclosed_mcp:
                mcp_contents.append(unclosed_mcp.group(1).strip())
                main_text = main_text[:unclosed_mcp.start()].strip()
                is_closed = False

        hidden_blocks = []
        if think_contents:
            hidden_blocks.append("<b>🧠 AI Reasoning:</b><br>" + "<br><br>".join(filter(None, think_contents)))
        if mcp_contents:
            hidden_blocks.append("<b>🛠️ MCP Tool Execution:</b><br>" + "<br><br>".join(filter(None, mcp_contents)))

        hidden_content = "<br><br>".join(hidden_blocks)
        final_html = ""

        if hidden_content or not is_closed:
            if index in user_toggled_thinks:
                is_expanded = index in expanded_indices
            else:
                is_expanded = not is_closed

            action = "collapse" if is_expanded else "expand"
            icon_name = "chevron-down" if is_expanded else "chevron-right"
            icon_html = f"<img src='assets/icons/{icon_name}.svg' width='14' height='14' style='vertical-align: middle;' />"

            # 根据内容智能显示折叠面板标题
            if mcp_contents and not think_contents:
                status_title = "Tool Execution (Completed)" if is_closed else "Executing Tools..."
            elif mcp_contents and think_contents:
                status_title = "Reasoning & Tool Execution (Completed)" if is_closed else "AI is Analyzing & Working..."
            else:
                status_title = "AI Reasoning (Completed)" if is_closed else "AI is thinking..."

            link = f"<a href='think://{action}?index={index}' style='color:{accent_color}; text-decoration:none;'><nobr>{icon_html} <b>{status_title}</b></nobr></a>"

            if not is_expanded:
                final_html += f"<div style='background:{bg_color}; border-left: 3px solid {border_color}; padding: 8px 12px; margin: 10px 0; border-radius: 4px; font-size: 13px;'>{link}</div>"
            else:
                safe_content = hidden_content.replace('\n', '<br>')
                safe_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe_content)
                safe_content = re.sub(r'\*(.*?)\*', r'<i>\1</i>', safe_content)
                suffix = "" if is_closed else f" <span style='color:{accent_color};'><i>...</i></span>"
                final_html += (
                    f"<div style='background:{bg_color}; border-left: 3px solid {accent_color}; padding: 8px 12px; "
                    f"margin: 10px 0; border-radius: 4px; font-size: 13px; color: {text_muted};'>"
                    f"{link}<br><br><div style='color:{text_muted};'>{safe_content}{suffix}</div></div>")

        if main_text:
            main_text = re.sub(r'\[FINAL_ANSWER\]\s*', '', main_text, flags=re.IGNORECASE)
            main_text = re.sub(r'\[\s*FOLLOW[_-]?\s*UPS?\s*\]\s*', '', main_text, flags=re.IGNORECASE)

            main_text = re.sub(r'<br\s*/?>', '\n', main_text, flags=re.IGNORECASE)

            rendered_main_html = TextFormatter.markdown_to_html(main_text)
            final_html += f"\n\n{rendered_main_html}"

        return final_html

    @staticmethod
    def markdown_to_html(text):
        tm = ThemeManager()
        processed_text = text

        # ================= 救砖：修复丢失换行符的极度压缩 Markdown =================
        # 1. 修复连成一行的水平分割线
        processed_text = re.sub(r'(?<=\S)\s+(--+)\s+(?=\S)', r'\n\n\1\n\n', processed_text)
        # 2. 修复紧贴文本的标题，以及跟在表格后面的标题
        processed_text = re.sub(r'(\|\s*)(#{1,6}\s+)', r'\1\n\n\2', processed_text)
        processed_text = re.sub(r'(?<=\S)\s+(#{1,6}\s+)', r'\n\n\1', processed_text)
        processed_text = re.sub(r'([^\n])\n(#{1,6}\s+)', r'\1\n\n\2', processed_text)
        # 3. 修复标题和表格完全粘在一行的情况
        processed_text = re.sub(r'(#{1,6}\s+[^|\n]+?)\s+(\|)', r'\1\n\n\2', processed_text)
        # 4. 修复表格缺空行导致的解析失败 (紧贴文本的表格前加强制换行)
        processed_text = re.sub(r'([^\n])\n(\s*\|.*\|)\s*\n(\s*\|[-:| ]+\|)', r'\1\n\n\2\n\3', processed_text)
        # 5. 修复表格内部被压扁成单行的情况
        processed_text = processed_text.replace('| |-', '|\n|-')
        processed_text = re.sub(r'(\|\s*\[\d+\]\s*\|)\s*(?=\|)', r'\1\n', processed_text)
        # =========================================================================

        processed_text = TextFormatter._render_simple_latex(processed_text)
        processed_text = TextFormatter._render_chemistry(processed_text)

        html = markdown.markdown(processed_text, extensions=['extra', 'nl2br', 'sane_lists', 'tables'])

        skip_pattern = r'(?si)(<a\b[^>]*>.*?</a>|<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>)'

        url_pattern = skip_pattern + r'|(?<![="\'/])\b((?:https?|ftp|file)://[^\s<>\)\]"\'，。？！；：“”‘’\n]+(?<![.,?!;:：]))'

        def url_repl(match):
            if match.group(1): return match.group(1)  # 返回原样 (因被 Skip 保护)
            return f'<a href="{match.group(2)}">{match.group(2)}</a>'

        html = re.sub(url_pattern, url_repl, html)

        # 3. 匹配常见科研数据库/论文 ID 及其它标识，自动挂载官方解析链接
        replacements = [
            # DOI
            (r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)(?<![.,?!;:：])', r'<a href="https://doi.org/\1">\1</a>'),
            # TaxID
            (r'\b(?:taxid|taxonomy\s*id)\s*:?\s*(\d+)\b',
             r'<a href="https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=\1">TaxID: \1</a>'),
            # BioProject
            (r'\b(PRJN[A-Z]\d+)\b', r'<a href="https://www.ncbi.nlm.nih.gov/bioproject/\1">\1</a>'),
            # NCBI Assembly (GCF / GCA)
            (r'\b(GC[FA]_\d{9}(?:\.\d+)?)\b', r'<a href="https://www.ncbi.nlm.nih.gov/datasets/genome/\1/">\1</a>'),
            # NCBI RefSeq / GenBank / Accessions
            (r'\b((?:NM|NP|NR|NC|NG|XM|XP|XR|WP|YP|AP)_\d{4,10}(?:\.\d+)?)\b',
             r'<a href="https://www.ncbi.nlm.nih.gov/search/all/?term=\1">\1</a>'),

            # AlphaFold 原始模型文件下载提取
            (r'\b(AF-[A-Z0-9]{6,10}-F\d+-model_v\d+\.(?:pdb|cif))\b',
             r'<a href="https://alphafold.ebi.ac.uk/files/\1" style="color: #10b981;">📥 \1</a>'),
            # AlphaFold 结构标识符
            (r'\b(AF-[A-Z0-9]{6,10}-F\d+)\b',
             r'<a href="https://alphafold.ebi.ac.uk/entry/\1">AlphaFold \1</a>'),

            # UniProt
            (r'\b([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})\b',
             r'<a href="https://www.uniprot.org/uniprotkb/\1/entry">\1</a>'),
            # Ensembl
            (r'\b(ENS[GTPER]\d{11})\b', r'<a href="https://www.ensembl.org/id/\1">\1</a>'),
            # E.C. 酶编号
            (r'\b(?:EC\s+|E\.C\.\s*)(\d+\.\d+\.\d+\.(?:\d+|-))\b',
             r'<a href="https://enzyme.expasy.org/EC/\1">EC \1</a>'),
            # PubChem CID
            (r'\b(?:CID|PubChem\s*CID)\s*:?\s*(\d+)\b',
             r'<a href="https://pubchem.ncbi.nlm.nih.gov/compound/\1">CID \1</a>'),
            # PDB 晶体结构
            (r'\bPDB\s*(?:ID\s*)?:?\s*([1-9][A-Z0-9]{3})\b', r'<a href="https://www.rcsb.org/structure/\1">PDB \1</a>'),
            # STRING DB 蛋白互作网络
            (r'\b(\d+\.ENSP\d{11})\b', r'<a href="https://string-db.org/network/\1">\1</a>'),

            # GBIF Taxon Key (分类单元唯一标识符)
            (r'\b(?:GBIF\s*Taxon\s*Key|GBIF\s*ID|TaxonKey)\s*:?\s*(\d+)\b',
             r'<a href="https://www.gbif.org/species/\1">GBIF Taxon \1</a>'),
            # GBIF 发生记录主页映射
            (r'(Global\s*Biodiversity\s*Information\s*Facility\s*\(GBIF\)\s*-\s*Occurrence\s*(?:Download|Records?))',
             r'<a href="https://www.gbif.org/occurrence/search">\1</a>'),

            # Gene Ontology (GO) 映射
            (r'\b(GO:\d{7})\b', r'<a href="https://www.ebi.ac.uk/QuickGO/term/\1">\1</a>'),
            # ChEMBL Target ID 映射
            (r'\b(CHEMBL\d+)\b', r'<a href="https://www.ebi.ac.uk/chembl/target_report_card/\1">\1</a>'),

            # 拟南芥 AGI 基因号
            (r'\b(AT[1-5CM]G\d{5})\b',
             r'<a href="https://plants.ensembl.org/Arabidopsis_thaliana/Gene/Summary?g=\1">\1</a>'),

        ]

        for pat, template in replacements:
            combined_pat = re.compile(skip_pattern + r'|' + pat)

            def get_replacer(tmpl):
                def replacer_func(match):
                    if match.group(1): return match.group(1)
                    return tmpl.replace(r'\1', match.group(2))

                return replacer_func

            html = combined_pat.sub(get_replacer(template), html)

        html = html.replace("<a href=", "<a style='color: #4daafc; text-decoration: none; font-weight: bold;' href=")
        parts = re.split(r'(<[^>]+>)', html)
        for i in range(0, len(parts), 2):
            if parts[i]:
                parts[i] = re.sub(
                    r'[^\s&;]{40,}',
                    lambda m: '\u200b'.join(list(m.group(0))),
                    parts[i]
                )
        html = ''.join(parts)

        final_html = f"<div style='font-family: {tm.font_family()};'>{html}</div>"

        return final_html

    @staticmethod
    def clean_text_for_export(text, include_citations=True):
        final_match = re.search(r'\[FINAL_ANSWER\]\s*', text, flags=re.IGNORECASE)
        if final_match:
            text = text[final_match.end():]
        else:
            text = re.sub(r'<(think|mcp_process)>.*?(?:</\1>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)

        text = re.sub(r"\[CLEAR_SEARCH\]|\[START_LLM_NETWORK\]|\[\s*FOLLOW[_-]?\s*UPS?\s*\]", "", text,
                      flags=re.IGNORECASE)
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
            return re.sub(r'</?(think|mcp_process)\s*>', '', cleaned, flags=re.IGNORECASE).strip()

        # 修改为匹配两种标签
        cleaned = re.sub(r'<(think|mcp_process)>.*?(?:</\1>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)

        if ('<think>' in text or '<mcp_process>' in text) and not cleaned.strip():
            if for_display:
                from src.core.theme_manager import ThemeManager
                tm = ThemeManager()
                return f"<span style='color:{tm.color('text_muted')}; font-style:italic;'>[AI is working in the background...]</span>"
            return ""
        return cleaned.lstrip()

    @staticmethod
    def clean_text_for_copy(text):
        return TextFormatter.clean_text_for_export(text, include_citations=False)
