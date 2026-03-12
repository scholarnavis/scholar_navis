import os
import base64
import mimetypes
from urllib.parse import quote

import chardet

from src.core.core_task import BackgroundTask


class ProcessAttachmentTask(BackgroundTask):
    def _execute(self):
        file_infos = self.kwargs.get('file_infos', [])

        if not file_infos:
            paths = self.kwargs.get('paths', [])
            file_infos = [{"path": p, "name": os.path.basename(p)} for p in paths]

        chunks = []
        html = ""
        total = len(file_infos)

        for i, info in enumerate(file_infos):
            if self.is_cancelled():
                self.send_log("INFO", "Attachment processing cancelled by user.")
                break

            path = info['path']
            f_name = info['name']

            ext = f_name.lower()

            try:
                # 1. Image processing
                if ext.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')):
                    self.update_progress(int((i / total) * 100), f"Encoding image: {f_name}...")
                    with open(path, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        mime_type, _ = mimetypes.guess_type(f_name)
                        mime_type = mime_type or 'image/jpeg'

                        chunks.append({
                            "path": path, "name": f_name, "page": 1,
                            "type": "image",
                            "base64_url": f"data:{mime_type};base64,{encoded_string}",
                            "content": f"[Image Attached: {f_name}]"
                        })

                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>🖼️ {f_name}</a></div>"

                # 2. PDF processing
                elif ext.endswith('.pdf'):
                    self.update_progress(int((i / total) * 100), f"Parsing PDF: {f_name}...")
                    import pymupdf4llm
                    md_chunks = pymupdf4llm.to_markdown(path, page_chunks=True)
                    for chunk in md_chunks:
                        text = chunk.get("text", "").strip()
                        if len(text) > 10:
                            chunks.append({
                                "path": path, "name": f_name, "page": chunk.get("metadata", {}).get("page", 1),
                                "content": text
                            })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

                # 2.5 DOCX processing
                elif ext.endswith('.docx'):
                    self.update_progress(int((i / total) * 100), f"Parsing DOCX: {f_name}...")
                    import docx
                    doc = docx.Document(path)

                    text = "\n".join([paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()])

                    if len(text) > 10:
                        chunks.append({
                            "path": path, "name": f_name, "page": 1,
                            "content": text
                        })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

                # 2.6 老旧 DOC 拦截
                elif ext.endswith('.doc'):
                    self.send_log("ERROR",
                                  f"Legacy .doc format is not natively supported. Please convert {f_name} to .docx")
                    # 给用户一个 HTML 提示，不用提取内容
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#ffb86c; text-decoration:none;'>⚠️ {f_name} (Please convert to .docx)</a></div>"



                # 3. Text processing
                else:
                    self.update_progress(int((i / total) * 100), f"Reading file: {f_name}...")
                    text = ""

                    with open(path, 'rb') as f:
                        raw_data = f.read()
                        detected = chardet.detect(raw_data)

                        # 如果文件太小或特征不明显导致检测失败，默认回退到 utf-8
                        encoding = detected['encoding'] if detected['encoding'] else 'utf-8'
                        text = raw_data.decode(encoding, errors='replace').strip()

                    if text:
                        chunks.append({
                            "path": path, "name": f_name, "page": 1, "content": text
                        })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

            except Exception as e:
                self.send_log("ERROR", f"Failed to process {f_name}: {e}")
                raise e

        self.update_progress(100, "Finalizing...")

        import json
        import tempfile
        import uuid

        result_dict = {"chunks": chunks, "html": html}
        temp_file_path = os.path.join(tempfile.gettempdir(), f"task_payload_{uuid.uuid4().hex}.json")

        with open(temp_file_path, 'w', encoding='utf-8') as f:
            json.dump(result_dict, f, ensure_ascii=False)

        return {"_is_temp_file": True, "path": temp_file_path}