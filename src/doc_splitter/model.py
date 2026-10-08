import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from .config import Config
from .fs import UnsafeInput, sha256, sync_dir

LOG = logging.getLogger(__name__)
ARTIFACTS = {
    "image_model.onnx": "655849d83877953e8001cfd1efa03c085caf31511ef0ccc8d31b765fe3e91e89",
    "text_model.onnx": "9141a601b17b1aa38a502b2b02aae19b82b2969f2f1e44ed5ef1cf91ee6aa958",
    "head.onnx": "de1bef8d8ae1889b24e1f3121e95b59fe0a0d3c902fc16080b8447d99c33ab75",
    "tokenizer.json": "cd98e5698b201ba914efb8c18b6709fa8735ab71dcad8d2b431e52e8bf68d932",
    "crf.json": "16ae180604997038f1d1720606549f51ef012c6e1284a5bc703a881ba1b77563",
    "tokenizer_config.json": "2cb37db9c77c8011481d9c38f940c31e8534ef93dd2aed70e3a2684439eec75d",
    "special_tokens_map.json": "a0a9a46202fe95ab00167831490a3e80363d9c3433afd4133caa8bb86de83880",
    "README.md": "8d0a2bb1d061bf2779e2fd66976dda1a8fd6bbeae0659c81158ed8a4a0509f4c",
}


def snapshot(config: Config) -> Path:
    root = config.model_cache / config.model_revision
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise ValueError("model snapshot directory must not be a symlink")
    for name, expected in ARTIFACTS.items():
        target = root / name
        if target.exists() and sha256(target) == expected:
            continue
        LOG.info("model_artifact_download artifact=%s revision=%s", name, config.model_revision)
        url = (
            "https://huggingface.co/nutrientdocs/doc-split-v1/resolve/"
            f"{config.model_revision}/{name}"
        )
        part = root / (name + ".part")
        digest = hashlib.sha256()
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=60) as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise RuntimeError(f"model artifact checksum mismatch: {name}")
        os.replace(part, target)
        sync_dir(root)
    return root


def calibrated_marginals(logits: np.ndarray, crf: dict) -> np.ndarray:
    logits = np.asarray(logits, dtype=np.float64).copy()
    if logits.ndim != 1 or not len(logits) or not np.isfinite(logits).all():
        raise UnsafeInput("boundary head returned invalid logits")
    logits[0] = 30.0
    transitions = np.asarray(crf["trans"], dtype=np.float64)
    emissions = np.column_stack((np.zeros(len(logits)), logits))
    forward = np.empty_like(emissions)
    backward = np.empty_like(emissions)
    forward[0] = np.asarray(crf["start"]) + emissions[0]
    backward[-1] = crf["end"]
    for index in range(1, len(logits)):
        scores = forward[index - 1, :, None] + transitions
        forward[index] = np.logaddexp.reduce(scores, axis=0) + emissions[index]
    for index in range(len(logits) - 2, -1, -1):
        scores = transitions + emissions[index + 1] + backward[index + 1]
        backward[index] = np.logaddexp.reduce(scores, axis=1)
    posterior = forward + backward
    raw = np.exp(posterior[:, 1] - np.logaddexp.reduce(posterior, axis=1))
    raw = np.clip(raw, 1e-12, 1 - 1e-12)
    beta_logit = 0.516 * np.log(raw) - 0.402 * np.log1p(-raw) - 0.155
    return 1 / (1 + np.exp(-beta_logit))


def page_ranges(scores: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    if scores.ndim != 1 or not len(scores) or not np.isfinite(scores).all():
        raise UnsafeInput("invalid boundary scores")
    starts = [0] + [i for i in range(1, len(scores)) if scores[i] >= threshold]
    return [(start + 1, end) for start, end in zip(starts, starts[1:] + [len(scores)], strict=True)]


class Model:
    def __init__(self, config: Config):
        root = snapshot(config)
        options = ort.SessionOptions()
        options.intra_op_num_threads = config.threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        self.image = ort.InferenceSession(
            str(root / "image_model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.text = ort.InferenceSession(
            str(root / "text_model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.head = ort.InferenceSession(
            str(root / "head.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.crf = json.loads((root / "crf.json").read_text())
        self.config = config

    def encode(self, images: list, texts: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        pixels = np.stack(
            [
                (np.asarray(image.convert("RGB").resize((512, 512)), dtype=np.float32) / 255 - 0.5)
                / 0.5
                for image in images
            ]
        ).transpose(0, 3, 1, 2)
        tokens = self.tokenizer.encode_batch(["query: " + (text or " ") for text in texts])
        ids = np.asarray([token.ids for token in tokens], dtype=np.int64)
        attention = np.asarray([token.attention_mask for token in tokens], dtype=np.int64)
        visual = self.image.run(["image_embed"], {"pixel_values": pixels})[0]
        textual = self.text.run(["text_embed"], {"input_ids": ids, "attention_mask": attention})[0]
        gate = np.asarray([float(bool(text.strip())) for text in texts], dtype=np.float32)
        return visual, textual * gate[:, None], gate

    def boundaries(self, embeddings: list) -> np.ndarray:
        image, text, gate = (np.concatenate([batch[i] for batch in embeddings]) for i in range(3))
        count = len(gate)
        if count > self.config.max_pages:
            raise UnsafeInput("page count exceeds safe full-sequence limit")
        logits = self.head.run(
            ["boundary_logit"],
            {
                "v_img": image[None],
                "v_txt": text[None],
                "gate": gate[None],
                "mask": np.ones((1, count), dtype=np.float32),
            },
        )[0]
        if logits.shape != (1, count):
            raise UnsafeInput("boundary head returned wrong sequence length")
        return calibrated_marginals(logits[0], self.crf)

    def warmup(self) -> None:
        from PIL import Image

        self.boundaries([self.encode([Image.new("RGB", (512, 512), "white")], [""])])
