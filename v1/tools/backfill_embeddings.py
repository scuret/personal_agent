"""Backfill `messages.embedding` and `facts.embedding` for historical rows.

Idempotent — only touches rows where embedding IS NULL. Run after the
schema migration lands (i.e. after the first time any daemon boots
with the new code), or after switching `EMBEDDER_MODEL` if you want to
recompute everything (delete the column contents first; this script
intentionally doesn't overwrite).

Usage:
    python -m tools.backfill_embeddings              # both tables
    python -m tools.backfill_embeddings --messages   # messages only
    python -m tools.backfill_embeddings --facts      # facts only
    python -m tools.backfill_embeddings --dry-run    # show counts, no writes

Batched encoding is faster than one-at-a-time — the sentence-transformers
model amortizes overhead across the batch.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.sqlite"
BATCH_SIZE = 32


def _open() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"audit db not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Make sure the `embedding` columns exist before we try to populate
    them — required when running the backfill against a DB that's never
    been opened by the new MemoryStore code."""
    for table in ("messages", "facts"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "embedding" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN embedding BLOB")
            print(f"[migrate] added {table}.embedding")


def _backfill_table(
    conn: sqlite3.Connection,
    table: str,
    dry_run: bool,
) -> int:
    rows = conn.execute(
        f"SELECT id, content FROM {table} WHERE embedding IS NULL AND content IS NOT NULL AND content != ''"
    ).fetchall()
    if not rows:
        print(f"[{table}] nothing to backfill")
        return 0

    print(f"[{table}] {len(rows)} row(s) need embedding")
    if dry_run:
        return len(rows)

    # Import the encoder here so a --dry-run avoids loading 500MB of torch.
    from memory.embedder import encode

    started = time.time()
    done = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        texts = [r["content"] for r in batch]
        try:
            vecs = encode(texts)  # shape (n, dim) float32, normalized
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] batch starting at #{batch[0]['id']} failed: {e}", file=sys.stderr)
            continue
        for r, vec in zip(batch, vecs, strict=False):
            conn.execute(
                f"UPDATE {table} SET embedding = ? WHERE id = ?",
                (vec.tobytes(), r["id"]),
            )
        done += len(batch)
        print(f"  [{table}] {done}/{len(rows)}")

    elapsed = time.time() - started
    print(f"[{table}] backfilled {done} row(s) in {elapsed:.1f}s")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill embeddings for historical rows.")
    parser.add_argument("--messages", action="store_true", help="Only backfill messages table")
    parser.add_argument("--facts", action="store_true", help="Only backfill facts table")
    parser.add_argument("--dry-run", action="store_true", help="Show counts without writing")
    args = parser.parse_args()

    # Default to both when neither flag is set.
    do_messages = args.messages or not (args.messages or args.facts)
    do_facts = args.facts or not (args.messages or args.facts)

    conn = _open()
    _ensure_schema(conn)

    total = 0
    if do_messages:
        total += _backfill_table(conn, "messages", dry_run=args.dry_run)
    if do_facts:
        total += _backfill_table(conn, "facts", dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n(dry-run) would have embedded {total} row(s) total")
    else:
        print(f"\ndone. embedded {total} row(s) total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
