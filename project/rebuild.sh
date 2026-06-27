#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY_DEPS=(duckdb datasets huggingface_hub boto3 pandas requests)

docker compose up -d --force-recreate lab
docker compose exec lab pip install "${PY_DEPS[@]}"

docker compose exec lab python3 -c "
import os, boto3
s3 = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT'],
                   aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
                   aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'])
try:
    s3.create_bucket(Bucket='lakehouse')
    print('Created bucket lakehouse')
except s3.exceptions.ClientError as e:
    if 'BucketAlreadyOwnedByYou' in str(e) or 'BucketAlreadyExists' in str(e):
        print('Bucket lakehouse already exists')
    else:
        raise
"
