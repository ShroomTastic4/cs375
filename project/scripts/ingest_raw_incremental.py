#!/usr/bin/env python3
"""
Incrementally ingest additional COCO images and a new VisDrone sequence
into the existing raw tables via INSERT (not replace), demonstrating the
local-storage-to-lakehouse flow and producing a new snapshot.

Run inside the lab container, after ingest_raw.py has already populated raw:
    docker compose exec lab python scripts/ingest_raw_incremental.py
"""

from collections import defaultdict

import duckdb
import pandas as pd
import requests

from ingest_raw import (
    BUCKET,
    COCO_REPO,
    HF_HEADERS,
    LOCAL_STORE,
    SQL_DIR,
    VISDRONE_FRAMES_PER_SEQUENCE,
    VISDRONE_REPO,
    _frame_order,
    fetch_coco_annotations,
    hf_dataset_files,
    hf_resolve_url,
    put_blob,
)

COCO_INCREMENT_SIZE = 10
VISDRONE_INCREMENT_SEQUENCES = 1


def ingest_coco_increment(con):
    print(f"[COCO incremental] looking for {COCO_INCREMENT_SIZE} new images from {COCO_REPO}")
    existing_ids = {row[0] for row in con.sql("SELECT DISTINCT image_id FROM raw.coco_annotations").fetchall()}
    all_files = sorted(hf_dataset_files(COCO_REPO, "images/"))
    new_files = [f for f in all_files if int(f.rsplit("/", 1)[-1].split(".")[0]) not in existing_ids]
    chosen = new_files[:COCO_INCREMENT_SIZE]
    if not chosen:
        print("  no new COCO images available")
        return

    uri_by_id = {}
    for path in chosen:
        fname = path.rsplit("/", 1)[-1]
        img_id = int(fname.split(".")[0])
        data = requests.get(hf_resolve_url(COCO_REPO, path), headers=HF_HEADERS, timeout=30).content
        uri_by_id[img_id] = put_blob(f"raw/coco/images/{fname}", data, "image/jpeg")
    print(f"  uploaded {len(uri_by_id)} new image blobs to s3://{BUCKET}/raw/coco/images/")

    coco = fetch_coco_annotations()
    images_by_id = {im["id"]: im for im in coco["images"] if im["id"] in uri_by_id}
    anns_by_image = defaultdict(list)
    for a in coco["annotations"]:
        if a["image_id"] in uri_by_id:
            anns_by_image[a["image_id"]].append(a)

    rows = []
    for img_id, uri in uri_by_id.items():
        img = images_by_id.get(img_id, {})
        anns = anns_by_image.get(img_id, [])
        base = {
            "image_id": img_id,
            "file_name": img.get("file_name"),
            "uri": uri,
            "width": img.get("width"),
            "height": img.get("height"),
        }
        if not anns:
            rows.append({**base, "annotation_id": None, "category_id": None, "bbox": None, "area": None, "iscrowd": None})
        for a in anns:
            rows.append({
                **base,
                "annotation_id": a["id"],
                "category_id": a["category_id"],
                "bbox": a["bbox"],
                "area": a["area"],
                "iscrowd": a["iscrowd"],
            })

    staged = LOCAL_STORE / "coco_annotations_increment.parquet"
    pd.DataFrame(rows).to_parquet(staged)
    print(f"  staged {len(rows)} new annotation rows at {staged} (local-storage staging area)")

    con.execute(f"INSERT INTO raw.coco_annotations SELECT * FROM read_parquet('{staged}')")
    print("  inserted into raw.coco_annotations -> lakehouse")


def ingest_visdrone_increment(con):
    print(f"[VisDrone incremental] looking for {VISDRONE_INCREMENT_SEQUENCES} new sequence(s) from {VISDRONE_REPO}")
    existing_videos = {row[0] for row in con.sql("SELECT DISTINCT video_id FROM raw.visdrone_fragments").fetchall()}
    files = hf_dataset_files(VISDRONE_REPO, "data/")
    sequences = defaultdict(list)
    for path in files:
        fname = path.rsplit("/", 1)[-1]
        seq_id = fname.split("-")[0].split(".")[0]
        sequences[seq_id].append(fname)
    full_sequences = sorted(
        seq for seq, frames in sequences.items()
        if len(frames) == VISDRONE_FRAMES_PER_SEQUENCE and seq not in existing_videos
    )
    chosen_ids = full_sequences[:VISDRONE_INCREMENT_SEQUENCES]
    chosen = {seq: sorted(sequences[seq], key=_frame_order) for seq in chosen_ids}
    if not chosen:
        print("  no new VisDrone sequences available")
        return

    rows = []
    for seq_id, frames in chosen.items():
        for idx, fname in enumerate(frames, start=1):
            data = requests.get(hf_resolve_url(VISDRONE_REPO, f"data/{fname}"), headers=HF_HEADERS, timeout=30).content
            uri = put_blob(f"raw/visdrone/{seq_id}/{fname}", data, "image/jpeg")
            rows.append({
                "video_id": seq_id,
                "fragment_index": idx,
                "fragment_count": len(frames),
                "filename": fname,
                "uri": uri,
                "byte_size": len(data),
            })

    print(f"  uploaded {len(rows)} new frame blobs across {len(chosen)} sequence(s)")
    staged = LOCAL_STORE / "visdrone_fragments_increment.parquet"
    pd.DataFrame(rows).to_parquet(staged)
    print(f"  staged {len(rows)} new fragment rows at {staged} (local-storage staging area)")

    con.execute(f"INSERT INTO raw.visdrone_fragments SELECT * FROM read_parquet('{staged}')")
    print("  inserted into raw.visdrone_fragments -> lakehouse")


def main():
    con = duckdb.connect()
    con.execute((SQL_DIR / "00_attach.sql").read_text())

    before = con.sql("SELECT max(snapshot_id) FROM ducklake_snapshots('lake')").fetchone()[0]

    ingest_coco_increment(con)
    ingest_visdrone_increment(con)

    print("\n=== New snapshot(s) from this incremental load ===")
    print(con.sql(f"""
        SELECT snapshot_id, schema_version, changes
        FROM ducklake_snapshots('lake')
        WHERE snapshot_id > {before}
        ORDER BY snapshot_id
    """))


if __name__ == "__main__":
    main()
