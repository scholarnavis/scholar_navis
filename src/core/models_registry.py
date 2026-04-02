import gc
import logging
import os
import shutil
import sys
import json
import warnings

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager
from src.core.kb_manager import KBManager



logger = logging.getLogger("ModelRegistry")

EMBEDDING_MODELS = [
    {
        "id": "embed_nano_fast",
        "ui_name": "Nano (Speed Priority) - Snowflake Arctic XS",
        "hf_repo_id": "Snowflake/snowflake-arctic-embed-xs",
        "description": "Minimal parameters, suitable for pure CPU environments.",
        "tags": ["Ultra-Fast", "CPU"],
        "max_tokens": 512,
        "dimensions": 384,
        "trust_remote_code": False,
        "chunk_size": 400,
        "chunk_overlap": 80,
        "batch_size": 64,
        "recommended_config": {
            "device_priority": "CPU",
            "min_ram": "512 MB",
            "disk_space": "100 MB"
        }
    },
    {
        "id": "embed_scientific_m3",
        "ui_name": "Scientific (Best for Bio) - BGE-M3",
        "hf_repo_id": "BAAI/bge-m3",
        "description": "Flagship model. Excellent understanding of rare terminology. Supports 8k context.",
        "tags": ["Academic", "Multilingual"],
        "max_tokens": 8192,
        "dimensions": 1024,
        "trust_remote_code": True,
        "chunk_size": 1200,
        "chunk_overlap": 200,
        "batch_size": 16,
        "recommended_config": {
            "device_priority": "GPU Recommended",
            "min_ram": "8 GB",
            "min_vram": "4 GB",
            "disk_space": "2.5 GB"
        }
    }
]

RERANKER_MODELS = [
    {
        "id": "rerank_lite",
        "ui_name": "Lite - BGE-Reranker-Base",
        "hf_repo_id": "BAAI/bge-reranker-base",
        "description": "Fast, CPU-friendly. Window limit: 512 tokens.",
        "max_chunk_size": 400,
        "recommended_config": {
            "device_priority": "CPU Friendly",
            "min_ram": "2 GB",
            "disk_space": "1.5 GB"
        }
    },
    {
        "id": "rerank_pro_m3",
        "ui_name": "Pro (Standard) - BGE-Reranker-v2-M3",
        "hf_repo_id": "BAAI/bge-reranker-v2-m3",
        "description": "Multi-language & long text support. GPU recommended.",
        "max_chunk_size": 2000,
        "recommended_config": {
            "device_priority": "GPU Recommended",
            "min_ram": "8 GB",
            "min_vram": "6 GB",
            "disk_space": "2.5 GB"
        }
    }
]

def get_optimal_chunk_settings(embedding_model_id: str, reranker_model_id: str):
    """
    智能木桶算法：根据 Embedding 和 Reranker 模型组合，动态计算最佳切分参数。
    """
    # 延迟导入防止循环依赖
    try:
        from src.core.config_manager import ConfigManager
        from src.core.device_manager import DeviceManager
        dev = DeviceManager().parse_device_string(ConfigManager().user_settings.get("inference_device", "Auto"))
    except:
        dev = "cpu"

    # 解析 AUTO 宏
    if embedding_model_id == "embed_auto":
        embedding_model_id = resolve_auto_model("embedding", dev) or "embed_nano_fast"
    if reranker_model_id == "rerank_auto":
        reranker_model_id = resolve_auto_model("reranker", dev) or "rerank_lite"

    embed_conf = get_model_conf(embedding_model_id, "embedding") or EMBEDDING_MODELS[0]
    rerank_conf = get_model_conf(reranker_model_id, "reranker") or RERANKER_MODELS[0]

    # 1. 获取 Embedding 模型的理想状态
    chunk_size = embed_conf.get("chunk_size", 800)
    overlap = embed_conf.get("chunk_overlap", 150)
    batch_size = embed_conf.get("batch_size", 16)

    # 2. 获取 Reranker 模型的致命天花板
    max_chunk = rerank_conf.get("max_chunk_size", 2000)

    # 3. 木桶裁切：如果切得比 Reranker 能看的还长，强制降维！
    if chunk_size > max_chunk:
        chunk_size = max_chunk
        # 按比例重新计算 overlap (15% 左右)
        overlap = int(chunk_size * 0.15)

    return chunk_size, overlap, batch_size


def resolve_auto_model(model_type="embedding", device="cpu"):
    # 加入 "dml" (DirectML) 的识别，以兼容未来的 DeviceManager 传参
    has_gpu = device in ["cuda", "mps", "dml", "directml"]

    if model_type == "embedding":
        if has_gpu:
            return "embed_scientific_m3"
        else:
            return "embed_nano_fast"

    elif model_type == "reranker":
        if has_gpu:
            return "rerank_pro_m3"
        else:
            return "rerank_lite"

    return None


