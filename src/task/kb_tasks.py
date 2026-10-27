import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from typing import TYPE_CHECKING

import onnxruntime as ort

from src.core import safe_io
from src.core.core_task import BackgroundTask
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager, DatabaseManager
from src.core.models_registry import get_model_conf, ensure_onnx_model, ModelMissingError
from src.core.onnx_provider import resolve_provider
from src.core.rerank_engine import RerankEngine

if TYPE_CHECKING:  # 仅用于类型注解：运行期不导入 chromadb（启动链上约 0.4 s）
    from chromadb import Documents, Embeddings

logger = logging.getLogger("Task.kb")

def _setup_worker_env():
    """KB 子进程的环境准备，返回数据根目录。

    数据根目录必须与主进程完全一致，统一取自
    :func:`src.core.platform_env.app_root`；子进程可能是 spawn 出来的全新解释器，
    因此在此重新求值，而不是依赖父进程的全局变量（此前这里维护了第三份
    "冻结 or 源码"判断，与主进程的 ``BASE_DIR`` 有分歧风险）。
    """
    import os

    from src.core.platform_env import app_root

    base_dir = app_root()
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
    repo_id = conf['hf_repo_id'] if conf else "Xenova/all-MiniLM-L6-v2"
    try:
        # allow_download=False：模型缺失不再自动联网拉取，而是抛 ModelMissingError，
        # 由上层向用户提示去 "设置 → AI Models" 手动下载。
        onnx_dir = ensure_onnx_model(
            repo_id, "embedding", allow_download=False,
            preferred_file=(conf or {}).get("onnx_file", ""))

        logger.info(f"Loading Embedding Model: {repo_id} on {device_str}")

        return ONNXEmbeddingFunction(
            onnx_dir, device=device_str,
            pooling=(conf or {}).get("pooling", "cls"),
            query_instruction=(conf or {}).get("query_instruction", ""),
            onnx_file=(conf or {}).get("onnx_file", ""),
            max_length=int((conf or {}).get("tokenizer_max_length", 512)))
    except ModelMissingError as e:
        # 模型未下载：原样上抛清晰提示，引导用户去设置手动下载，不自动联网拉取
        raise e
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise RuntimeError(f"Model Load Failed: {str(e)}")


