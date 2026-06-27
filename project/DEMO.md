# Live Demo Runbook

~12 minutes, run from the `project/` directory. Everything executes inside the `lab` container.

## 1. Rebuild from nothing (reproducibility)

```
./rebuild.sh
```

Narrate: this empties the RustFS `lakehouse` bucket, deletes the local DuckLake catalog, then
replays raw -> silver -> gold from the original sources (`aegean-ai/coco-25k` + official COCO
annotations, `Voxel51/visdrone-mot` images + its real per-frame detection labels). End state is a
freshly versioned lakehouse, snapshot 0 through ~13, built from a literally empty bucket.

## 2. Show the populated lakehouse

```
docker compose exec lab python scripts/show_lakehouse_state.py
```

Narrate: row counts across every raw/silver/gold table for both datasets, sample rows with `uri`
columns (never pixels), the actual RustFS object counts under `raw/coco/images/` and
`raw/visdrone/`, and a direct cross-check that those catalog URIs resolve to real RustFS objects.

## 3. The video-fragment query and the COCO metadata query

```
docker compose exec lab python scripts/build_silver_gold.py
```

Narrate: the COCO query for crowded/object-dense scenes over `gold.coco_labels`; then the VisDrone
"busy fragments" query - `WHERE n_objects > <75th-percentile threshold>` over real per-frame
detection counts (sourced from `raw.visdrone_detections`, not a file-size proxy) - and the
before/after byte comparison proving only the selected fragments were pulled from RustFS, not
whole clips (last real run: 4 busy frames, 1.9MB, vs. 12.1MB across all 28 frames in those clips).

## 4. Version control: time travel + rollback

```
docker compose exec lab python scripts/version_control_demo.py
```

Narrate while it runs: full snapshot history with DuckLake's own `changes` column as the audit
log; time travel across the `is_crowd` -> `is_crowd_annotation` schema rename, by version and by
timestamp; a deliberately corrupted `UPDATE`; the two-snapshot diff via `ducklake_table_changes`;
the rollback; and that the bad snapshot is still queryable afterward (history is immutable -
rollback only adds a corrective snapshot).

## 5. Incremental ingest: new data, new snapshot

```
docker compose exec lab python scripts/ingest_raw_incremental.py
```

Narrate: pulls COCO images/VisDrone frames (with their real detections) not already in `raw`,
stages them as local Parquet in `local-store/` (the "local Docker host storage" half), then
`INSERT`s into the existing raw tables - watch new snapshots appear with `tables_inserted_into` in
their `changes` map, with no data rewritten.

## 6. Close the loop: push gold back to the Hub

```
docker compose exec lab python scripts/push_gold_to_hub.py
```

Narrate: `gold.coco_labels` and `gold.visdrone_training` each become a Hugging Face dataset repo
(`Acender/cs375-coco-gold`, `Acender/cs375-visdrone-gold`) via
`Dataset.from_parquet(...).push_to_hub(...)` - the lakehouse's curated output is now consumable
the same way the raw input was pulled in.

## 7. Q&A

Point to `REPORT.md` for the six conceptual questions (metadata/data separation, snapshots and
time travel, uniqueness without constraints, tracing an INSERT to bytes, why a SQL catalog, and
why pixels never enter a table).
