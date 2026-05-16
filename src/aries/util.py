"""Small shared utilities."""
from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    import blake3 as _blake3
    _HAS_BLAKE3 = True
except ImportError:  # pragma: no cover
    _HAS_BLAKE3 = False


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, compact separators) as bytes.

    Used by UCAN signing and Receipt signing so two encoders agree.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(value: Any, length: int = 16) -> str:
    """BLAKE3 (or SHA-256 fallback) hash of canonical JSON, truncated hex."""
    data = canonical_json(value) if not isinstance(value, (bytes, bytearray)) else bytes(value)
    if _HAS_BLAKE3:
        return _blake3.blake3(data).hexdigest()[:length]
    return hashlib.sha256(data).hexdigest()[:length]
