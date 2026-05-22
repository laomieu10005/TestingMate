from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AstCacheKey:
    path: str
    size: int
    mtime_ns: int
    digest: str
    context: str = ""


class AstCache:
    """Small in-process cache for source text and parsed AST payloads."""

    def __init__(self) -> None:
        self._asts: dict[AstCacheKey, dict] = {}
        self._texts: dict[AstCacheKey, str] = {}

    def source_text(self, source: Path) -> str:
        key = self._key(source, "text")
        if key not in self._texts:
            self._texts[key] = source.read_text(encoding="utf-8", errors="replace")
        return self._texts[key]

    def ast_for(self, source: Path, loader: Callable[[Path], dict], context: str = "") -> dict:
        key = self._key(source, context)
        if key not in self._asts:
            self._asts[key] = loader(source)
        return self._asts[key]

    def clear(self) -> None:
        self._asts.clear()
        self._texts.clear()

    @staticmethod
    def _key(source: Path, context: str = "") -> AstCacheKey:
        stat = source.stat()
        digest = hashlib.sha1(
            f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{context}".encode("utf-8", errors="ignore")
        ).hexdigest()
        return AstCacheKey(
            path=str(source.resolve()),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            digest=digest,
            context=context,
        )
