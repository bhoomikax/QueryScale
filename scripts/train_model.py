#!/usr/bin/env python3
"""Train and evaluate the index-benefit model.

By default the model is trained only from measured benchmark runs in the
benchmark history. ``--synthetic-demo`` instead trains on explicitly synthetic
labels and writes to ``models/demo``; that model is never used for ranking.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.services.analysis import run_training


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the QueryScale index-benefit model.")
    parser.add_argument("--synthetic-demo", action="store_true", help="Train on synthetic demonstration labels (models/demo).")
    parser.add_argument("--strict-dataset", action="store_true", help="Only combine runs from an identical dataset.")
    parser.add_argument("--no-rerank", action="store_true", help="Do not regenerate recommendations after training.")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    result = run_training({
        "synthetic_demo": args.synthetic_demo,
        "strict_dataset": args.strict_dataset,
        "rerank": not args.no_rerank,
        "seed": args.seed,
    })
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in {"trained", "insufficient_data"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
