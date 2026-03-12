import logging
import re
from langdetect import detect_langs


log = logging.getLogger(__name__)


def detect_primary_language(text):
    if not text.strip():
        return "unknown"

    # 匹配 CJK (中日韩) 统一表意文字
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)

    text_without_spaces = text.replace(" ", "")
    if len(chinese_chars) > 5 or (len(chinese_chars) / max(len(text_without_spaces), 1) > 0.1):
        return 'zh'

    # 第二层：纯拉丁字母语言的统计检测
    try:
        # 去除数字、常见标点符号和特殊字符，减少噪音
        text_to_detect = re.sub(r'[0-9\.\-\/\(\)（），。！？:;""'']+', ' ', text).strip()

        # 去除完了啥都没有那就是英语
        if not text_to_detect:
            return "en"

        detected_langs = detect_langs(text_to_detect)
        top_lang = detected_langs[0]

        log.info(f"Detected languages: {detected_langs}")

        if top_lang.prob >= 0.5:
            return top_lang.lang
        else:
            return "uncertain"

    except Exception as e:
        log.error(f"Exception detected: {e}")
        return "error"

if __name__ == "__main__":

    texts = [
        "帮我找 5 篇关于 CRISPR-Cas9 off-target effects 的最新论文",  # 中英夹杂
        "Find 5 recent papers about CRISPR-Cas9 off-target effects.",  # 纯英文
        "Cherche 5 articles récents sur CRISPR-Cas9 off-target effects.",  # 法英夹杂
        "Buscar 5 artículos recientes sobre CRISPR-Cas9 off-target effects." , # 西英夹杂
        "帮我找 5 篇关于 CRISPR-Cas9 off-target effects（脱靶效应）的最新论文，重点看看Semantic Scholar 上的结果，列出它们的标题、作者、年份和 DOI。"
    ]

    for t in texts:
        lang = detect_primary_language(t)
        print(f"[{lang:<2}] {t}")