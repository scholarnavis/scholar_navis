"""配置束的**唯一构造口径**：本地导出与 WebDAV 上传共用同一份内容。

配置束 = **可移植的用户配置**。以下四类东西刻意不进来：

* ``config/fulltext_links.json``（DOI ↔ 本地 PDF 关联）：存的是本机绝对路径，
  换一台机器那些文件几乎必然不存在，导过去只会让每条"已有关联"都指向空；
  它本来就是独立文件，随应用目录迁移，不随配置束走。
* ``config/model_capabilities.json``：本机对 provider 实测出来的能力缓存，
  换机器/换 key 后结论可能不成立，重新探测即可。
* ``config/external_models.json`` 与模型目录：本地推理已下线，没有消费者。
* ``config/webdav_sources.json``：备份源是"把配置束送到哪里"的目的地本身。
  把服务器地址与账号塞进要上传到该服务器的文件里，既没有迁移价值，也会让
  备份文件自身携带一份凭据线索。

序列化与加解密不在这里：本地导出由 :class:`src.task.config_task.ExportConfigTask`
负责，WebDAV 上传直接复用同一个任务实现，因此两条路径产出的文件格式完全一致
（有口令时 ``{payload, salt, mode="encrypted"}``，无口令时 ``{data, mode="plain"}``）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import zlib

from src.core.skill_store import read_skill_source

logger = logging.getLogger(__name__)

#: 单个技能源码解压后的字节上限。正常技能脚本远小于此值，上限只用来挡住压缩炸弹。
SKILL_CODE_MAX_BYTES = 8 * 1024 * 1024

#: 技能源码压缩级别。技能脚本体积小，level=9 换来的体积收益与导入耗时无关紧要，
#: 保留原级别以免已经导出的配置包与新包的体积特征不一致。
_SKILL_COMPRESS_LEVEL = 9


def read_rss_feeds() -> list:
    """当前 RSS 订阅清单（订阅是用户配置，应当随配置束迁移）。读不到就给空表。"""
    from src.core.rss_paths import RSS_FEEDS_FILE

    try:
        with open(RSS_FEEDS_FILE, "r", encoding="utf-8") as fh:
            feeds = json.load(fh)
    except (OSError, ValueError) as e:
        logger.debug("RSS feed list unavailable for export: %s", e)
        return []
    return feeds if isinstance(feeds, list) else []


def _export_skill_sources(mcp_servers: dict) -> dict:
    """把外部技能源码压缩成 base64；单个技能失败只跳过该技能。"""
    exported: dict = {}
    for name, cfg in (mcp_servers or {}).get("external_skills", {}).items():
        if name in ("built-in", "Academic Tool", "builtin"):
            continue
        skill_path = str((cfg or {}).get("command") or "")
        if not skill_path or not os.path.exists(skill_path):
            continue
        try:
            # .py 明文与历史 .enc 密文共用同一读取入口（src/core/skill_store.py）
            code = read_skill_source(skill_path).encode("utf-8")
            exported[name] = base64.b64encode(
                zlib.compress(code, level=_SKILL_COMPRESS_LEVEL)).decode("utf-8")
        except Exception as e:      # noqa: BLE001 - 单个技能读不到不应中断整包
            logger.warning("Failed to read and compress skill %s for export: %s", name, e)
    return exported


def build_bundle(config, llm_configs: list) -> dict:
    """构造配置束。``config`` 是 :class:`ConfigManager`，读取的是**已保存**的配置。

    界面上的未保存改动不在包内：导出/上传前由调用方走
    ``check_unsaved_changes`` 提示用户先保存，避免"上传了一份看不见的旧配置"。
    """
    return {
        "settings": config.user_settings,
        "mcp_servers": config.mcp_servers,
        "llm_configs": llm_configs,
        "skill_files_zlib_b64": _export_skill_sources(config.mcp_servers),
        "rss_feeds": read_rss_feeds(),
    }


def decompress_limited(data: bytes, max_bytes: int = SKILL_CODE_MAX_BYTES) -> bytes:
    """有上限的 zlib 解压。

    导入的配置束是不可信输入：几 KB 的 base64 可以膨胀到 GB 级，而
    ``zlib.decompress`` 没有长度参数，会直接把内存吃满。这里用
    ``decompressobj`` 限定输出，并检查是否还有未消耗的输入。
    """
    obj = zlib.decompressobj()
    out = obj.decompress(data, max_bytes + 1)
    if len(out) > max_bytes or obj.unconsumed_tail:
        raise ValueError(f"技能源码解压后超过 {max_bytes} 字节上限")
    tail = obj.flush()
    if len(out) + len(tail) > max_bytes:
        raise ValueError(f"技能源码解压后超过 {max_bytes} 字节上限")
    return out + tail
