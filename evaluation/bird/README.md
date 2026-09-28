# BIRD Mini-Dev

Place the licensed BIRD Mini-Dev dataset here when it is available locally. Do not commit the dataset or database dumps to this repository.

Run it explicitly with:

```sh
python -m evaluation.runner --questions evaluation/bird/mini_dev.json --dataset bird-mini-dev
```

BIRD is intentionally not part of the normal GitHub push/PR job. Use a manual or scheduled workflow after validating the local evaluator against the reference result semantics.