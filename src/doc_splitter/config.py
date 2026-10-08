import dataclasses
import hashlib
import json
import math
import os
import re
from pathlib import Path

MODEL_REVISION = "dccadc69dd4ae69c63f42aa6715f249c6e5e6cf8"


@dataclasses.dataclass(frozen=True)
class Config:
    inbox: Path
    consume: Path
    archive: Path
    review: Path
    staging: Path
    work: Path
    state: Path
    model_cache: Path
    model_revision: str = MODEL_REVISION
    threshold: float = 0.5
    poll_seconds: float = 5
    settle_seconds: float = 30
    completion_mode: str = "settle"
    ocr_languages: str = "deu+eng"
    ocr_timeout: int = 120
    batch_size: int = 2
    threads: int = 2
    max_pages: int = 128
    max_source_mb: int = 512
    max_render_pixels: int = 16_000_000
    render_dpi: int = 150
    job_timeout: int = 1800
    max_attempts: int = 3
    retry_seconds: int = 30
    heartbeat_timeout: int = 60
    startup_timeout: int = 1800
    dry_run: bool = False
    review_margin: float = 0.0
    image_revision: str = "development"

    @classmethod
    def from_env(cls) -> "Config":
        values = {}
        paths = {"inbox", "consume", "archive", "review", "staging", "work", "state", "model_cache"}
        for field in dataclasses.fields(cls):
            key = "SPLIT_" + field.name.upper()
            raw = os.getenv(key)
            if field.name in paths:
                if not raw:
                    raise ValueError(f"{key} must be an explicit absolute path")
                values[field.name] = Path(raw)
            elif raw is not None:
                if field.type is bool:
                    if raw.lower() not in {"true", "false"}:
                        raise ValueError(f"{key} must be true or false")
                    values[field.name] = raw.lower() == "true"
                else:
                    values[field.name] = field.type(raw)
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        if self.model_revision != MODEL_REVISION:
            raise ValueError(
                "unsupported model revision: artifact hashes must be reviewed together"
            )
        if not math.isfinite(self.threshold) or not 0 < self.threshold < 1:
            raise ValueError("threshold must be finite and between 0 and 1")
        if not math.isfinite(self.review_margin) or not 0 <= self.review_margin < 0.5:
            raise ValueError("review_margin must be between 0 and 0.5")
        for name in (
            "poll_seconds",
            "ocr_timeout",
            "batch_size",
            "threads",
            "max_pages",
            "max_source_mb",
            "max_render_pixels",
            "render_dpi",
            "job_timeout",
            "max_attempts",
            "retry_seconds",
            "heartbeat_timeout",
            "startup_timeout",
        ):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(self.settle_seconds) or self.settle_seconds < 0:
            raise ValueError("settle_seconds must be finite and nonnegative")
        if self.max_pages > 128:
            raise ValueError("v1 supports at most 128 pages; larger batches require benchmarking")
        if self.batch_size > 8:
            raise ValueError("batch_size must not exceed the tested memory safety ceiling of 8")
        if self.completion_mode not in {"settle", "marker", "atomic"}:
            raise ValueError("completion_mode must be settle, marker or atomic")
        if not re.fullmatch(r"[a-zA-Z0-9_]+(\+[a-zA-Z0-9_]+)*", self.ocr_languages):
            raise ValueError("invalid OCR language identifiers")
        roots = []
        for name in (
            "inbox",
            "consume",
            "archive",
            "review",
            "staging",
            "work",
            "state",
            "model_cache",
        ):
            path = getattr(self, name)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{name} must be absolute without parent traversal")
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError(f"{name} must not contain symlink components")
            for other in roots:
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError("configured roots must be disjoint and non-nested")
            roots.append(path)

    def manifest_config(self) -> dict:
        # Filesystem locations do not affect inference and need not expose host topology.
        return {
            f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.type is not Path
        }

    def revision(self) -> str:
        return hashlib.sha256(
            json.dumps(self.manifest_config(), sort_keys=True).encode()
        ).hexdigest()
