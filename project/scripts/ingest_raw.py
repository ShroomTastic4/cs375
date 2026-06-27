#!/usr/bin/env python3
"""
Land a small sample of COCO (images) and VisDrone (video frame sequences)
into the raw layer: blobs as RustFS objects, metadata as DuckLake tables
of URIs (no pixels in the catalog).

Run inside the lab container:
    docker compose exec lab python scripts/ingest_raw.py
"""

import json
import os
import zipfile
from collections import defaultdict
from pathlib import Path

import boto3
import duckdb
import pandas as pd
import requests

BUCKET = "lakehouse"
LOCAL_STORE = Path("/data/local")
SQL_DIR = Path("sql")

COCO_REPO = "aegean-ai/coco-25k"
COCO_SAMPLE_SIZE = 25
COCO_ANNOTATIONS_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
COCO_ANNOTATIONS_CACHE = LOCAL_STORE / "coco_annotations_src" / "instances_train2017.json"

VISDRONE_REPO = "Voxel51/visdrone-mot"
VISDRONE_SEQUENCE_COUNT = 3
VISDRONE_FRAMES_PER_SEQUENCE = 7
VISDRONE_SAMPLES_CACHE = LOCAL_STORE / "visdrone_samples_src" / "samples.json"
# The dataset's own FiftyOne metadata.json sets dynamic_groups_target_frame_rate=30;
# there's no per-sequence timestamp in samples.json, so this is used to derive start/end times.
VISDRONE_FPS = 30

HF_HEADERS = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.environ.get("HF_TOKEN") else {}

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["S3_ENDPOINT"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)


def ensure_bucket():
    try:
        s3.create_bucket(Bucket=BUCKET)
        print(f"Created bucket '{BUCKET}'")
    except s3.exceptions.ClientError as e:
        if "BucketAlreadyOwnedByYou" not in str(e) and "BucketAlreadyExists" not in str(e):
            raise


def hf_dataset_files(repo, prefix):
    r = requests.get(f"https://huggingface.co/api/datasets/{repo}", headers=HF_HEADERS, timeout=30)
    r.raise_for_status()
    files = [s["rfilename"] for s in r.json()["siblings"]]
    return [f for f in files if f.startswith(prefix)]


def hf_resolve_url(repo, path):
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def put_blob(key, data, content_type):
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
    return f"s3://{BUCKET}/{key}"


# ---------------------------------------------------------------------------
# COCO: sample images -> raw blobs, official annotations -> raw.coco_annotations
# ---------------------------------------------------------------------------

def fetch_coco_annotations():
    if COCO_ANNOTATIONS_CACHE.exists():
        print(f"  Using cached annotations at {COCO_ANNOTATIONS_CACHE}")
        return json.loads(COCO_ANNOTATIONS_CACHE.read_text())

    print("  Downloading COCO train2017 instance annotations (~240MB, cached after first run)...")
    r = requests.get(COCO_ANNOTATIONS_URL, stream=True, timeout=120)
    r.raise_for_status()
    zip_path = LOCAL_STORE / "annotations_trainval2017.zip"
    with open(zip_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    COCO_ANNOTATIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf, zf.open("annotations/instances_train2017.json") as src:
        COCO_ANNOTATIONS_CACHE.write_bytes(src.read())
    zip_path.unlink()
    return json.loads(COCO_ANNOTATIONS_CACHE.read_text())


def ingest_coco():
    print(f"[COCO] sampling {COCO_SAMPLE_SIZE} images from {COCO_REPO}")
    filenames = sorted(hf_dataset_files(COCO_REPO, "images/"))[:COCO_SAMPLE_SIZE]

    uri_by_id = {}
    for path in filenames:
        fname = path.rsplit("/", 1)[-1]
        img_id = int(fname.split(".")[0])
        data = requests.get(hf_resolve_url(COCO_REPO, path), headers=HF_HEADERS, timeout=30).content
        uri_by_id[img_id] = put_blob(f"raw/coco/images/{fname}", data, "image/jpeg")
    print(f"  uploaded {len(uri_by_id)} image blobs to s3://{BUCKET}/raw/coco/images/")

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
                "iscrowd": a["iscrowd"],  # native 0/1 from the source - silver casts this to BOOLEAN
            })

    out = LOCAL_STORE / "coco_annotations.parquet"
    pd.DataFrame(rows).to_parquet(out)
    print(f"  wrote {len(rows)} annotation rows to {out}")

    categories_out = LOCAL_STORE / "coco_categories.parquet"
    pd.DataFrame(coco["categories"]).rename(columns={"id": "category_id"}).to_parquet(categories_out)
    print(f"  wrote {len(coco['categories'])} category rows to {categories_out}")


# ---------------------------------------------------------------------------
# VisDrone: sample frame sequences -> raw blobs, fragment index + per-frame
# detections -> raw.visdrone_fragments / raw.visdrone_detections
# ---------------------------------------------------------------------------

def _frame_order(filename):
    stem = filename.rsplit(".", 1)[0]
    return int(stem.split("-")[1]) if "-" in stem else 1


