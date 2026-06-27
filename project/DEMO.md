# Live Demo Runbook

~10 minutes, run from the `project/` directory. Everything executes inside the `lab` container.

## 1. Rebuild from nothing (reproducibility)

```
./rebuild.sh
```

Narrate: this empties the RustFS `lakehouse` bucket, deletes the local DuckLake catalog, then
replays raw -> silver -> gold from the original sources (`aegean-ai/coco-25k` + official COCO
annotations, `Voxel51/visdrone-mot`). End state is a freshly versioned lakehouse, snapshot 0
through ~11, built from a literally empty bucket.

## 2. Spot-check the medallion layers

```
docker compose exec lab python3 -c "
import duckdb
con = duckdb.connect()
con.execute(open('sql/00_attach.sql').read())
print(con.sql('SHOW ALL TABLES'))
"
```

Narrate: raw/silver/gold schemas, all backed by the one RustFS bucket; point out `uri` columns
hold S3 paths, not pixels.

## 3. Version control: time travel + rollback

```
docker compose exec lab python scripts/version_control_demo.py
```

Narrate while it runs: full snapshot history with DuckLake's own `changes` column as the audit
log; time travel across the `is_crowd` -> `is_crowd_annotation` schema rename; a deliberately
corrupted `UPDATE`; the two-snapshot diff via `ducklake_table_changes`; the rollback; and that the
bad snapshot is still queryable afterward (history is immutable - rollback only adds a corrective
snapshot).

## 4. Incremental ingest: new data, new snapshot

```
docker compose exec lab python scripts/ingest_raw_incremental.py
```

Narrate: pulls COCO images/VisDrone frames not already in `raw`, stages them as local Parquet in
`local-store/` (the "local Docker host storage" half), then `INSERT`s into the existing raw
tables - watch a new snapshot appear with `tables_inserted_into` in its `changes` map, with no
data rewritten.

## 5. Close the loop: push gold back to the Hub

```
docker compose exec lab python scripts/push_gold_to_hub.py
```

Narrate: `gold.coco_labels` and `gold.visdrone_training` each become a Hugging Face dataset repo
(`Acender/cs375-coco-gold`, `Acender/cs375-visdrone-gold`) via
`Dataset.from_parquet(...).push_to_hub(...)` - the lakehouse's curated output is now consumable
the same way the raw input was pulled in.

## 6. Q&A

Point to `REPORT.md` for the six conceptual questions (metadata/data separation, snapshots and
time travel, uniqueness without constraints, tracing an INSERT to bytes, why a SQL catalog, and
why pixels never enter a table).
