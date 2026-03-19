import json
import logging
import os
import shutil
import uuid
import zipfile
import onnxruntime as ort
import torch
import torch.nn.functional as F
from chromadb import Documents, Embeddings, EmbeddingFunction
from optimum.onnxruntime import ORTModelForFeatureExtraction
from transformers import AutoTokenizer
from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager, DatabaseManager
from src.core.models_registry import get_model_conf, ensure_onnx_model
from src.core.rerank_engine import RerankEngine

logger = logging.getLogger("Task.kb")

def _setup_worker_env():
    import os
    import sys

    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    models_dir = os.path.join(base_dir, "models")
    os.environ["HF_HOME"] = models_dir
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = models_dir

    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    os.environ["HF_HUB_OFFLINE"] = "1"


    try:
        from src.core.network_worker import setup_global_network_env
        setup_global_network_env()
    except Exception as e:
        import logging
        logging.getLogger("WorkerEnv").warning(f"Failed to setup network env in child process: {e}")

    return base_dir


# --- kb_tasks.py ---

def _worker_load_model(kb_id, config):
    logger = logging.getLogger("Worker.ModelLoader")
    kb_mgr = KBManager()
    dev_mgr = DeviceManager()
    kb_data = kb_mgr.get_kb_by_id(kb_id)
    if not kb_data:
        raise RuntimeError(f"Metadata not found for KB ID: {kb_id}")
    user_device = config.user_settings.get("inference_device", "auto")
    device_str = dev_mgr.parse_device_string(user_device)
    model_id = kb_data.get('model_id', 'embed_auto')
    conf = get_model_conf(model_id, "embedding")
    repo_id = conf['hf_repo_id'] if conf else "sentence-transformers/all-MiniLM-L6-v2"
    try:
        onnx_dir = ensure_onnx_model(repo_id, "embedding")

        logger.info(f"Loading Embedding Model: {repo_id} on {device_str}")

        return ONNXEmbeddingFunction(onnx_dir, device=device_str)
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise RuntimeError(f"Model Load Failed: {str(e)}")


class ONNXEmbeddingFunction(EmbeddingFunction):

    def __init__(self, onnx_cache_dir, device="cpu"):
        logger = logging.getLogger("Worker.ONNXProvider")

        self.tokenizer = AutoTokenizer.from_pretrained(onnx_cache_dir, local_files_only=True)
        available_providers = ort.get_available_providers()

        provider = "CPUExecutionProvider"
        provider_options = None
        device_str = str(device).lower()

        logger.info(f"ONNX Init Requested Device: {device_str}")
        logger.info(f"Available ONNX Providers in Env: {available_providers}")

        if device_str.startswith("cuda") and "CUDAExecutionProvider" in available_providers:
            provider = "CUDAExecutionProvider"
            if ":" in device_str:
                provider_options = {'device_id': int(device_str.split(":")[1])}

        elif device_str.startswith("dml") and "DmlExecutionProvider" in available_providers:
            provider = "DmlExecutionProvider"
            if ":" in device_str:
                provider_options = {'device_id': int(device_str.split(":")[1])}

        elif device_str.startswith("rocm") and "ROCmExecutionProvider" in available_providers:
            provider = "ROCmExecutionProvider"
            if ":" in device_str:
                provider_options = {'device_id': int(device_str.split(":")[1])}

        elif device_str.startswith("coreml") and "CoreMLExecutionProvider" in available_providers:
            provider = "CoreMLExecutionProvider"

        elif device_str == "auto":
            if "CUDAExecutionProvider" in available_providers:
                provider = "CUDAExecutionProvider"
            elif "DmlExecutionProvider" in available_providers:
                provider = "DmlExecutionProvider"
            elif "ROCmExecutionProvider" in available_providers:
                provider = "ROCmExecutionProvider"
            elif "CoreMLExecutionProvider" in available_providers:
                provider = "CoreMLExecutionProvider"

        logger.info(f"Final Selected ONNX Provider: {provider}")
        logger.info(f"Provider Options: {provider_options}")

        kwargs = {"provider": provider}
        if provider_options:
            kwargs["provider_options"] = provider_options

        self.model = ORTModelForFeatureExtraction.from_pretrained(
            onnx_cache_dir,
            export=False,
            local_files_only=True,
            **kwargs
        )

        actual_providers = self.model.providers
        if provider != "CPUExecutionProvider" and actual_providers and actual_providers[0] == "CPUExecutionProvider":
            fallback_msg = f"CRITICAL: Silent fallback detected! Requested '{provider}' but ONNX Runtime forced 'CPUExecutionProvider'. Hardware acceleration failed."
            logger.error(fallback_msg)
            raise RuntimeError(fallback_msg)

    def __call__(self, input: Documents) -> Embeddings:
        if not input:
            return []

        if isinstance(input, str):
            texts = [input]
        elif isinstance(input, list):
            texts = [str(item) for item in input]
        elif isinstance(input, dict):
            texts = [str(v) for v in input.values()]
        else:
            texts = [str(input)]

        inputs = self.tokenizer(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)

        try:
            outputs = self.model(**inputs)
            embeddings = outputs.last_hidden_state[:, 0, :]
        except KeyError as e:

            logging.getLogger("Worker.ONNXProvider").debug(f"Optimum mapping failed, bypassing to raw ONNX: {e}")

            ort_inputs = {k: v.cpu().numpy() for k, v in inputs.items()}
            raw_outputs = self.model.model.run(None, ort_inputs)

            emb_array = raw_outputs[0]
            if len(emb_array.shape) == 3:
                emb_array = emb_array[:, 0, :]

            embeddings = torch.tensor(emb_array)

        embeddings = F.normalize(embeddings, p=2, dim=1)
        return embeddings.tolist()


class RerankTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()
        query = self.kwargs.get("query")
        docs = self.kwargs.get("docs")
        domain = self.kwargs.get("domain", "General Academic")
        top_k = self.kwargs.get("top_k", 8)

        self.send_log("INFO", "Starting isolated Reranker process to bypass GIL...")

        # This executes in a separate process, completely freeing the Main UI
        engine = RerankEngine()
        ranked_docs = engine.rerank(query, docs, domain=domain, top_k=top_k)

        return ranked_docs



class ImportFilesTask(BackgroundTask):
    def _execute(self):
        # 1. 隔离与初始化环境变量（防 Windows 多进程陷阱）
        _setup_worker_env()

        import os
        import uuid
        import shutil
        import tempfile
        import traceback
        from src.core.kb_manager import KBManager
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

        self.send_log("INFO", "Loading AI Model...")
        self.send_log("INFO", f"Task kwargs: files_count={len(files)}, is_rebuild={is_rebuild}")

        from src.core.config_manager import ConfigManager
        config = ConfigManager()
        embed_fn = _worker_load_model(kb_id, config)

        try:
            # 连接数据库
            if not db_mgr.switch_kb(kb_id, embedding_function=embed_fn):
                raise RuntimeError("Failed to connect to ChromaDB.")

            process_tasks = []

            # 2. 处理重建逻辑或增量导入逻辑
            if is_rebuild:
                self.send_log("WARNING", "Rebuilding vector database...")
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
            current_rerank_id = config.user_settings.get("rerank_model_id", "rerank_auto")

            opt_chunk, opt_overlap, opt_batch = get_optimal_chunk_settings(current_embed_id, current_rerank_id)
            self.send_log("INFO", f"Chunk settings applied: Chunk={opt_chunk}, Overlap={opt_overlap}, Batch={opt_batch}")

            # 4. 初始化基础文本切分器
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=opt_chunk,
                chunk_overlap=opt_overlap,
                separators=["\n\n", "\n", ". ", " ", ""],
                length_function=len
            )

            # 初始化 PDF 引擎，扩大异常捕获范围，抛出底层信息
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
                            self.task_ref.send_log("INFO", msg)

                    def warning(self, msg): self.task_ref.send_log("WARNING", msg)

                    def error(self, msg): self.task_ref.send_log("ERROR", f"PDFEngine Error: {msg}")

                pdf_engine.logger = LoggerHijacker(self)
            except Exception as e:
                self.send_log("ERROR", f"PDFEngine import or initialization failed: {e}")
                self.send_log("ERROR", f"Traceback:\n{traceback.format_exc()}")

            total = len(process_tasks)
            self.send_log("INFO", f"Actual tasks to process: {total}")
            success_count = 0
            temp_dir = tempfile.gettempdir()

            # 5. 核心循环解析
            for i, (read_path, source_name) in enumerate(process_tasks):
                if self.is_cancelled():
                    self.send_log("WARNING", "Task cancelled by user, safely aborting...")
                    raise InterruptedError("Task was safely terminated by the user.")

                if not os.path.exists(read_path): continue
                pct = int((i / total) * 100)
                self.update_progress(pct, f"Indexing: {source_name}")
                self.send_log("INFO", f"Processing item {i}: {source_name} at {read_path}")

                try:
                    # PDF 智能分流
                    if source_name.lower().endswith('.pdf') and pdf_engine:
                        # 制造物理替身欺骗 LangChain
                        temp_pdf_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}_{source_name}")
                        shutil.copy2(read_path, temp_pdf_path)

                        try:
                            # 动态参数注入 process_pdf
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
                                self.send_log("WARNING", f"{source_name} extracted 0 characters.")
                        finally:

                            if getattr(db_mgr, 'client', None):
                                try:
                                    db_mgr.client._system.stop()
                                except Exception:
                                    pass

                            # 清理替身
                            if os.path.exists(temp_pdf_path):
                                os.remove(temp_pdf_path)

                    # 其他格式文档走基础切分
                    else:
                        text_content = FileService.read_file_content(read_path)

                        if source_name.lower().endswith('.doc'):
                            self.send_log("WARNING", f"Skipping {source_name}: Legacy .doc is not supported for indexing.")
                            continue

                        if not text_content or len(text_content.strip()) < 10:
                            self.send_log("WARNING", f"Skipped empty/unreadable file: {source_name}")
                            continue

                        chunks = text_splitter.split_text(text_content)
                        if not chunks: continue

                        ids = [f"{source_name}_{k}_{uuid.uuid4().hex[:6]}" for k in range(len(chunks))]
                        metadatas = [{
                            "source": source_name,
                            "chunk_id": k,
                            "page": 1,
                            "file_path": read_path
                        } for k in range(len(chunks))]

                        for j in range(0, len(chunks), opt_batch):
                            batch_chunks = chunks[j:j + opt_batch]
                            batch_ids = ids[j:j + opt_batch]
                            batch_metas = metadatas[j:j + opt_batch]
                            db_mgr.add_documents(documents=batch_chunks, metadatas=batch_metas, ids=batch_ids)
                        success_count += 1

                except Exception as e:
                    self.send_log("ERROR", f"Failed to index {source_name}: {str(e)}")
                    self.send_log("ERROR", f"Task Inner Loop Error Traceback:\n{traceback.format_exc()}")

            kb_mgr._touch_meta(os.path.join(kb_mgr.WORKSPACE_DIR, kb_id))
            self.send_log("INFO", f"Indexing complete. Processed {success_count}/{total} files.")

        finally:
            self.send_log("INFO", "Releasing model memory and database locks...")

            if getattr(db_mgr, 'client', None):
                try:
                    db_mgr.client._system.stop()
                except Exception:
                    pass

            if 'embed_fn' in locals() and embed_fn:
                if hasattr(embed_fn, 'model'):
                    del embed_fn.model
                del embed_fn

            import gc
            gc.collect()

            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    self.send_log("INFO", "CUDA cache cleared.")
            except Exception:
                pass


