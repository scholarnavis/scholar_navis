import re
import markdown
import os
import tempfile
import shutil
import hashlib
from urllib.parse import urlparse, parse_qs
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from src.core.theme_manager import ThemeManager
from src.ui.components.toast import ToastManager

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
    def markdown_to_plain_text(text):
        """将 Markdown 转换为适合纯文本阅读的格式（例如去除加粗、格式化表格制表符）"""
        # 去除 LaTeX 包装符
        text = re.sub(r'\$\$(.*?)\$\$', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\$(.*?)\$', r'\1', text)
        # 去除标题符
        text = re.sub(r'^#{1,6}\s*(.*)', r'\1', text, flags=re.MULTILINE)
        # 去除加粗和斜体
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        # 去除链接，保留文本
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)

        # 格式化表格
        lines = text.split('\n')
        clean_lines = []
        for line in lines:
            if re.match(r'^\s*\|?.*\|.*\|?\s*$', line):
                if re.match(r'^\s*\|?[\s\-\:\|]+\|?\s*$', line):  # 跳过 Markdown 表格分隔线
                    continue
                # 按列拆分，并使用制表符对齐
                row = [cell.strip() for cell in line.strip('| \t').split('|') if cell.strip() or cell == '']
                clean_lines.append('\t'.join(row))
            else:
                clean_lines.append(line)
        return '\n'.join(clean_lines)


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
                status_title = "Tool Execution" if is_closed else "Executing Tools..."
            elif mcp_contents and think_contents:
                status_title = "Reasoning & Tool Execution" if is_closed else "AI is Analyzing & Working..."
            else:
                status_title = "AI Reasoning" if is_closed else "AI is thinking..."

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

        skip_pattern = r'(?si)(<a\b[^>]*>.*?</a>|<pre\b[^>]*>.*?</pre>|<code\b[^>]*>.*?</code>|<img\b[^>]*>)'

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

            # PubMed PMID (新增)
            (r'\b(?:PMID|PubMed\s*ID)\s*:?\s*(\d+)\b',
             r'<a href="https://pubmed.ncbi.nlm.nih.gov/\1/">PMID \1</a>'),

            # GBIF Taxon Key (分类单元唯一标识符)
            (r'\b(?:GBIF\s*Taxon\s*Key|GBIF\s*ID|TaxonKey)\s*:?\s*(\d+)\b',
             r'<a href="https://www.gbif.org/species/\1">GBIF Taxon \1</a>'),
            # GBIF 发生记录主页映射
            (r'(Global\s*Biodiversity\s*Information\s*Facility\s*\(GBIF\)\s*-\s*Occurrence\s*(?:Download|Records?))',
             r'<a href="https://www.gbif.org/occurrence/search">\1</a>'),

            # Gene Ontology (GO) 映射
            (r'\b(GO:\d{7})\b', r'<a href="https://www.ebi.ac.uk/QuickGO/term/\1">\1</a>'),

            (r'\b(K\d{5})\b', r'<a href="https://www.kegg.jp/entry/\1">KEGG \1</a>'),
            # KEGG Pathway Identifier mapping
            (r'\b([a-z]{2,4}\d{5})\b', r'<a href="https://www.kegg.jp/pathway/\1">KEGG Pathway \1</a>'),

            # InterPro (IPR) - 新增
            (r'\b(IPR\d{6})\b',
             r'<a href="https://www.ebi.ac.uk/interpro/entry/InterPro/\1/">\1</a>'),

            # ChEMBL Target ID 映射
            (r'\b(CHEMBL\d+)\b', r'<a href="https://www.ebi.ac.uk/chembl/target_report_card/\1">\1</a>'),

            # ChEBI ID 映射
            (r'\b(CHEBI:\d+)\b', r'<a href="https://www.ebi.ac.uk/chebi/searchId.do?chebiId=\1">\1</a>'),

            # 拟南芥 AGI 基因号
            (r'\b(AT[1-5CM]G\d{5})\b',
             r'<a href="https://plants.ensembl.org/Arabidopsis_thaliana/Gene/Summary?g=\1">\1</a>'),

            # JASPAR Motif ID (例如: MA0001.1)
            (r'\b(MA\d{4}\.\d+)\b', r'<a href="https://jaspar.elixir.no/matrix/\1/">JASPAR \1</a>'),

            # SNP / Variation ID (例如: rs1234567)
            (r'\b(rs\d+)\b', r'<a href="https://www.ensembl.org/Variation/Explore?v=\1">SNP \1</a>'),



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

        # 全局清理不需要的运行标识与文字
        text = re.sub(r"\[CLEAR_SEARCH\]|\[START_LLM_NETWORK\]|\[\s*FOLLOW[_-]?\s*UPS?\s*\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\[AI is reasoning in the background\.\.\.\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Initializing\.\.\.", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Reasoning & Tool Execution", "", text, flags=re.IGNORECASE)

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


    @staticmethod
    def format_response(text, index, expanded_indices, user_toggled_thinks, mermaid_cache):
        """统一处理包含 Mermaid 图表和 Think 面板的对话渲染"""
        if not text:
            return ""

        tm = ThemeManager()
        pattern = r'```mermaid\s*\n(.*?)\n```'

        def repl_mermaid(match):
            code = match.group(1).strip()
            code_hash = hashlib.md5(code.encode('utf-8')).hexdigest()
            mermaid_cache[code_hash] = code  # 存入外部传入的字典中
            return (
                f"<br><div style='padding:12px; margin: 8px 0; border:1px solid {tm.color('accent')}; border-radius:6px; background-color: transparent;'>"
                f"<div style='margin-bottom: 5px;'><b>Mermaid Diagram Generated</b></div>"
                f"<a href='mermaid://view?hash={code_hash}' style='color:{tm.color('accent')}; text-decoration:none; font-weight:bold;'>"
                f"Click here to view / edit interactive diagram</a></div><br>")

        processed_text = re.sub(pattern, repl_mermaid, text, flags=re.DOTALL | re.IGNORECASE)
        return TextFormatter.format_chat_text(processed_text, index, expanded_indices, user_toggled_thinks)

    @staticmethod
    def handle_link_click(url, parent_widget, mermaid_cache, user_toggled_thinks, expanded_indices,
                          render_callback=None):
        """统一分发系统的自定义链接路由 (mermaid://, think://, cite:// 等)"""
        from PySide6.QtWidgets import QWidget
        from PySide6.QtCore import QUrlQuery, QUrl
        from PySide6.QtGui import QDesktopServices
        import os, tempfile, shutil

        qt_parent = parent_widget if isinstance(parent_widget, QWidget) else None

        scheme = url.scheme()
        query = QUrlQuery(url)

        # 1. 处理 Mermaid 图表
        if scheme == "mermaid":
            code_hash = query.queryItemValue("hash")
            code = mermaid_cache.get(code_hash, "")
            if code:
                if getattr(parent_widget, 'mermaid_viewer', None) is None:
                    from src.ui.components.mermaid_viewer import MermaidViewer
                    parent_widget.mermaid_viewer = MermaidViewer(qt_parent)
                parent_widget.mermaid_viewer.load_diagram(code)
            else:
                ToastManager().show("Diagram data lost. Please ask the AI to generate it again.", "error")
            return

        # 2. 处理 Think 折叠面板
        if scheme == "think":
            # 兼容 host 或 path (应对归一化)
            action = url.host() if url.host() else url.path().strip('/')
            idx_str = query.queryItemValue("index")
            idx = int(idx_str) if idx_str and idx_str.lstrip('-').isdigit() else -1

            if idx != -1:
                user_toggled_thinks.add(idx)
                if action == 'expand':
                    expanded_indices.add(idx)
                else:
                    expanded_indices.discard(idx)

                if render_callback:
                    render_callback(idx)
            return

        # 3. 处理文献/PDF引用跳转
        if scheme == "cite":
            file_path = query.queryItemValue("path")

            if file_path.startswith(("http://", "https://")):
                QDesktopServices.openUrl(QUrl(file_path))
                return

            text_snippet = query.queryItemValue("text")
            source_name = query.queryItemValue("name")

            if os.path.exists(file_path):
                ext = source_name.lower().split('.')[-1] if '.' in source_name else ""

                if ext == 'pdf':
                    from src.ui.components.pdf_viewer import InternalPDFViewer
                    if getattr(parent_widget, 'pdf_viewer', None) is None:
                        parent_widget.pdf_viewer = InternalPDFViewer(qt_parent)
                    parent_widget.pdf_viewer.load_document(file_path, 0, text_snippet, display_name=source_name)

                elif ext in ['md', 'txt', 'csv', 'json']:
                    from src.ui.components.pdf_viewer import InternalTextViewer
                    if getattr(parent_widget, 'text_viewer', None) is None:
                        parent_widget.text_viewer = InternalTextViewer(qt_parent)
                    parent_widget.text_viewer.load_document(file_path, text_snippet, display_name=source_name)

                else:
                    temp_dir = tempfile.gettempdir()
                    safe_name = source_name if source_name else "document.bin"
                    temp_file_path = os.path.join(temp_dir, f"scholar_navis_view_{safe_name}")
                    try:
                        shutil.copy2(file_path, temp_file_path)
                        QDesktopServices.openUrl(QUrl.fromLocalFile(temp_file_path))
                    except Exception as e:
                        ToastManager().show(f"Failed to invoke external program: {str(e)}", "error")
            else:
                ToastManager().show(f"File not found: {source_name or file_path}", "error")
            return

        # 4. 普通网络链接交由系统默认浏览器
        url_str = url.toString() if hasattr(url, 'toString') else str(url)
        QDesktopServices.openUrl(QUrl(url_str))


