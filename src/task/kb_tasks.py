import logging
import os
import sys
import shutil
import time
import zipfile
import json
import uuid

from chromadb.utils import embedding_functions
from huggingface_hub import snapshot_download

from src.core.config_manager import ConfigManager
from src.core.core_task import BackgroundTask, TaskState
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager
from src.core.models_registry import get_model_conf

logger = logging.getLogger("Task.kb")

def _setup_worker_env():
    import os
    import sys

    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    os.environ["ANONYMIZED_TELEMETRY"] = "False"

    try:
        from src.core.network_worker import setup_global_network_env
        setup_global_network_env()
    except Exception as e:
        import logging
        logging.getLogger("WorkerEnv").warning(f"Failed to setup network env in child process: {e}")

    return base_dir


def _worker_load_model(kb_id):
    logger = logging.getLogger("Worker.ModelLoader")


    kb_mgr = KBManager()
    dev_mgr = DeviceManager()
    kb_data = kb_mgr.get_kb_by_id(kb_id)
    if not kb_data:
        raise RuntimeError(f"未找到 KB ID: {kb_id} 的元数据")

    model_id = kb_data.get('model_id', 'embed_auto')
    conf = get_model_conf(model_id, "embedding")

    if conf and conf.get('is_network', False):
        logger.info(f"Using Network Embedding API: {conf.get('hf_repo_id')}")
        from src.core.network_worker import NetworkEmbeddingFunction
        sys_cfg = ConfigManager().user_settings
        return NetworkEmbeddingFunction(
            api_url=sys_cfg.get("network_embed_url", "https://api.openai.com"),
            api_key=sys_cfg.get("network_embed_key", ""),
            model_name=conf.get('hf_repo_id')  # 通常填模型名称，如 text-embedding-3-small
        )

    device_info = dev_mgr.get_optimal_device()
    device = device_info.get('type', 'cpu') if isinstance(device_info, dict) else str(device_info)
    repo_id = conf['hf_repo_id'] if conf else "sentence-transformers/all-MiniLM-L6-v2"

    try:
        model_path = snapshot_download(
            repo_id=repo_id,
            local_files_only=True
        )

        logger.info(f"Cache verification successful. Path: {model_path}")

        embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=model_path,
            device=device,
            normalize_embeddings=True
        )
        return embed_fn

    except Exception as e:
        error_info = f"FATAL: Model {repo_id} not found in system default cache."
        logger.error(f"{error_info} | {e}")
        raise RuntimeError(error_info)

