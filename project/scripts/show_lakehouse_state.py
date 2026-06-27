#!/usr/bin/env python3
"""
Show the populated lakehouse: row counts and sample rows for every raw/
silver/gold table, cross-checked against the actual objects in RustFS -
evidence that the catalog's URIs point at real bytes, not just metadata.

Run inside the lab container:
    docker compose exec lab python scripts/show_lakehouse_state.py
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
import duckdb

SQL_DIR = Path("sql")
BUCKET = "lakehouse"

TABLES = [
    "raw.coco_annotations",
    "raw.coco_categories",
    "raw.visdrone_fragments",
    "silver.coco_annotations",
    "silver.visdrone_fragments",
    "gold.coco_labels",
    "gold.visdrone_training",
]

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)


def show_tables(con):
    print("=== Row counts across all medallion layers ===")
    for table in TABLES:
        count = con.sql(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<32} {count:>5} rows")

    print("\n=== Sample rows (with URIs) ===")
    for table in ("raw.coco_annotations", "raw.visdrone_fragments", "gold.coco_labels", "gold.visdrone_training"):
        print(f"\n-- {table} --")
        print(con.sql(f"SELECT * FROM {table} LIMIT 3"))


def show_rustfs_objects():
    print("\n=== RustFS object counts (the actual image/video bytes) ===")
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in ("raw/coco/images/", "raw/visdrone/"):
        objects = [o for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix) for o in page.get("Contents", [])]
        total_bytes = sum(o["Size"] for o in objects)
        print(f"  s3://{BUCKET}/{prefix}  -> {len(objects)} object(s), {total_bytes:,} bytes")


def cross_check_uris(con):
    print("\n=== Cross-check: catalog URIs actually resolve to RustFS objects ===")
    sample_uris = con.sql("""
        (SELECT uri FROM raw.coco_annotations LIMIT 2)
        UNION ALL
        (SELECT uri FROM raw.visdrone_fragments LIMIT 2)
    """).fetchall()
    for (uri,) in sample_uris:
        parsed = urlparse(uri)
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        head = s3.head_object(Bucket=bucket, Key=key)
        print(f"  OK  {uri}  ({head['ContentLength']} bytes, {head['ContentType']})")


def main():
    con = duckdb.connect()
    con.execute((SQL_DIR / "00_attach.sql").read_text())

    show_tables(con)
    show_rustfs_objects()
    cross_check_uris(con)


if __name__ == "__main__":
    main()
