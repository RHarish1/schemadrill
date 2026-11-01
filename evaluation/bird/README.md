# BIRD Mini-Dev

BIRD is an external benchmark and is not included in this repository. Download the
licensed Mini-Dev split from the official BIRD release and place its JSON file under
`evaluation/bird/` (for example, `evaluation/bird/dev.json`). Do not commit the dataset
or database dumps.

The loader accepts the standard BIRD fields `question`, `evidence`, `SQL`, and `db_id`.
The corresponding databases must be available through the configured PostgreSQL
connection, with `db_id` values mapped to the database/schema names used by SchemaDrill.

Run a credential-free evaluator smoke check against the loaded databases:

```sh
python -m evaluation.runner \
  --bird-root evaluation/bird \
  --split dev \
  --gold-as-generated
```

Run live generation and write JSONL records:

```sh
python -m evaluation.runner \
  --bird-root evaluation/bird \
  --split dev \
  --output benchmark-results/bird-dev.jsonl
```

BIRD runs are explicit rather than part of the normal push/PR job because the dataset,
database setup, and Gemini credentials are external prerequisites.