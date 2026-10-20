import glob
import logging
import os
import shutil
import sys
import json
import time

os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "15")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "15")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from src.core.config_manager import ConfigManager
from src.core.device_manager import DeviceManager

# 说明：本模块位于启动导入链上（主窗口/工具面板 → models_registry），因此不在
# 顶层导入 KBManager（它会拉起 chromadb）；只在需要的函数内按需导入。
# 模型下载只接受 ONNX-ready 仓库，不再依赖 torch/optimum 现场导出。



logger = logging.getLogger("ModelRegistry")


class ModelMissingError(RuntimeError):
    """本地模型缺失但禁止自动下载时抛出。

    携带可读的展示名与仓库名，便于上层任务用一句话提示用户去
    "设置 → AI Models" 手动下载，而不是悄悄联网拉取。
    """

    def __init__(self, repo_id, ui_name, model_type="model"):
        self.repo_id = repo_id
        self.ui_name = ui_name
        self.model_type = model_type
        super().__init__(
            f"{ui_name} ({model_type}) is not downloaded yet. "
            f"Please download it manually from Settings → AI Models."
        )


def _display_name_for(repo_id):
    for m in EMBEDDING_MODELS + RERANKER_MODELS:
        if m.get('hf_repo_id') == repo_id:
            return m.get('ui_name', m.get('id', repo_id))
    return repo_id


