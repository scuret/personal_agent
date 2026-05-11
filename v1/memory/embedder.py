"""Local sentence-transformers wrapper for semantic recall.

Lazy-loads the model on first encode() call so daemon startup stays
snappy. The model + its weights are cached in a module-level singleton
so each daemon process loads it once.

Configured via env var:
  EMBEDDER_MODEL (default: BAAI/bge-base-en-v1.5)

Choose `BAAI/bge-small-en-v1.5` to drop the resident footprint from
~500MB to ~200MB at a modest quality cost. `BAAI/bge-large-en-v1.5`
goes the other way (~1.5GB, better quality).

Output is a numpy float32 vector. The default model is 768 dims; small
is 384, large is 1024. Storage in SQLite is `array.tobytes()`.

Debugging CLI:
    python -m memory.embedder "wedding planning"
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

import numpy as np

DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"

_MODEL: Any = None
_MODEL_NAME: str | None = None
_LOAD_LOCK = threading.Lock()


def _model_name() -> str:
    return (os.environ.get("EMBEDDER_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _get_model() -> Any:
    """Lazy-load + cache the SentenceTransformer instance.

    Thread-safe via a module-level lock — daemons share a single
    in-process model across worker threads. First call takes ~2-3s
    (model load); subsequent calls reuse the loaded instance.
    """
    global _MODEL, _MODEL_NAME
    name = _model_name()
    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    with _LOAD_LOCK:
        if _MODEL is not None and _MODEL_NAME == name:
            return _MODEL
        # Import here so importing this module doesn't drag in 500MB
        # of torch unless someone actually needs embeddings.
        from sentence_transformers import SentenceTransformer  # noqa: E402

        _MODEL = SentenceTransformer(name)
        _MODEL_NAME = name
    return _MODEL


def encode(text: str | list[str]) -> np.ndarray:
    """Return a float32 embedding for `text` (or a 2D array for a list).

    Single string → 1D vector of shape (dim,).
    List of strings → 2D array of shape (n, dim).
    """
    model = _get_model()
    is_single = isinstance(text, str)
    vec = model.encode(
        [text] if is_single else text,
        normalize_embeddings=True,  # so cosine == dot product later
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    arr = np.asarray(vec, dtype=np.float32)
    return arr[0] if is_single else arr


def encode_to_bytes(text: str) -> bytes:
    """Convenience: encode a single string and return the raw bytes for
    SQLite BLOB storage. Returns empty bytes if encoding fails."""
    return encode(text).tobytes()


def decode_blob(blob: bytes | None) -> np.ndarray | None:
    """Reverse of encode_to_bytes. Returns None on empty / missing blob."""
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def dim() -> int:
    """Embedding dimensionality for the configured model. Triggers a
    load if the model isn't already cached."""
    model = _get_model()
    return int(model.get_sentence_embedding_dimension())


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m memory.embedder <text>", file=sys.stderr)
        sys.exit(2)
    text = " ".join(sys.argv[1:])
    vec = encode(text)
    print(f"model: {_model_name()}")
    print(f"dim:   {vec.shape[0]}")
    print(f"norm:  {float(np.linalg.norm(vec)):.4f}  (should be ~1.0 after normalize)")
    print(f"first 8 values: {vec[:8].tolist()}")


if __name__ == "__main__":
    main()