class DeleteFilesTask(BackgroundTask):
    def _execute(self):
        # 1. 进门防隔离
        _setup_worker_env()

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
        if db_mgr.switch_kb(kb_id, embedding_function=None):
            try:
                total = len(file_names)
                for i, fname in enumerate(file_names):
                    if self.is_cancelled():
                        raise InterruptedError("Task was safely terminated by the user.")
                    self.update_progress(int((i / total) * 100), f"Deleting vectors for {fname}...")
                    db_mgr.delete_by_source(fname)

            finally:
                if getattr(db_mgr, 'client', None):
                    try:
                        db_mgr.client._system.stop()
                    except Exception:
                        pass

        kb_mgr._touch_meta(os.path.join(kb_mgr.WORKSPACE_DIR, kb_id))
        self.send_log("INFO", f"Successfully deleted {len(file_names)} files (No model loaded).")


class RenameFilesTask(BackgroundTask):
    def _execute(self):
        _setup_worker_env()

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
        if db_mgr.switch_kb(kb_id, embedding_function=None) and db_mgr.collection:
            try:
                total = len(renames)
                for i, (old_name, new_name) in enumerate(renames.items()):
                    if self.is_cancelled():
                        raise InterruptedError("Task was safely terminated by the user.")
                    self.update_progress(int((i / total) * 100), f"Updating index: {old_name} -> {new_name}")

                    try:
                        existing = db_mgr.collection.get(where={"source": old_name}, include=['metadatas'])
                        if existing and existing['ids']:
                            new_metadatas = existing['metadatas']
                            for m in new_metadatas:
                                m['source'] = new_name
                            db_mgr.collection.update(ids=existing['ids'], metadatas=new_metadatas)
                    except Exception as e:
                        self.send_log("WARNING", f"Vector update failed: {e}")
            finally:
                if getattr(db_mgr, 'client', None):
                    try:
                        db_mgr.client._system.stop()
                    except Exception:
                        pass

        kb_mgr._touch_meta(os.path.join(kb_mgr.WORKSPACE_DIR, kb_id))
        self.send_log("INFO", f"Successfully renamed {len(renames)} files.")

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
                    if self.is_cancelled():
                        raise InterruptedError("Export task was safely terminated by the user.")
                    if file.endswith('.lock') or file.endswith('.tmp'): continue

        self.send_log("INFO", f"Export successful: {dest_path}")


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