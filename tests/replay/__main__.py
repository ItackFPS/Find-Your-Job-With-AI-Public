"""Command line: replay N runs on a fresh copy of a database, without contacting any job source.

    python -m tests.replay --source-db <copy of jobs.db> --config <config dir> --work-dir <empty dir> [--runs 2]

The source database is opened read-only; every write goes to ``<work-dir>/jobs.db``. The report (JSON)
is printed and written to ``<work-dir>/replay-report.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

from tests.replay.harness import ReplayCorpus, copy_database, replay_run
from tests.replay.inventory import inventory, new_orphans


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.replay", description=__doc__.splitlines()[0])
    ap.add_argument("--source-db", required=True, type=Path)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--work-dir", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--statuses", default="active", help="comma-separated job statuses to replay")
    ap.add_argument("--fake-embeddings", action="store_true",
                    help="deterministic hash embeddings instead of the cached model (faster; semantic scores differ)")
    args = ap.parse_args(argv)

    corpus = ReplayCorpus.from_database(args.source_db, statuses=tuple(args.statuses.split(",")))
    work_db = copy_database(args.source_db, args.work_dir)
    runs = []
    ctx = patch("src.embeddings.embedding.EmbeddingService", _fake_embedding_class()) if args.fake_embeddings \
        else _null()
    initial = inventory(work_db)
    with ctx:
        for _ in range(args.runs):
            before = inventory(work_db)
            run = replay_run(work_db, args.config, corpus).as_dict()
            after = inventory(work_db)
            run["orphans_after"] = after
            run["new_orphans"] = new_orphans(before, after)
            runs.append(run)
    report = {
        "source_db": str(args.source_db), "work_db": str(work_db),
        "corpus": {k: len(v) for k, v in sorted(corpus.by_source.items())},
        "fake_embeddings": args.fake_embeddings, "orphans_before": initial, "runs": runs,
        "limits": ["corpus = stored offers only (offers only ever rejected are absent)",
                   "no job source contacted: non-loopback sockets refused during each run"],
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    (args.work_dir / "replay-report.json").write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0 if all(r["status"] == "completed" for r in runs) else 1


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_embedding_class() -> type:
    import hashlib

    from src.embeddings.embedding import BatchEmbeddingResult, EmbeddingService

    class FakeEmbeddingService(EmbeddingService):
        def __init__(self, *a: object, **k: object) -> None:
            super().__init__(model_name="fake")

        @staticmethod
        def _vec(text: str) -> list[float]:
            h = hashlib.sha256(text.encode()).digest()
            return [(b + 1) / 256 for b in h[:8]]

        def embed(self, text: str) -> list[float]:  # type: ignore[override]
            return self._vec(text)

        def embed_batch(self, texts, ids=None):  # type: ignore[no-untyped-def,override]
            return BatchEmbeddingResult(ids=list(ids or range(len(texts))), vectors=[self._vec(t) for t in texts])

    return FakeEmbeddingService


if __name__ == "__main__":
    sys.exit(main())
