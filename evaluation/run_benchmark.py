"""CLI for reproducible SchemaDrill benchmark runs."""

from __future__ import annotations

import argparse
import json

from evaluation.benchmark import MATRIX_CONFIGS, run_benchmark
from evaluation.datasets import DATASETS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions")
    parser.add_argument("--dataset", choices=sorted(DATASETS), default="chinook")
    parser.add_argument("--all-datasets", action="store_true")
    parser.add_argument("--output-dir", default="benchmark-results")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--gold-as-generated",
        action="store_true",
        help="Use fixture SQL instead of an LLM for credential-free evaluator smoke runs",
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Initialize the selected fixture (local database hosts only)",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="Run all retry/FK configurations independently",
    )
    parser.add_argument(
        "--config",
        action="append",
        metavar="JSON",
        help=("Configuration JSON; may be repeated " '(for example \'{"name":"retry",...}\')'),
    )
    args = parser.parse_args()
    configs = [json.loads(value) for value in args.config] if args.config else None
    if args.matrix and configs:
        parser.error("--matrix and --config cannot be combined")
    if args.matrix:
        configs = list(MATRIX_CONFIGS)
    datasets = list(DATASETS) if args.all_datasets else [args.dataset]
    summaries = {}
    for dataset in datasets:
        summary = run_benchmark(
            args.questions if len(datasets) == 1 else None,
            args.output_dir,
            dataset=dataset,
            configs=configs,
            limit=args.limit,
            seed=args.seed,
            gold_as_generated=args.gold_as_generated,
            initialize=args.initialize,
        )
        summaries[dataset] = summary
    print(
        json.dumps(
            summaries if len(summaries) > 1 else next(iter(summaries.values())),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