class ImportFilesTask(BackgroundTask):
    def _execute(self):
        # 1. 隔离与初始化环境变量（防 Windows 多进程陷阱）
        _setup_worker_env()

        import os
        import uuid
        import shutil
        import tempfile
        from src.core.kb_manager import KBManager
        from src.core.database import DatabaseManager
        from src.services.file_service import FileService
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        # 引入动态参数与配置管理
        from src.core.models_registry import get_optimal_chunk_settings
        from src.core.config_manager import ConfigManager

        kb_id = self.kwargs.get('kb_id')
        files = self.kwargs.get('files', [])
        is_rebuild = self.kwargs.get('is_rebuild', False)

        kb_mgr = KBManager()
        db_mgr = DatabaseManager()

        self.send_log("INFO", "🧠 Loading AI Model...")
        embed_fn = _worker_load_model(kb_id)

        # 连接数据库
        if not db_mgr.switch_kb(kb_id, embedding_function=embed_fn):
            raise RuntimeError("Failed to connect to ChromaDB.")

        process_tasks = []

        # 2. 处理重建逻辑或增量导入逻辑
        if is_rebuild:
            self.send_log("WARNING", "🧹 Rebuilding vector database...")
            try:
                db_mgr.client.delete_collection("main_collection")
                kb_info = kb_mgr.get_kb_by_id(kb_id)
                db_mgr.collection = db_mgr.client.get_or_create_collection(
                    name="main_collection",
                    embedding_function=embed_fn,
                    metadata={"kb_name": kb_info['name']}
                )
            except Exception:
                pass

            all_docs = kb_mgr.get_kb_files(kb_id)
            process_tasks = [(d['path'], d['name']) for d in all_docs]
        else:
            for fp in files:
                result = kb_mgr.import_file_to_kb(kb_id, fp)
                if result:
                    process_tasks.append(result)

        # 3. 动态获取最优切分参数（木桶原理）
        kb_info = kb_mgr.get_kb_by_id(kb_id)
        current_embed_id = kb_info.get('model_id', 'embed_auto') if kb_info else 'embed_auto'
        current_rerank_id = ConfigManager().user_settings.get("rerank_model_id", "rerank_auto")

        opt_chunk, opt_overlap, opt_batch = get_optimal_chunk_settings(current_embed_id, current_rerank_id)
        self.send_log("INFO", f"📐 动态切分策略应用: Chunk={opt_chunk}, Overlap={opt_overlap}, Batch={opt_batch}")

        # 🌟 4. 初始化基础文本切分器（应用动态参数）
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=opt_chunk,
            chunk_overlap=opt_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
            length_function=len
        )

        # 初始化 PDF 引擎
        pdf_engine = None
        try:
            from src.core.pdf_engine import PDFEngine
            pdf_engine = PDFEngine()

            # 日志劫持（将子模块底层的 logger 信息转发回 UI 进度条界面）
            class LoggerHijacker:
                def __init__(self, task_ref): self.task_ref = task_ref

                def debug(self, msg): pass

                def info(self, msg):
                    if "Vectorized" in msg or "Loaded" in msg:
                        self.task_ref.send_log("INFO", f"📄 {msg}")

                def warning(self, msg): self.task_ref.send_log("WARNING", f"{msg}")

                def error(self, msg): self.task_ref.send_log("ERROR", f"底层崩溃: {msg}")

            pdf_engine.logger = LoggerHijacker(self)
        except ImportError as e:
            self.send_log("ERROR", f"PDFEngine 导入失败 (缺库?): {e}")

        total = len(process_tasks)
        success_count = 0
        temp_dir = tempfile.gettempdir()

        # 5. 核心循环解析
        for i, (read_path, source_name) in enumerate(process_tasks):
            if not os.path.exists(read_path): continue
            pct = int((i / total) * 100)
            self.update_progress(pct, f"Indexing: {source_name}")

            try:
                #  PDF 智能分流
                if source_name.lower().endswith('.pdf') and pdf_engine:
                    # 制造物理替身欺骗 LangChain
                    temp_pdf_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}_{source_name}")
                    shutil.copy2(read_path, temp_pdf_path)

                    try:
                        #  动态参数注入 process_pdf
                        chunks_count = pdf_engine.process_pdf(
                            temp_pdf_path,
                            real_filename=source_name,
                            chunk_size=opt_chunk,
                            chunk_overlap=opt_overlap,
                            batch_size=opt_batch
                        )
                        if chunks_count > 0:
                            success_count += 1
                        else:
                            self.send_log("WARNING", f"⚠️ {source_name} 提取了 0 个字符。")
                    finally:
                        # 拔x无情：清理替身，不留垃圾
                        if os.path.exists(temp_pdf_path):
                            os.remove(temp_pdf_path)

                # 其他格式文档走基础切分
                else:
                    text_content = FileService.read_file_content(read_path)
                    if not text_content or len(text_content.strip()) < 10:
                        self.send_log("WARNING", f"Skipped empty/unreadable file: {source_name}")
                        continue

                    chunks = text_splitter.split_text(text_content)
                    if not chunks: continue

                    ids = [f"{source_name}_{k}_{uuid.uuid4().hex[:6]}" for k in range(len(chunks))]
                    metadatas = [{"source": source_name, "chunk_id": k} for k in range(len(chunks))]
                    for j in range(0, len(chunks), opt_batch):
                        batch_chunks = chunks[j:j + opt_batch]
                        batch_ids = ids[j:j + opt_batch]
                        batch_metas = metadatas[j:j + opt_batch]
                        db_mgr.add_documents(documents=batch_chunks, metadatas=batch_metas, ids=batch_ids)
                    success_count += 1

            except Exception as e:
                self.send_log("ERROR", f"Failed to index {source_name}: {str(e)}")

        # 6. 收尾工作：更新时间戳、强制 WAL 落盘
        kb_mgr._touch_meta(os.path.join(kb_mgr.WORKSPACE_DIR, kb_id))
        self.send_log("INFO", f"Indexing complete. Processed {success_count}/{total} files.")

        if getattr(db_mgr, 'client', None):
            try:
                db_mgr.client._system.stop()
            except Exception:
                pass

        # 释放模型显存
        del embed_fn
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


class DeleteFilesTask(BackgroundTask):
    def _execute(self):
        # 1. 进门防隔离
        _setup_worker_env()

        from src.core.kb_manager import KBManager
        from src.core.database import DatabaseManager

        kb_id = self.kwargs.get('kb_id')
        file_names = self.kwargs.get('file_names', [])
        kb_mgr = KBManager()
        db_mgr = DatabaseManager()

        # 2. 清理物理文件和 JSON 映射
        kb_data = kb_mgr.get_kb_by_id(kb_id)
        file_map = kb_data.get('file_map', {})

        to_delete_uuids = [k for k, v in file_map.items() if v in file_names]
        for uuid_name in to_delete_uuids:
            fp = os.path.join(kb_mgr.WORKSPACE_DIR, kb_id, "documents", uuid_name)
            if os.path.exists(fp): os.remove(fp)
            del file_map[uuid_name]

        kb_mgr._update_meta_field(kb_id, "file_map", file_map)

        # 3. 清理向量库中的数据
        embed_fn = _worker_load_model(kb_id)
        if db_mgr.switch_kb(kb_id, embedding_function=embed_fn):
            total = len(file_names)
            for i, fname in enumerate(file_names):
                self.update_progress(int((i / total) * 100), f"Deleting vectors for {fname}...")
                db_mgr.delete_by_source(fname)

            #4. 强制落盘保险
            if getattr(db_mgr, 'client', None):
                try:
                    db_mgr.client._system.stop()
                except Exception:
                    pass

        kb_mgr._touch_meta(os.path.join(kb_mgr.WORKSPACE_DIR, kb_id))
        self.send_log("INFO", f"Successfully deleted {len(file_names)} files.")


class RenameFilesTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()
        from src.core.kb_manager import KBManager
        from src.core.database import DatabaseManager

        kb_id = self.kwargs.get('kb_id')
        renames = self.kwargs.get('renames', {})
        kb_mgr = KBManager()
        db_mgr = DatabaseManager()

        # 1. 更新 JSON 映射
        kb_data = kb_mgr.get_kb_by_id(kb_id)
        file_map = kb_data.get('file_map', {})
        for k, v in file_map.items():
            if v in renames: file_map[k] = renames[v]
        kb_mgr._update_meta_field(kb_id, "file_map", file_map)

        # 2. 更新向量库中的 metadata
        embed_fn = _worker_load_model(kb_id)
        if db_mgr.switch_kb(kb_id, embedding_function=embed_fn) and db_mgr.collection:
            total = len(renames)
            for i, (old_name, new_name) in enumerate(renames.items()):
                self.update_progress(int((i / total) * 100), f"Updating index: {old_name} -> {new_name}")
                try:
                    existing = db_mgr.collection.get(where={"source": old_name})
                    if existing and existing['ids']:
                        new_metadatas = existing['metadatas']
                        for m in new_metadatas: m['source'] = new_name
                        db_mgr.collection.update(ids=existing['ids'], metadatas=new_metadatas)
                except Exception as e:
                    self.send_log("WARNING", f"Vector metadata update failed for {old_name}: {e}")

            #  3. 强制落盘保险
            if getattr(db_mgr, 'client', None):
                try:
                    db_mgr.client._system.stop()
                except Exception:
                    pass

        kb_mgr._touch_meta(os.path.join(kb_mgr.WORKSPACE_DIR, kb_id))
        self.send_log("INFO", f"✅ Successfully renamed {len(renames)} files.")


class ExportKBTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()
        from src.core.kb_manager import KBManager
        kb_id = self.kwargs.get('kb_id')
        dest_path = self.kwargs.get('dest_path')
        kb_mgr = KBManager()
        src_dir = os.path.join(kb_mgr.WORKSPACE_DIR, kb_id)
        if not os.path.exists(src_dir): raise FileNotFoundError("Not found")
        self.send_log("INFO", f"📦 Packing Knowledge Base {kb_id}...")
        with zipfile.ZipFile(dest_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(src_dir):
                for file in files:
                    if file.endswith('.lock') or file.endswith('.tmp'): continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, start=os.path.dirname(src_dir))
                    zipf.write(file_path, arcname)
                    self.update_progress(0, f"Packing: {file}")
        self.send_log("INFO", f"✅ Export successful: {dest_path}")


class ImportExternalKBTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()
        from src.core.kb_manager import KBManager
        bundle_path = self.kwargs.get('bundle_path')
        kb_mgr = KBManager()
        self.send_log("INFO", "Extracting project bundle...")
        temp_extract_dir = os.path.join(kb_mgr.WORKSPACE_DIR, "temp_import")
        if os.path.exists(temp_extract_dir): shutil.rmtree(temp_extract_dir)
        os.makedirs(temp_extract_dir)
        try:
            with zipfile.ZipFile(bundle_path, 'r') as zipf:
                zipf.extractall(temp_extract_dir)
            found_root = None
            for root, dirs, files in os.walk(temp_extract_dir):
                if "meta.json" in files:
                    found_root = root
                    break
            if not found_root: raise ValueError("Invalid Project Bundle")

            with open(os.path.join(found_root, "meta.json"), 'r', encoding='utf-8') as f:
                info = json.load(f)
            kb_id = info.get('id')
            if not kb_id: raise ValueError("Missing ID")
            target_dir = os.path.join(kb_mgr.WORKSPACE_DIR, kb_id)
            if os.path.exists(target_dir):
                new_id = str(uuid.uuid4())
                info['id'] = new_id
                info['name'] = f"{info['name']} (Imported)"
                kb_id = new_id
                target_dir = os.path.join(kb_mgr.WORKSPACE_DIR, kb_id)
                with open(os.path.join(found_root, "meta.json"), 'w', encoding='utf-8') as f: json.dump(info, f)
            shutil.move(found_root, target_dir)

            self.send_log("INFO", f"Project imported successfully as '{info['name']}'")
        finally:
            if os.path.exists(temp_extract_dir): shutil.rmtree(temp_extract_dir)


class SwitchKBTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()
        from src.core.kb_manager import KBManager
        from src.core.models_registry import get_model_conf, check_model_exists
        kb_id = self.kwargs.get('kb_id')
        self.send_log("INFO", "Checking model requirements...")
        kb_mgr = KBManager()
        kb_data = kb_mgr.get_kb_by_id(kb_id)
        if not kb_data: return
        model_id = kb_data.get('model_id')
        if not model_id: return
        conf = get_model_conf(model_id, "embedding")
        if not conf: return
        if not check_model_exists(conf['hf_repo_id']): raise FileNotFoundError("Model missing.")
        self.send_log("INFO", "Model is ready.")