# hf_repo_id 指向已包含 ONNX 的镜像仓库；运行时只下载 ONNX + tokenizer，
# 不再从 PyTorch 权重现场导出。
EMBEDDING_MODELS = [
    {
        "id": "embed_qwen3_06b",
        "ui_name": "Best - Qwen3-Embedding-0.6B",
        "hf_repo_id": "onnx-community/Qwen3-Embedding-0.6B-ONNX",
        "onnx_file": "onnx/model_int8.onnx",
        "description": "Best open multilingual quality under 1B; 32K context, last-token pooling.",
        "tags": ["Best Quality", "Multilingual", "GPU Recommended"],
        "params": "0.6B",
        "max_tokens": 32768,
        "tokenizer_max_length": 2048,
        "dimensions": 1024,
        "trust_remote_code": False,
        "pooling": "last_token",
        "query_instruction": ("Given a scientific question, retrieve passages "
                              "that answer the question"),
        "chunk_size": 1200,
        "chunk_overlap": 200,
        "batch_size": 16,
        "recommended_config": {
            "device_priority": "GPU Recommended",
            "min_ram": "4 GB",
            "disk_space": "800 MB"
        }
    },
    {
        "id": "embed_bge_large_en",
        "ui_name": "English Fast - BGE-Large-EN-v1.5",
        "hf_repo_id": "Xenova/bge-large-en-v1.5",
        "onnx_file": "onnx/model_int8.onnx",
        "description": "Strong English retrieval; 512 tokens, 1024 dims; much faster than 0.6B models.",
        "tags": ["English", "Fast", "CPU Friendly"],
        "params": "335M",
        "max_tokens": 512,
        "tokenizer_max_length": 512,
        "dimensions": 1024,
        "trust_remote_code": False,
        "pooling": "cls",
        "chunk_size": 1000,
        "chunk_overlap": 160,
        "batch_size": 16,
        "recommended_config": {
            "device_priority": "CPU Friendly",
            "min_ram": "1 GB",
            "disk_space": "400 MB"
        }
    },
    {
        "id": "embed_gemma_300m",
        "ui_name": "CPU/Edge - EmbeddingGemma 308M",
        "hf_repo_id": "onnx-community/embeddinggemma-300m-ONNX",
        "onnx_file": "onnx/model_quantized.onnx",
        "description": ("308M multilingual, 2K tokens, 768 dims, built-in "
                        "sentence embedding. Low RAM; Gemma license, check commercial terms."),
        "tags": ["CPU Friendly", "Multilingual", "On-device"],
        "params": "308M",
        "max_tokens": 2048,
        "tokenizer_max_length": 2048,
        "dimensions": 768,
        "trust_remote_code": False,
        "pooling": "sentence_embedding",
        "chunk_size": 1000,
        "chunk_overlap": 160,
        "batch_size": 16,
        "recommended_config": {
            "device_priority": "CPU Friendly",
            "min_ram": "1 GB",
            "disk_space": "400 MB"
        }
    },
    {
        "id": "embed_scientific_m3",
        "ui_name": "Scientific (Best for Bio) - BGE-M3",
        "hf_repo_id": "Xenova/bge-m3",
        "onnx_file": "onnx/model_int8.onnx",
        "description": "Flagship model. Excellent understanding of rare terminology. Supports 8k context.",
        "tags": ["Academic", "Multilingual"],
        "params": "568M",
        "max_tokens": 8192,
        "tokenizer_max_length": 2048,
        "dimensions": 1024,
        "trust_remote_code": True,
        "pooling": "cls",
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

# 只用于已存在的 KB 读取，不再出现在模型选择列表/新库里。
LEGACY_EMBEDDING_MODELS = {
    "embed_nano_fast": {
        "id": "embed_nano_fast",
        "ui_name": "Legacy - Snowflake Arctic XS",
        "hf_repo_id": "Snowflake/snowflake-arctic-embed-xs",
        "max_tokens": 512,
        "dimensions": 384,
        "trust_remote_code": False,
        "pooling": "cls",
        "chunk_size": 400,
        "chunk_overlap": 80,
        "batch_size": 64,
    },
}

RERANKER_MODELS = [
    {
        "id": "rerank_lite",
        "ui_name": "Lite - BGE-Reranker-Base",
        "hf_repo_id": "Xenova/bge-reranker-base",
        "description": "Legacy fast model kept only for existing configurations.",
        "max_chunk_size": 400,
        "hidden": True,
        "recommended_config": {
            "device_priority": "CPU Friendly",
            "min_ram": "2 GB",
            "disk_space": "1.5 GB"
        }
    },
    {
        "id": "rerank_pro_m3",
        "ui_name": "Balanced - BGE-Reranker-v2-M3",
        "hf_repo_id": "onnx-community/bge-reranker-v2-m3-ONNX",
        "onnx_file": "onnx/model_quantized.onnx",
        "description": "0.6B multilingual cross-encoder; best drop-in classifier; 512-token window.",
        "params": "0.6B",
        "scoring": "classifier",
        "tokenizer_max_length": 512,
        "max_chunk_size": 2000,
        "recommended_config": {
            "device_priority": "GPU Recommended",
            "min_ram": "8 GB",
            "min_vram": "6 GB",
            "disk_space": "2.5 GB"
        }
    },
    {
        "id": "rerank_qwen3_06b",
        "ui_name": "Quality - Qwen3-Reranker-0.6B",
        "hf_repo_id": "onnx-community/Qwen3-Reranker-0.6B-ONNX",
        "onnx_file": "onnx/model_quantized.onnx",
        "description": ("0.6B causal reranker; strong multilingual ranking; 32K context. "
                        "Scores yes/no logits; slower than BGE classifier."),
        "params": "0.6B",
        "scoring": "qwen3_yes_no",
        "query_instruction": ("Given a scientific question, retrieve passages "
                              "that answer the question"),
        "tokenizer_max_length": 4096,
        "max_chunk_size": 2000,
        "recommended_config": {
            "device_priority": "GPU Recommended",
            "min_ram": "4 GB",
            "min_vram": "3 GB",
            "disk_space": "1.3 GB"
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
    except Exception as e:
        logger.debug(f"Device probe failed, fallback to cpu: {e}")
        dev = "cpu"

    # 解析 AUTO 宏
    if embedding_model_id == "embed_auto":
        embedding_model_id = resolve_auto_model("embedding", dev) or "embed_qwen3_06b"
    if reranker_model_id == "rerank_auto":
        reranker_model_id = resolve_auto_model("reranker", dev) or "rerank_pro_m3"

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
    if model_type == "embedding":
        # Qwen3-Embedding 0.6B 是当前兼顾质量与体积的最佳默认；
        # BGE-M3 保留为兼容/备选，Snowflake XS 仅服务旧知识库。
        return "embed_qwen3_06b"

    elif model_type == "reranker":
        return "rerank_pro_m3"

    return None


def get_model_conf(model_id, model_type="embedding"):
    target_list = EMBEDDING_MODELS if model_type == "embedding" else RERANKER_MODELS
    for m in target_list:
        if m['id'] == model_id:
            return m
    if model_type == "embedding":
        return LEGACY_EMBEDDING_MODELS.get(model_id)
    return None


def init_external_models_file():
    cfg = ConfigManager()
    if not os.path.exists(cfg.EXTERNAL_MODELS_PATH):
        default_structure = {"embedding": [], "reranker": []}
        cfg.save_external_models(default_structure)


_LEGACY_ONNX_REPOS = {
    "Xenova/snowflake-arctic-embed-xs": "Snowflake/snowflake-arctic-embed-xs",
    "Xenova/bge-m3": "BAAI/bge-m3",
    "Xenova/bge-reranker-base": "BAAI/bge-reranker-base",
    "onnx-community/bge-reranker-v2-m3-ONNX": "BAAI/bge-reranker-v2-m3",
    "Xenova/bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",
}


def _repo_candidates(repo_id):
    legacy = _LEGACY_ONNX_REPOS.get(repo_id)
    return [repo_id] + ([legacy] if legacy else [])


def check_model_exists(repo_id):
    if not repo_id:
        return False

    hf_home = _get_hf_home()
    for candidate in _repo_candidates(repo_id):
        onnx_dir = os.path.join(hf_home, "models--" + candidate.replace("/", "--"))
        logger.info(f"Detecting ONNX model path: {onnx_dir}")
        if os.path.exists(onnx_dir):
            for root, dirs, files in os.walk(onnx_dir):
                if any(f.endswith('.onnx') for f in files):
                    logger.info(f"Success! .onnx file found at this path.")
                    return True

    for candidate in _repo_candidates(repo_id):
        if _official_check(candidate) or _manual_check(candidate):
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
    except OSError: return False
    return True

def _official_check(repo_id):
    try:
        from huggingface_hub import scan_cache_dir
        info = scan_cache_dir(_get_hf_home())
        for repo in info.repos:
            if repo.repo_id == repo_id and repo.revisions: return True
    except (ImportError, OSError, ValueError, RuntimeError): pass
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
        snapshot_download(repo_id, etag_timeout=15)
        return True
    except (ImportError, OSError, ValueError, RuntimeError): return False


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
        user_pref = config.user_settings.get("inference_device", "auto")
        dev = self.dev_mgr.parse_device_string(user_pref)

        # --- 校验 A: Embedding 模型 ---
        from src.core.kb_manager import KBManager  # 惰性：避免启动链上拉起 chromadb

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


def _preferred_onnx_file(repo_id):
    for m in EMBEDDING_MODELS + RERANKER_MODELS:
        if m.get("hf_repo_id") == repo_id and m.get("onnx_file"):
            return str(m["onnx_file"])
    return ""


def ensure_onnx_model(repo_id, model_type=None, onnx_files_available=False,
                      allow_download=True, preferred_file=None):
    """确保本地有可直接运行的 ONNX 模型。

    模型仓库必须是 ONNX-ready（例如 Xenova/*-ONNX 镜像）。不再从 PyTorch
    权重现场导出：那一步需要 torch/optimum，已从基础依赖中移除。
    ``preferred_file`` 可避免把仓库里所有量化变体都下载下来。
    """
    hf_home = _get_hf_home()
    onnx_dir = os.path.join(hf_home, "models--" + repo_id.replace("/", "--"))
    logger.info(f"Requesting model: {repo_id} | Target ONNX cache dir: {onnx_dir}")
    for candidate in _repo_candidates(repo_id):
        candidate_dir = os.path.join(hf_home, "models--" + candidate.replace("/", "--"))
        if not os.path.exists(candidate_dir):
            continue
        for _root, _dirs, files in os.walk(candidate_dir):
            if any(f.endswith(".onnx") for f in files):
                logger.info("Local ONNX cache hit: %s", candidate_dir)
                return candidate_dir
    if not allow_download:
        raise ModelMissingError(repo_id, _display_name_for(repo_id), model_type or "model")

    from huggingface_hub import snapshot_download
    preferred = preferred_file or _preferred_onnx_file(repo_id)
    logger.info("Downloading ONNX fileset (no PyTorch conversion)%s...",
                f", preferred={preferred}" if preferred else "")
    # 直接落到应用缓存目录；ONNX 仓库通常把模型放在 onnx/ 子目录，递归查找。
    allow_patterns = ["*.json", "*.txt", "*.model", "*.spm"]
    if preferred:
        # 只取首选变体及其外部权重，避免为了 model.onnx_data 顺带拉 2GB。
        allow_patterns.extend([preferred, preferred + "_data",
                               preferred + ".data"])
    else:
        allow_patterns.extend(["*.onnx", "*.onnx_data*"])
    # 清理上次异常退出的下载锁；否则 hf_hub 会无限等待 “acquire lock”。
    if os.path.isdir(onnx_dir):
        for lock_file in glob.glob(
                os.path.join(onnx_dir, "**", "*.lock"), recursive=True):
            try:
                os.remove(lock_file)
                logger.warning("Removed stale model download lock: %s", lock_file)
            except OSError as e:
                logger.warning("Cannot remove model download lock %s: %s",
                               lock_file, e)
    last_error = None
    for attempt in range(4):
        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=onnx_dir,
                allow_patterns=allow_patterns,
                ignore_patterns=["*.bin", "*.safetensors", "*.h5", "*.msgpack",
                                 "*.pt", "*.pth", "*.ckpt"],
                etag_timeout=15,
                max_workers=1,
            )
            break
        except Exception as e:
            last_error = e
            logger.warning("ONNX download attempt %d/4 failed: %s", attempt + 1, e)
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
    else:
        raise last_error
    if not any(f.endswith(".onnx")
               for _root, _dirs, files in os.walk(onnx_dir) for f in files):
        raise RuntimeError(
            f"{repo_id} does not contain an ONNX file. "
            "Automatic PyTorch-to-ONNX conversion has been removed; "
            "choose an ONNX-ready model repository.")
    logger.info("ONNX model ready: %s", onnx_dir)
    return onnx_dir




