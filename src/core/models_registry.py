import logging
import os
import shutil
import sys
import json

from huggingface_hub import scan_cache_dir, snapshot_download, constants

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

CONFIG_DIR = os.path.join(BASE_DIR, "config")
EXTERNAL_MODELS_FILE = os.path.join(CONFIG_DIR, "external_models.json")


logger =logging.getLogger("ModelRegistry")

EMBEDDING_MODELS = [
    {
        "id": "embed_auto",
        "ui_name": "AUTO (System Recommended)",
        "is_auto": True,
        "description": "System will auto-select based on hardware.",
        "chunk_size": 800,
        "chunk_overlap": 150,
        "batch_size": 16
    },
    {
        "id": "embed_nano_fast",
        "ui_name": "⚡ Nano (Speed Priority) - Snowflake Arctic XS",
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
    },
    {
        "id": "embed_gte_qwen2_7b",
        "ui_name": "GTE-Qwen2-7B-Instruct",
        "hf_repo_id": "Alibaba-NLP/gte-Qwen2-7B-instruct",
        "description": "LLM-based embedding. State-of-the-art semantic understanding.",
        "tags": ["MTEB Top 10", "High VRAM"],
        "max_tokens": 32768,
        "dimensions": 3584,
        "trust_remote_code": True,
        "chunk_size": 1500,
        "chunk_overlap": 250,
        "batch_size": 4,
        "recommended_config": {
            "device_priority": "GPU Required",
            "min_ram": "32 GB",
            "min_vram": "16 GB",
            "disk_space": "15 GB"
        }
    },
    {
        "id": "embed_nv_v2",
        "ui_name": "NVIDIA NV-Embed-v2",
        "hf_repo_id": "nvidia/NV-Embed-v2",
        "description": "NVIDIA official model. Supports 32k context. Requires 24GB+ VRAM.",
        "tags": ["NVIDIA","SOTA"],
        "max_tokens": 32768,
        "dimensions": 4096,
        "trust_remote_code": True,
        "chunk_size": 2000,
        "chunk_overlap": 300,
        "batch_size": 2,
        "recommended_config": {
            "device_priority": "High-End GPU Only",
            "min_ram": "64 GB",
            "min_vram": "24 GB",
            "disk_space": "16 GB"
        }
    }
]

RERANKER_MODELS = [
    {
        "id": "rerank_auto",
        "ui_name": "AUTO (Best Performance)",
        "is_auto": True,
        "description": "System will auto-select based on hardware.",
        "max_chunk_size": 800
    },
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
    },
    {
        "id": "rerank_gemma_9b",
        "ui_name": "Ultra (Precision) - BGE-Reranker-v2-Gemma",
        "hf_repo_id": "BAAI/bge-reranker-v2-gemma",
        "description": "Based on Gemma-2-9B. Massive parameters, high performance.",
        "tags": ["9B Params", "High VRAM"],
        "max_chunk_size": 2500,
        "recommended_config": {
            "device_priority": "High-End GPU Only",
            "min_ram": "32 GB",
            "min_vram": "20 GB",
            "disk_space": "20 GB"
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
    has_gpu = device in ["cuda", "mps"]
    vram_gb = 0
    if has_gpu and device == "cuda":
        try:
            import torch
            props = torch.cuda.get_device_properties(0)
            vram_gb = props.total_memory / (1024 ** 3)
        except:
            vram_gb = 0

    if model_type == "embedding":
        if has_gpu:
            if vram_gb >= 22: return "embed_nv_v2"
            elif vram_gb >= 12: return "embed_gte_qwen2_7b"
            else: return "embed_scientific_m3"
        else: return "embed_nano_fast"

    elif model_type == "reranker":
        if has_gpu:
            if vram_gb >= 20: return "rerank_gemma_9b"
            else: return "rerank_pro_m3"
        else: return "rerank_lite"
    return None

def get_model_conf(model_id, model_type="embedding"):
    target_list = EMBEDDING_MODELS if model_type == "embedding" else RERANKER_MODELS
    for m in target_list:
        if m['id'] == model_id: return m
    return None

def init_external_models_file():
    if not os.path.exists(CONFIG_DIR): os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(EXTERNAL_MODELS_FILE):
        default_structure = {"embedding": [], "reranker": []}
        try:
            with open(EXTERNAL_MODELS_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_structure, f, indent=4)
        except: pass

def check_model_exists(repo_id):
    if not repo_id or repo_id == "Unknown": return False
    if _official_check(repo_id): return True
    if _manual_check(repo_id): return True
    hf_home = _get_hf_home()
    base_repo_path = os.path.join(hf_home, "hub", "models--" + repo_id.replace("/", "--"))
    if os.path.exists(base_repo_path):
        if _repair_model_links(repo_id):
            if _official_check(repo_id) or _manual_check(repo_id): return True
    return False

def load_external_models():
    init_external_models_file()
    if os.path.exists(EXTERNAL_MODELS_FILE):
        try:
            with open(EXTERNAL_MODELS_FILE, 'r', encoding='utf-8') as f:
                ext_models = json.load(f)
                for m in ext_models.get("embedding", []):
                    if not get_model_conf(m['id'], "embedding"): EMBEDDING_MODELS.append(m)
                for m in ext_models.get("reranker", []):
                    if not get_model_conf(m['id'], "reranker"): RERANKER_MODELS.append(m)
        except: pass

load_external_models()

def _get_hf_home(): return os.environ.get("HF_HOME", constants.HF_HOME)
def _is_file_valid(path):
    if not os.path.exists(path): return False
    try:
        if os.path.getsize(path) == 0: return False
    except: return False
    return True

def _official_check(repo_id):
    try:
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
        for folder in ["snapshots", "refs"]:
            p = os.path.join(repo_dir, folder)
            if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
        snapshot_download(repo_id, resume_download=True)
        return True
    except: return False