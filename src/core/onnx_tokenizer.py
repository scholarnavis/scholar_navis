"""Tokenizer for ONNX embedding/reranker models.

Only depends on ``tokenizers`` (Rust) and ``numpy``; no transformers, no torch.
ONNX-ready model repositories ship ``tokenizer.json`` alongside the model.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, List, Sequence, Tuple, Union

logger = logging.getLogger("Core.ONNXTokenizer")


class OnnxTokenizer:
    """Thin wrapper around a HuggingFace ``tokenizer.json``.

    Padding is done here (to the longest sequence in the batch) rather than
    through ``Tokenizer.enable_padding``: the latter needs a fixed length and
    would force every request to 512 tokens even for short queries.
    """

    def __init__(self, model_dir: str, max_length: int = 512):
        from tokenizers import Tokenizer

        path = os.path.join(model_dir, "tokenizer.json")
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"{model_dir} has no tokenizer.json; "
                "choose an ONNX-ready model repository.")
        self.tokenizer = Tokenizer.from_file(path)
        self.tokenizer.enable_truncation(max_length=max_length)
        self.max_length = max_length
        self.pad_id = self._resolve_pad_id(model_dir)

    def _resolve_pad_id(self, model_dir: str) -> int:
        for name in ("tokenizer_config.json", "special_tokens_map.json"):
            try:
                with open(os.path.join(model_dir, name), "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                continue
            pad = cfg.get("pad_token")
            if isinstance(pad, dict):
                pad = pad.get("content")
            if isinstance(pad, str):
                token_id = self.tokenizer.token_to_id(pad)
                if token_id is not None:
                    return int(token_id)
        for candidate in ("[PAD]", "<pad>", "<PAD>", "pad"):
            token_id = self.tokenizer.token_to_id(candidate)
            if token_id is not None:
                return int(token_id)
        return 0

    def token_to_id(self, token: str) -> int:
        token_id = self.tokenizer.token_to_id(token)
        return int(token_id) if token_id is not None else -1

    def encode(self, texts: Sequence[Union[str, Tuple[str, str]]],
               padding_side: str = "right") -> dict[str, Any]:
        """Return numpy-ready ids/mask/type ids, padded to the batch max."""
        import numpy as np

        encodings = self.tokenizer.encode_batch(list(texts))
        max_len = max((len(e.ids) for e in encodings), default=0)
        ids: List[List[int]] = []
        masks: List[List[int]] = []
        types: List[List[int]] = []
        for enc in encodings:
            pad = max_len - len(enc.ids)
            if padding_side == "left":
                ids.append([self.pad_id] * pad + list(enc.ids))
                masks.append([0] * pad + list(enc.attention_mask))
                types.append([0] * pad + list(enc.type_ids))
            else:
                ids.append(list(enc.ids) + [self.pad_id] * pad)
                masks.append(list(enc.attention_mask) + [0] * pad)
                types.append(list(enc.type_ids) + [0] * pad)
        return {
            "input_ids": np.asarray(ids, dtype=np.int64),
            "attention_mask": np.asarray(masks, dtype=np.int64),
            "token_type_ids": np.asarray(types, dtype=np.int64),
        }
