#!/usr/bin/env python3
"""
Push gold tables back to the Hugging Face Hub, closing the loop between
the lakehouse and HF.

Run inside the lab container, after build_silver_gold.py:
    docker compose exec lab python scripts/push_gold_to_hub.py
"""

import os
from pathlib import Path

import duckdb
from datasets import Dataset

SQL_DIR = Path("sql")
LOCAL_STORE = Path("/data/local")

TARGETS = [
    ("gold.coco_labels", "Acender/cs375-coco-gold"),
    ("gold.visdrone_training", "Acender/cs375-visdrone-gold"),
]


def main():
    con = duckdb.connect()
    con.execute((SQL_DIR / "00_attach.sql").read_text())
    token = os.environ["HF_TOKEN"]

    for table, repo_id in TARGETS:
        local_path = LOCAL_STORE / f"{table.replace('.', '_')}.parquet"
        con.execute(f"COPY {table} TO '{local_path}' (FORMAT PARQUET)")
        print(f"Exported {table} -> {local_path}")

        ds = Dataset.from_parquet(str(local_path))
        print(f"Pushing {table} ({len(ds)} rows) -> https://huggingface.co/datasets/{repo_id}")
        ds.push_to_hub(repo_id, private=False, token=token)
        print(f"  done: https://huggingface.co/datasets/{repo_id}")


if __name__ == "__main__":
    main()
