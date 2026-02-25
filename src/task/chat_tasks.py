import os
import base64
import mimetypes
from urllib.parse import quote
from src.core.core_task import BackgroundTask


class ProcessAttachmentTask(BackgroundTask):
    def _execute(self):
        paths = self.kwargs.get('paths', [])
        chunks = []
        html = ""

        total = len(paths)
        for i, path in enumerate(paths):
            f_name = os.path.basename(path)
            ext = path.lower()

            try:
                # 1. 图片处理
                if ext.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')):
                    self.update_progress(int((i / total) * 100), f"Encoding image: {f_name}...")
                    with open(path, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        mime_type, _ = mimetypes.guess_type(path)
                        mime_type = mime_type or 'image/jpeg'

                        chunks.append({
                            "path": path, "name": f_name, "page": 1,
                            "type": "image",
                            "base64_url": f"data:{mime_type};base64,{encoded_string}",
                            "content": f"[Image Attached: {f_name}]"
                        })

                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>🖼️ {f_name}</a></div>"

                # 2. PDF 处理
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

                # 3. 纯文本处理
                else:
                    self.update_progress(int((i / total) * 100), f"Reading file: {f_name}...")
                    with open(path, 'r', encoding='utf-8') as f:
                        text = f.read().strip()
                        if text:
                            chunks.append({
                                "path": path, "name": f_name, "page": 1, "content": text
                            })
                    link = f"cite://view?path={quote(path)}&page=1&name={quote(f_name)}"
                    html += f"<div style='margin-bottom: 4px;'>▪ <a href='{link}' style='color:#05B8CC; text-decoration:none;'>📄 {f_name}</a></div>"

            except Exception as e:
                self.send_log("ERROR", f"Failed to process {f_name}: {e}")
                raise e

        self.update_progress(100, "Processing complete.")

        # 组装结果并通过队列安全返回给主线程
        return {
            "chunks": chunks,
            "html": html
        }