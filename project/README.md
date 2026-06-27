# Lakehouse Project: DuckLake + RustFS + Hugging Face

A versioned medallion lakehouse (raw -> silver -> gold) over a self-hosted RustFS S3 layer,
catalogued with DuckLake, populated with COCO (images) and VisDrone (video frame sequences),
and round-tripped through the Hugging Face Hub.

See [REPORT.md](REPORT.md) for the design-principle write-up and [DEMO.md](DEMO.md) for a short
live-demo script.

## Prerequisites

- Docker + Docker Compose
- A Hugging Face token with write access (for the Hub round-trip), in `project/.env`:
  ```
  HF_TOKEN=hf_...
  ```
  (`.env` is gitignored - never commit a real token.)

## Run instructions

All commands below run from this directory (`project/`).

### 1. Bring up the stack and build the lakehouse from scratch

```bash
./rebuild.sh
```

This empties the RustFS `lakehouse` bucket, resets the local DuckLake catalog
(`metadata.ducklake`), then:
- starts `rustfs` (S3-compatible object storage) and `lab` (the DuckDB/Python environment),
- installs Python deps in `lab`,
- runs `scripts/ingest_raw.py` - samples COCO images from `aegean-ai/coco-25k` + official COCO
  annotations, and VisDrone frame sequences + real per-frame detections from
  `Voxel51/visdrone-mot`, uploads the image/frame bytes to RustFS, and lands the raw layer,
- runs `scripts/build_silver_gold.py` - builds silver and gold, and runs the COCO crowded-scenes
  query plus the VisDrone busy-fragments query.

### 2. Inspect the populated lakehouse

```bash
docker compose exec lab python scripts/show_lakehouse_state.py
```

Prints row counts across every raw/silver/gold table, sample rows with their `uri` columns, the
actual RustFS object counts, and a direct cross-check that catalog URIs resolve to real RustFS
objects.

### 3. Version control: time travel, snapshot diff, rollback

```bash
docker compose exec lab python scripts/version_control_demo.py
```

Walks the snapshot history (with DuckLake's own change log as the audit trail), time-travels
across a schema rename by version and by timestamp, deliberately corrupts a column, diffs the
two snapshots, and rolls back.

### 4. Incremental ingest (new data, new snapshot)

```bash
docker compose exec lab python scripts/ingest_raw_incremental.py
```

Pulls additional COCO images and one more VisDrone sequence not already in `raw`, stages them
under `local-store/` (the local Docker host staging area), and `INSERT`s into the existing raw
tables.

### 5. Push a gold table back to the Hugging Face Hub

```bash
docker compose exec lab python scripts/push_gold_to_hub.py
```

Publishes `gold.coco_labels` and `gold.visdrone_training` as HF dataset repos:
- https://huggingface.co/datasets/Acender/cs375-coco-gold
- https://huggingface.co/datasets/Acender/cs375-visdrone-gold

## Layout

```
docker-compose.yml      # rustfs (S3) + lab (DuckDB/Python) services
sql/
  00_attach.sql         # extensions + S3 secret + ATTACH DuckLake
  10_raw.sql            # land COCO/VisDrone parquet manifests into the raw layer
  20_silver.sql         # raw -> silver: types, missing values, dedup, schema evolution
  30_gold.sql           # silver -> gold: ML-ready label/fragment tables
scripts/
  ingest_raw.py             # initial raw population (images/frames -> RustFS, metadata -> raw)
  ingest_raw_incremental.py # incremental raw load (new snapshot) - the HF round-trip's ingest half
  build_silver_gold.py      # runs 20_silver.sql/30_gold.sql + the COCO and VisDrone demo queries
  version_control_demo.py   # time travel, snapshot diff, rollback demo
  push_gold_to_hub.py       # the HF round-trip's publish half
  show_lakehouse_state.py   # prints populated-lakehouse evidence across all layers
rebuild.sh              # rebuild the whole lakehouse from an empty bucket
REPORT.md               # design-principle report
DEMO.md                 # live-demo runbook
```

## Notes on data sources

- **COCO**: `aegean-ai/coco-25k` (course-hosted image subset) for image bytes, joined against the
  official COCO `instances_train2017.json` annotations (downloaded once, cached under
  `local-store/coco_annotations_src/`) for real bounding boxes/categories.
- **VisDrone**: `Voxel51/visdrone-mot` - a FiftyOne export of VisDrone2019-MOT sequences. Each
  7-frame sequence is treated as one "clip"; its `samples.json` provides real per-frame
  detections (label, bounding box, confidence, occlusion), which feed `n_objects`/`classes` on
  the fragment index. The official VisDrone Task 2 (VID) split has no public HF mirror, so MOT
  (a closely related video task in the same benchmark, also with per-frame bounding boxes) is
  used instead - documented here for transparency.
- `local-store/`, `metadata.ducklake`, `rustfs-data/`, `rustfs-logs/` are all gitignored -
  generated/cached state, fully reproduced by `./rebuild.sh`.
