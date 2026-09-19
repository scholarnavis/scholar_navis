"""Image attachment utilities.

纯标准库实现（不依赖 Qt），因此可以安全地在 UI 进程与后台任务
子进程中共同使用：

- ``IMAGE_EXTENSIONS`` / ``SVG_EXTENSIONS``：受支持的图片类型集合。
- ``guess_mime``：根据扩展名推断 MIME。
- ``encode_data_url``：把图片文件编码为 ``data:<mime>;base64,...``。
- ``is_image_file``：判断路径是否为受支持的图片。
- ``MAX_IMAGE_BYTES``：单张图片的大小上限。

SVG 说明：绝大多数视觉模型不接受 SVG 作为多模态输入。SVG 在 UI
进程附加时会被栅格化为 PNG（见 chat_mixins.attachments），其
``image_path`` 指向该 PNG；此处仅负责按扩展名读取与编码。
"""
import base64
import os

logger = __import__("logging").getLogger(__name__)

#: 受支持的光栅图片扩展名（可被视觉模型直接消费）
RASTER_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')

#: 受支持的矢量图片扩展名（发送前需栅格化）
SVG_EXTENSIONS = ('.svg',)

#: 受支持的图片扩展名全集
IMAGE_EXTENSIONS = RASTER_EXTENSIONS + SVG_EXTENSIONS

#: 单张附件图片的最大体积（字节）：15 MB
MAX_IMAGE_BYTES = 15 * 1024 * 1024

_MIME_MAP = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
}


def guess_mime(path: str) -> str:
    """根据扩展名推断 MIME 类型，未知类型回退为 PNG。"""
    return _MIME_MAP.get(os.path.splitext(path)[1].lower(), 'image/png')


def is_image_file(path: str) -> bool:
    """判断路径是否为受支持的图片类型。"""
    return bool(path) and os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def is_svg_file(path: str) -> bool:
    """判断路径是否为 SVG。"""
    return bool(path) and os.path.splitext(path)[1].lower() in SVG_EXTENSIONS


def encode_data_url(path: str) -> str:
    """把图片文件编码为 data URL。

    :param path: 本地图片路径（应传入 ``image_path``，SVG 需已栅格化为 PNG）
    :raises FileNotFoundError: 文件不存在。
    :raises ValueError: 超过大小上限。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Image file not found: {path}")

    size = os.path.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image exceeds size limit ({size / 1024 / 1024:.1f} MB > "
            f"{MAX_IMAGE_BYTES / 1024 / 1024:.0f} MB): {os.path.basename(path)}")

    with open(path, 'rb') as f:
        raw = f.read()

    mime = guess_mime(path)
    b64 = base64.b64encode(raw).decode('ascii')
    return f"data:{mime};base64,{b64}"