def sample_visdrone_sequences(n):
    files = hf_dataset_files(VISDRONE_REPO, "data/")
    sequences = defaultdict(list)
    for path in files:
        fname = path.rsplit("/", 1)[-1]
        seq_id = fname.split("-")[0].split(".")[0]
        sequences[seq_id].append(fname)
    full_sequences = sorted(seq for seq, frames in sequences.items() if len(frames) == VISDRONE_FRAMES_PER_SEQUENCE)
    chosen = full_sequences[:n]
    return {seq: sorted(sequences[seq], key=_frame_order) for seq in chosen}


def fetch_visdrone_samples():
    """Real per-frame detections (label, bbox, confidence, occlusion) from the
    dataset's own FiftyOne export - keyed by filepath, e.g. 'data/0000001.jpg'."""
    if VISDRONE_SAMPLES_CACHE.exists():
        print(f"  Using cached samples.json at {VISDRONE_SAMPLES_CACHE}")
        samples = json.loads(VISDRONE_SAMPLES_CACHE.read_text())["samples"]
    else:
        print("  Downloading VisDrone-MOT samples.json (~60MB, cached after first run)...")
        r = requests.get(hf_resolve_url(VISDRONE_REPO, "samples.json"), headers=HF_HEADERS, timeout=120)
        r.raise_for_status()
        VISDRONE_SAMPLES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        VISDRONE_SAMPLES_CACHE.write_bytes(r.content)
        samples = json.loads(r.content)["samples"]
    return {s["filepath"]: s for s in samples}


def ingest_visdrone():
    print(f"[VisDrone] sampling {VISDRONE_SEQUENCE_COUNT} sequences from {VISDRONE_REPO}")
    sequences = sample_visdrone_sequences(VISDRONE_SEQUENCE_COUNT)
    samples_by_filepath = fetch_visdrone_samples()

    fragment_rows = []
    detection_rows = []
    for seq_id, frames in sequences.items():
        for idx, fname in enumerate(frames, start=1):
            data = requests.get(hf_resolve_url(VISDRONE_REPO, f"data/{fname}"), headers=HF_HEADERS, timeout=30).content
            uri = put_blob(f"raw/visdrone/{seq_id}/{fname}", data, "image/jpeg")

            sample = samples_by_filepath.get(f"data/{fname}", {})
            detections = sample.get("detections", [])
            frame_number = sample.get("frame_number")

            fragment_rows.append({
                "video_id": seq_id,
                "fragment_index": idx,
                "fragment_count": len(frames),
                "filename": fname,
                "uri": uri,
                "byte_size": len(data),
                "scene_id": sample.get("scene_id"),
                "frame_number": frame_number,
                "start_frame": frame_number,
                "end_frame": frame_number,
                "start_time": frame_number / VISDRONE_FPS if frame_number is not None else None,
                "end_time": frame_number / VISDRONE_FPS if frame_number is not None else None,
                "n_objects": len(detections),
                "classes": sorted({d["label"] for d in detections}),
            })

            for d in detections:
                detection_rows.append({
                    "video_id": seq_id,
                    "fragment_index": idx,
                    "uri": uri,
                    "label": d["label"],
                    "bbox": d["bounding_box"],
                    "confidence": d.get("confidence"),
                    "visibility": d.get("visibility"),
                    "occlusion": d.get("occlusion"),
                })

    print(f"  uploaded {len(fragment_rows)} frame blobs across {len(sequences)} sequences to s3://{BUCKET}/raw/visdrone/")
    out = LOCAL_STORE / "visdrone_fragments.parquet"
    pd.DataFrame(fragment_rows).to_parquet(out)
    print(f"  wrote {len(fragment_rows)} fragment-index rows to {out}")

    detections_out = LOCAL_STORE / "visdrone_detections.parquet"
    pd.DataFrame(detection_rows).to_parquet(detections_out)
    print(f"  wrote {len(detection_rows)} per-frame detection rows to {detections_out}")


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------

def checkpoint(con):
    print("\n=== Checkpoint ===")
    print("\nducklake_snapshots('lake'):")
    print(con.sql("FROM ducklake_snapshots('lake')"))

    print("\nRustFS raw objects (sample):")
    for prefix in ("raw/coco/images/", "raw/visdrone/"):
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=5)
        for obj in resp.get("Contents", []):
            print(f"  {obj['Key']}  ({obj['Size']} bytes)")

    print("\nraw.coco_annotations sample URIs:")
    print(con.sql("SELECT DISTINCT image_id, file_name, uri FROM raw.coco_annotations LIMIT 5"))

    print("\nraw.visdrone_fragments sample URIs:")
    print(con.sql("SELECT video_id, fragment_index, uri, n_objects, classes FROM raw.visdrone_fragments LIMIT 5"))

    print("\nraw.visdrone_detections sample rows:")
    print(con.sql("SELECT video_id, fragment_index, label, bbox FROM raw.visdrone_detections LIMIT 5"))


def main():
    ensure_bucket()
    ingest_coco()
    ingest_visdrone()

    con = duckdb.connect()
    con.execute((SQL_DIR / "00_attach.sql").read_text())
    con.execute((SQL_DIR / "10_raw.sql").read_text())
    checkpoint(con)


if __name__ == "__main__":
    main()