class ONNXEmbeddingFunction:
    """本地 ONNX 嵌入函数（ChromaDB 兼容）。

    刻意**不继承** ``chromadb.EmbeddingFunction``：chromadb 只做鸭子类型校验
    （``chromadb.api.types.validate_embedding_function`` 比较 ``__call__`` 的
    参数名是否与协议一致，不做 isinstance 检查），而继承就必须在导入期导入
    chromadb——本模块位于启动链上，代价约 0.4 s。接口要求：
    ``__call__(self, input: Documents) -> Embeddings``，参数名必须是 ``input``。
    """

    @staticmethod
    def _find_onnx_file(root: str, preferred: str = "") -> str:
        if preferred:
            candidate = os.path.join(root, preferred)
            if os.path.isfile(candidate):
                return candidate
        for base, _dirs, files in os.walk(root):
            if "model.onnx" in files:
                return os.path.join(base, "model.onnx")
        for base, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".onnx"):
                    return os.path.join(base, name)
        return ""

    def __init__(self, onnx_cache_dir, device="cpu", pooling="cls",
                 query_instruction="", onnx_file="", max_length=512):
        from src.core.onnx_tokenizer import OnnxTokenizer

        self.onnx_cache_dir = str(onnx_cache_dir)
        self.device = str(device)
        self.pooling = str(pooling or "cls")
        self.query_instruction = str(query_instruction or "")
        self.onnx_file = str(onnx_file or "")
        self.max_length = int(max_length)
        self._raw_session = None
        self._raw_inputs = set()
        self._raw_input_shapes = {}
        logger = logging.getLogger("Worker.ONNXProvider")

        self.tokenizer = OnnxTokenizer(onnx_cache_dir, max_length=max_length)
        available_providers = ort.get_available_providers()

        device_str = str(device).lower()
        logger.info(f"ONNX Init Requested Device: {device_str}")
        logger.info(f"Available ONNX Providers in Env: {available_providers}")

        resolved = resolve_provider(device_str)
        provider = resolved.provider
        if resolved.degraded:
            logger.warning(f"Embedding device degraded to CPU: {resolved.reason}")

        model_path = self._find_onnx_file(onnx_cache_dir, self.onnx_file)
        if not model_path:
            raise FileNotFoundError(
                f"No ONNX model found under {onnx_cache_dir}; download an ONNX-ready model.")

        providers = [provider]
        if "CPUExecutionProvider" not in providers:
            providers.append("CPUExecutionProvider")
        try:
            self._raw_session = ort.InferenceSession(model_path, providers=providers)
        except Exception as e:
            if providers[0] != "CPUExecutionProvider":
                logger.warning("ONNX GPU embedding failed (%s); retrying on CPU.", e)
                self._raw_session = ort.InferenceSession(
                    model_path, providers=["CPUExecutionProvider"])
            else:
                raise
        self._raw_inputs = {i.name for i in self._raw_session.get_inputs()}
        self._raw_input_shapes = {i.name: i.shape
                                  for i in self._raw_session.get_inputs()}
        logger.info("Embedding uses ONNX Runtime on %s (%s).",
                    self._raw_session.get_providers()[0], model_path)

    def name(self) -> str:
        """Chroma 1.x EmbeddingFunction 协议要求；用于防止集合函数冲突。"""
        return "scholar_navis_onnx"

    def get_config(self) -> dict:
        return {"onnx_cache_dir": self.onnx_cache_dir, "device": self.device,
                "pooling": self.pooling,
                "query_instruction": self.query_instruction,
                "onnx_file": self.onnx_file,
                "max_length": self.max_length}

    def embed_query(self, input):
        """Chroma 1.x query path 调用；Qwen3 需要给 query 加 instruction。"""
        if self.query_instruction:
            if isinstance(input, str):
                input = (f"Instruct: {self.query_instruction}\n"
                         f"Query: {input}")
            elif isinstance(input, list):
                input = [f"Instruct: {self.query_instruction}\nQuery: {x}"
                         if isinstance(x, str) else x for x in input]
        return self.__call__(input)

    @staticmethod
    def build_from_config(config: dict):
        return ONNXEmbeddingFunction(
            config.get("onnx_cache_dir", ""), config.get("device", "cpu"),
            config.get("pooling", "cls"),
            config.get("query_instruction", ""),
            config.get("onnx_file", ""),
            config.get("max_length", 512))

    def __call__(self, input: "Documents") -> "Embeddings":
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

        import numpy as np
        enc = self.tokenizer.encode(texts)
        batch, seq = enc["input_ids"].shape
        ort_inputs = {}
        for name in self._raw_inputs:
            if name in enc:
                ort_inputs[name] = np.asarray(enc[name])
            elif name == "position_ids":
                ort_inputs[name] = np.tile(
                    np.arange(seq, dtype=np.int64), (batch, 1))
            elif name.startswith("past_key_values."):
                # Qwen3 的 ONNX 导出保留了 KV cache 输入；无历史时给 0 长度。
                shape = self._raw_input_shapes.get(name) or []
                dims = [d if isinstance(d, int) else (batch if i == 0 else 0)
                        for i, d in enumerate(shape)]
                if len(dims) >= 3:
                    dims[-2:] = [dims[-2] if isinstance(dims[-2], int) else 8,
                                 dims[-1] if isinstance(dims[-1], int) else 128]
                ort_inputs[name] = np.zeros(tuple(dims), dtype=np.float32)
        outputs = self._raw_session.run(None, ort_inputs)
        if self.pooling == "sentence_embedding" and len(outputs) > 1:
            out = np.asarray(outputs[1], dtype=np.float32)
        else:
            out = np.asarray(outputs[0], dtype=np.float32)
        if out.ndim == 3:
            mask = np.asarray(enc["attention_mask"], dtype=np.float32)
            if self.pooling == "last_token":
                # Qwen3-Embedding: 取最后一个非 padding token 的 hidden state。
                last = np.maximum(mask.sum(axis=1).astype(np.int64) - 1, 0)
                out = out[np.arange(out.shape[0]), last]
            elif self.pooling == "mean":
                denom = np.maximum(mask.sum(axis=1, keepdims=True), 1.0)
                out = (out * mask[..., None]).sum(axis=1) / denom
            else:
                out = out[:, 0, :]
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        out = out / np.maximum(norms, 1e-12)
        return out.tolist()


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
                except (ValueError, RuntimeError, OSError):
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

                        temp_pdf_path = os.path.join(temp_dir, f"{uuid.uuid4().hex}_{source_name}")
                        shutil.copy2(read_path, temp_pdf_path)

                        try:
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
                except (RuntimeError, AttributeError, OSError):
                    pass

            if 'embed_fn' in locals() and embed_fn:
                if hasattr(embed_fn, 'model'):
                    del embed_fn.model
                del embed_fn

            import gc
            gc.collect()


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
                    except (RuntimeError, AttributeError, OSError):
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
                    except (RuntimeError, AttributeError, OSError):
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

        # 先收集清单：既用于进度分母，也避免边遍历边写时目录被改动。
        plan = []
        for root, _dirs, files in os.walk(src_dir):
            for file in files:
                if file.endswith('.lock') or file.endswith('.tmp'):
                    continue
                file_path = os.path.join(root, file)
                plan.append((file_path, os.path.relpath(file_path, src_dir)))

        self.send_log("INFO", f"Packing Knowledge Base {kb_id} ({len(plan)} files)...")

        # 写 .part 再原子替换。直接写目标路径时，中途取消会让 ZipFile.close()
        # 补出一个结构合法但内容残缺的 zip，用户无法分辨它是否完整。
        temp_path = f"{dest_path}.part"
        step = max(1, len(plan) // 20)
        try:
            with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for index, (file_path, arcname) in enumerate(plan, 1):
                    if self.is_cancelled():
                        raise InterruptedError("Export task was safely terminated by the user.")
                    zipf.write(file_path, arcname)
                    if index % step == 0:
                        self.update_progress(
                            int(index * 100 / len(plan)),
                            f"Packing {index}/{len(plan)} files...")
            os.replace(temp_path, dest_path)
        except BaseException:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

        self.update_progress(100, f"Export complete: {len(plan)} files")
        self.send_log("INFO", f"Export successful: {dest_path}")

class ImportExternalKBTask(BackgroundTask):
    """导入外部知识库归档。

    归档来自用户之间传递的文件，属于不可信输入：条目名可能带 ``../``，
    条目体积可能远大于压缩包（压缩炸弹），``meta.json`` 里的 ``id`` 也可能
    是相对路径。三者都必须在这里挡住，否则会写出工作区之外或写满磁盘。
    """

    #: 条目数量上限
    MAX_ENTRIES = 20000
    #: 全部条目解压后的总字节上限
    MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
    #: 单个条目解压后的字节上限
    MAX_ENTRY_BYTES = 4 * 1024 * 1024 * 1024
    #: 知识库 id 允许的字符集（现行实现是 uuid4）
    _KB_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

    def _check_archive_budget(self, infos) -> None:
        if len(infos) > self.MAX_ENTRIES:
            raise ValueError(
                f"Archive contains too many entries ({len(infos)} > {self.MAX_ENTRIES}).")
        total = 0
        for info in infos:
            if info.file_size > self.MAX_ENTRY_BYTES:
                raise ValueError(f"Archive entry too large: {info.filename}")
            total += info.file_size
            if total > self.MAX_TOTAL_BYTES:
                raise ValueError("Archive expands beyond the allowed total size.")

    def _execute(self):
        _setup_worker_env()
        from src.core.kb_manager import KBManager
        bundle_path = self.kwargs.get('bundle_path')
        kb_mgr = KBManager()
        self.send_log("INFO", "Extracting project bundle...")

        # 唯一前缀便于强杀后识别残留；固定目录名会在并发导入时互相踩。
        temp_extract_dir = tempfile.mkdtemp(prefix="kb_import_", dir=kb_mgr.WORKSPACE_DIR)
        try:
            with zipfile.ZipFile(bundle_path, 'r') as zipf:
                infos = [i for i in zipf.infolist() if not i.is_dir()]
                self._check_archive_budget(infos)
                total = len(infos)
                step = max(1, total // 20)
                for index, info in enumerate(infos, 1):
                    if self.is_cancelled():
                        raise InterruptedError("Import task was safely terminated by the user.")

                    target = os.path.join(temp_extract_dir, info.filename)
                    # 逐条校验落点仍在临时目录内，替代不设防的 extractall
                    if not safe_io.ensure_within(temp_extract_dir, target):
                        raise ValueError(f"Archive entry escapes target directory: {info.filename}")
                    os.makedirs(os.path.dirname(target) or temp_extract_dir, exist_ok=True)

                    written = 0
                    with zipf.open(info) as src, open(target, "wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > self.MAX_ENTRY_BYTES:
                                raise ValueError(f"Archive entry too large: {info.filename}")
                            dst.write(chunk)

                    if index % step == 0:
                        self.update_progress(
                            int(index * 100 / total), f"Extracting {index}/{total}...")

            found_root = None
            for root, dirs, files in os.walk(temp_extract_dir):
                if "meta.json" in files:
                    found_root = root
                    break
            if not found_root: raise ValueError("Invalid Project Bundle")

            with open(os.path.join(found_root, "meta.json"), 'r', encoding='utf-8') as f:
                info = json.load(f)

            raw_id = str(info.get('id') or "")
            if not raw_id: raise ValueError("Missing ID")
            if not self._KB_ID_RE.match(raw_id):
                raise ValueError(f"Invalid knowledge base id: {raw_id!r}")
            kb_id = raw_id

            target_dir = os.path.join(kb_mgr.WORKSPACE_DIR, kb_id)
            if not safe_io.ensure_within(kb_mgr.WORKSPACE_DIR, target_dir):
                raise ValueError(f"Knowledge base id escapes workspace: {raw_id!r}")

            if os.path.exists(target_dir):
                new_id = str(uuid.uuid4())
                info['id'] = new_id
                info['name'] = f"{info['name']} (Imported)"
                kb_id = new_id
                target_dir = os.path.join(kb_mgr.WORKSPACE_DIR, kb_id)
                safe_io.atomic_write_json(os.path.join(found_root, "meta.json"), info)
            shutil.move(found_root, target_dir)

            self.send_log("INFO", f"Project imported successfully as '{info['name']}'")
        finally:
            if os.path.exists(temp_extract_dir): shutil.rmtree(temp_extract_dir, ignore_errors=True)


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