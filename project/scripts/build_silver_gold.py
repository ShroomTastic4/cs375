#!/usr/bin/env python3
"""
Build the silver and gold layers on top of the raw layer, then run two
demo queries: a COCO metadata query (crowded scenes) and a VisDrone
fragment query that proves only the selected fragments are read from
RustFS, not whole clips.

Run inside the lab container:
    docker compose exec lab python scripts/ingest_raw.py   # if not already done
    docker compose exec lab python scripts/build_silver_gold.py
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
import duckdb

SQL_DIR = Path("sql")

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)


def uri_to_bucket_key(uri):
    parsed = urlparse(uri)
    return parsed.netloc, parsed.path.lstrip("/")


def demo_coco_crowded_scenes(con):
    print("\n=== COCO metadata query: crowded scenes (iscrowd flag) ===")
    print(con.sql("""
        SELECT image_id, file_name, object_count, labels
        FROM gold.coco_labels
        WHERE has_crowd_annotation
        ORDER BY object_count DESC
    """))

    print("\n=== COCO metadata query: most object-dense scenes (fallback view) ===")
    print(con.sql("""
        SELECT image_id, file_name, object_count, primary_label, split
        FROM gold.coco_labels
        ORDER BY object_count DESC
        LIMIT 5
    """))


def demo_visdrone_selective_read(con):
    print("\n=== VisDrone fragment query: busy fragments by real per-frame detection counts ===")
    threshold = con.sql("SELECT quantile_cont(n_objects, 0.75) FROM gold.visdrone_training").fetchone()[0]
    print(f"(threshold = 75th percentile of n_objects across all fragments = {threshold})\n")

    query = f"""
        SELECT video_id, fragment_index, uri, byte_size, n_objects, classes
        FROM gold.visdrone_training
        WHERE n_objects > {threshold}
        ORDER BY n_objects DESC
        LIMIT 100
    """
    selected = con.sql(query).fetchall()
    print(con.sql(query))

    selected_video_ids = sorted({row[0] for row in selected})
    totals = con.sql(f"""
        SELECT video_id, count(*) AS frame_count, sum(byte_size) AS total_bytes
        FROM gold.visdrone_training
        WHERE video_id IN ({", ".join("'" + v + "'" for v in selected_video_ids)})
        GROUP BY video_id
    """).fetchall()
    total_bytes_by_video = {row[0]: (row[1], row[2]) for row in totals}

    fetched_bytes = 0
    for video_id, fragment_index, uri, byte_size, n_objects, classes in selected:
        bucket, key = uri_to_bucket_key(uri)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert len(body) == byte_size
        fetched_bytes += len(body)

    clip_frame_count, clip_total_bytes = 0, 0
    for frame_count, total in total_bytes_by_video.values():
        clip_frame_count += frame_count
        clip_total_bytes += total

    print(f"\nFetched {len(selected)} busy-fragment object(s), {fetched_bytes} bytes total.")
    print(f"Those same {len(selected_video_ids)} video(s) have {clip_frame_count} frames "
          f"totaling {clip_total_bytes} bytes.")
    print(f"-> read {fetched_bytes / clip_total_bytes:.1%} of the clip bytes: "
          f"confirms only the selected fragments were read from RustFS, not whole clips.")


def main():
    con = duckdb.connect()
    con.execute((SQL_DIR / "00_attach.sql").read_text())
    con.execute((SQL_DIR / "20_silver.sql").read_text())
    con.execute((SQL_DIR / "30_gold.sql").read_text())

    print("=== New snapshots from this run ===")
    print(con.sql("""
        SELECT snapshot_id, schema_version, changes
        FROM ducklake_snapshots('lake')
        ORDER BY snapshot_id DESC
        LIMIT 8
    """))

    demo_coco_crowded_scenes(con)
    demo_visdrone_selective_read(con)


if __name__ == "__main__":
    main()