def get_model_conf(model_id, model_type="embedding"):
    target_list = EMBEDDING_MODELS if model_type == "embedding" else RERANKER_MODELS
    for m in target_list:
        if m['id'] == model_id: return m
    return None


def init_external_models_file():
    cfg = ConfigManager()
    if not os.path.exists(cfg.EXTERNAL_MODELS_PATH):
        default_structure = {"embedding": [], "reranker": []}
        cfg.save_external_models(default_structure)


def check_model_exists(repo_id):
    if not repo_id:
        return False

    hf_home = _get_hf_home()
    onnx_dir = os.path.join(hf_home, "models--" + repo_id.replace("/", "--"))

    logger.info(f"Detecting ONNX model path: {onnx_dir}")

    if os.path.exists(onnx_dir):
        for root, dirs, files in os.walk(onnx_dir):
            if any(f.endswith('.onnx') for f in files):
                logger.info(f"Success! .onnx file found at this path.")
                return True

    logger.warning(f"Failed! No .onnx file found at path {onnx_dir}, or directory does not exist.")
    return False


def load_external_models():
    init_external_models_file()
    cfg = ConfigManager()
    ext_models = cfg.load_external_models_data()
    if ext_models:
        for m in ext_models.get("embedding", []):
            m['trust_remote_code'] = False
            if not get_model_conf(m['id'], "embedding"): EMBEDDING_MODELS.append(m)
        for m in ext_models.get("reranker", []):
            m['trust_remote_code'] = False
            if not get_model_conf(m['id'], "reranker"): RERANKER_MODELS.append(m)


def register_external_model(model_info, model_type="embedding"):
    if not model_info: return
    model_id = model_info.get("id")

    model_info['trust_remote_code'] = False

    if get_model_conf(model_id, model_type):
        return

    # 加入当前可用列表
    if model_type == "embedding":
        EMBEDDING_MODELS.append(model_info)
    else:
        RERANKER_MODELS.append(model_info)

    # 写入外部配置，使得下一次启动自动生效
    cfg = ConfigManager()
    ext_models = cfg.load_external_models_data()
    if model_type not in ext_models:
        ext_models[model_type] = []

    # 去重后不加密落盘
    if not any(m.get("id") == model_id for m in ext_models[model_type]):
        ext_models[model_type].append(model_info)
        cfg.save_external_models(ext_models)

load_external_models()

def _get_hf_home():
    from huggingface_hub import constants
    return os.environ.get("HF_HOME", constants.HF_HOME)

def _is_file_valid(path):
    if not os.path.exists(path): return False
    try:
        if os.path.getsize(path) == 0: return False
    except: return False
    return True

def _official_check(repo_id):
    try:
        from huggingface_hub import scan_cache_dir
        info = scan_cache_dir(_get_hf_home())
        for repo in info.repos:
            if repo.repo_id == repo_id and repo.revisions: return True
    except: pass
    return False

def _manual_check(repo_id):
    hf_home = _get_hf_home()
    cache_dir = os.path.join(hf_home, "hub", "models--" + repo_id.replace("/", "--"), "snapshots")
    if os.path.exists(cache_dir):
        for snap in os.listdir(cache_dir):
            if _is_file_valid(os.path.join(cache_dir, snap, "config.json")): return True
    if _is_file_valid(os.path.join(hf_home, repo_id, "config.json")): return True
    if _is_file_valid(os.path.join(hf_home, repo_id.split("/")[-1], "config.json")): return True
    return False

def _repair_model_links(repo_id):
    logger.info(f"Detecting broken links for {repo_id}. Fixing...")
    repo_dir = os.path.join(_get_hf_home(), "hub", "models--" + repo_id.replace("/", "--"))
    if not os.path.exists(repo_dir): return False
    try:
        from huggingface_hub import snapshot_download
        for folder in ["snapshots", "refs"]:
            p = os.path.join(repo_dir, folder)
            if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
        snapshot_download(repo_id, resume_download=True)
        return True
    except: return False


class ModelManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ModelManager, cls).__new__(cls)
            cls._instance.logger = logging.getLogger("ModelManager")
            cls._instance.dev_mgr = DeviceManager()
        return cls._instance

    def verify_chat_models(self, kb_id):

        config = ConfigManager()
        dev = self.dev_mgr.get_optimal_device()

        # --- 校验 A: Embedding 模型 ---
        kb_info = KBManager().get_kb_by_id(kb_id)
        embed_id = kb_info.get('model_id', 'embed_auto') if kb_info else 'embed_auto'
        if embed_id == "embed_auto":
            embed_id = resolve_auto_model("embedding", dev)

        e_conf = get_model_conf(embed_id, "embedding")
        if not e_conf or not check_model_exists(e_conf.get('hf_repo_id')):
            ui_name = e_conf.get('ui_name', embed_id) if e_conf else embed_id
            return False, f"Embedding Model ({ui_name})", embed_id, "embedding"

        rerank_id = config.user_settings.get("rerank_model_id", "rerank_auto")
        if rerank_id == "rerank_auto":
            rerank_id = resolve_auto_model("reranker", dev)

        r_conf = get_model_conf(rerank_id, "reranker")
        if r_conf and not check_model_exists(r_conf.get('hf_repo_id')):
            ui_name = r_conf.get('ui_name', rerank_id)
            return False, f"Reranker Model ({ui_name})", rerank_id, "reranker"

        return True, None, None, None


def get_onnx_cache_dir(repo_id):
    """获取模型的专属 ONNX 本地缓存目录"""
    hf_home = _get_hf_home()
    return os.path.join(hf_home, "models--" + repo_id.replace("/", "--"))

def get_model_type_by_repo(repo_id):
    for m in EMBEDDING_MODELS:
        if m.get('hf_repo_id') == repo_id: return "embedding"
    for m in RERANKER_MODELS:
        if m.get('hf_repo_id') == repo_id: return "reranker"
    return "embedding"


def ensure_onnx_model(repo_id, model_type=None):
    hf_home = _get_hf_home()
    onnx_dir = os.path.join(hf_home, "models--" + repo_id.replace("/", "--"))
    logger.info(f"Requesting model: {repo_id} | Target ONNX cache dir: {onnx_dir}")

    if os.path.exists(onnx_dir):
        for root, dirs, files in os.walk(onnx_dir):
            if any(f.endswith('.onnx') for f in files):
                logger.info("Local ONNX cache hit, skipping download and conversion.")
                return onnx_dir

    logger.info("Local ONNX cache miss, preparing for download and conversion...")
    logger.info("Loading heavy AI frameworks (Transformers/Optimum) into memory...")
    from huggingface_hub import snapshot_download
    from transformers import AutoTokenizer
    from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTModelForSequenceClassification
    model_path = snapshot_download(repo_id=repo_id)

    source_has_onnx = False
    for root, dirs, files in os.walk(model_path):
        if any(f.endswith('.onnx') for f in files):
            source_has_onnx = True
            break

    should_export = not source_has_onnx

    if source_has_onnx:
        logger.info("Official ONNX model detected in downloaded source, skipping format conversion.")
    else:
        logger.info("No official ONNX model detected, starting PyTorch to ONNX engine...")

    if not model_type:
        model_type = get_model_type_by_repo(repo_id)

    is_trust_remote = False
    for m in EMBEDDING_MODELS + RERANKER_MODELS:
        if m.get('hf_repo_id') == repo_id:
            is_trust_remote = m.get('trust_remote_code', False)
            break

    try:
        tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            trust_remote_code=is_trust_remote
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")

            if model_type == "embedding":
                model = ORTModelForFeatureExtraction.from_pretrained(
                    model_path,
                    export=should_export,
                    provider="CPUExecutionProvider",
                    trust_remote_code=is_trust_remote
                )
            else:
                model = ORTModelForSequenceClassification.from_pretrained(
                    model_path,
                    export=should_export,
                    provider="CPUExecutionProvider",
                    trust_remote_code=is_trust_remote
                )

        os.makedirs(onnx_dir, exist_ok=True)
        model.save_pretrained(onnx_dir)
        tokenizer.save_pretrained(onnx_dir)
        logger.info(f"ONNX processing complete and saved to: {onnx_dir}")

    except Exception as e:
        logger.error(f"ONNX Export Failed (Possible Out of Memory for 7B+ models): {str(e)}")
        raise e

    finally:
        # 无论成功失败，尝试释放内存
        if 'model' in locals(): del model
        if 'tokenizer' in locals(): del tokenizer
        gc.collect()

        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    folder_name = "models--" + repo_id.replace("/", "--")
    hf_model_dir = os.path.join(hf_home, "hub", folder_name)
    if os.path.exists(hf_model_dir):
        try:
            shutil.rmtree(hf_model_dir)
            logger.info(f"Cleaned up original PyTorch cache to save disk space: {hf_model_dir}")
        except Exception as e:
            logger.warning(f"Failed to clean original cache: {e}")

    return onnx_dir




