#!/usr/bin/env bash
# Rebuilds the whole lakehouse from scratch: empties the RustFS bucket,
# resets the local DuckLake catalog, then replays raw -> silver -> gold.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY_DEPS=(duckdb datasets huggingface_hub boto3 pandas requests pytz)

docker compose up -d --force-recreate lab
docker compose exec lab pip install "${PY_DEPS[@]}"

echo "Resetting the local DuckLake catalog..."
rm -f metadata.ducklake

echo "Emptying the lakehouse bucket..."
docker compose exec lab python3 -c "
import os, boto3
s3 = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT'],
                   aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
                   aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'])
try:
    s3.create_bucket(Bucket='lakehouse')
    print('Created bucket lakehouse')
except s3.exceptions.ClientError as e:
    if 'BucketAlreadyOwnedByYou' not in str(e) and 'BucketAlreadyExists' not in str(e):
        raise

paginator = s3.get_paginator('list_objects_v2')
keys = [o['Key'] for page in paginator.paginate(Bucket='lakehouse') for o in page.get('Contents', [])]
if keys:
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        s3.delete_objects(Bucket='lakehouse', Delete={'Objects': [{'Key': k} for k in batch]})
    print(f'Deleted {len(keys)} object(s) - starting from an empty bucket')
else:
    print('Bucket lakehouse already empty')
"

echo "Rebuilding the lakehouse: raw -> silver -> gold..."
docker compose exec lab python scripts/ingest_raw.py
docker compose exec lab python scripts/build_silver_gold.py
