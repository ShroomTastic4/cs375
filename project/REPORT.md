# Lakehouse Project Report

## 1. Metadata/data separation: what it buys you, and concurrent-read consistency

A single self-contained file (a `.db` file, a giant Parquet, etc.) couples "where the data
lives" with "what the data is": every reader and writer has to go through that one file, so it
becomes both the scaling bottleneck and the single point of failure. DuckLake splits the two:
the catalog (schemas, snapshots, which Parquet files make up each table) lives in a small SQL
database (`metadata.ducklake`), while the actual bytes live as immutable Parquet objects in
RustFS. That buys:

- **Independent scaling** - object storage scales for cheap, unbounded bytes; the catalog stays
  small and fast no matter how much data sits in RustFS.
- **No write-in-place** - every change produces a *new* Parquet file. Nothing already written is
  ever mutated, so a reader can never observe a half-written file.
- **Cheap metadata operations** - listing snapshots, tables, or schema history is a SQL query
  against a small relational catalog, not a scan of potentially millions of objects in RustFS.

**Consistency under concurrent reads:** because data files are immutable, a reader pins to a
snapshot id the moment it starts a query and keeps reading exactly those files for the whole
query, even if a writer commits a brand-new snapshot in the meantime (snapshot isolation /
MVCC). Readers never block and never see a torn state. Writers coordinate through the catalog's
own transactional commit (we found a `ducklake_commit` function in the extension) using
optimistic concurrency - the losing writer in a race just retries against the new current
snapshot.

## 2. Snapshots, delete-plus-insert, time travel, rollback, and the cost of history

We saw this directly: running `UPDATE silver.coco_annotations SET category_id = -999` and then
diffing with `ducklake_table_changes('lake','silver','coco_annotations', good, bad)` showed 117
`update_preimage` rows and 117 `update_postimage` rows. An UPDATE isn't an in-place edit - it's
"mark the old rows invisible as of this new snapshot, and write a new Parquet file holding the
new versions," recorded as one atomic catalog entry (a new `snapshot_id` plus a `changes` map).

- **Time travel** (`AT (VERSION => n)` / `AT (TIMESTAMP => ...)`) works by asking the catalog
  "which Parquet files (and delete-markers) made up this table as of snapshot `n`" and reading
  exactly those files. Nothing is rewritten or deleted on disk when a row is "deleted," so any
  past snapshot stays reconstructible as long as its files still exist.
- **Rollback** isn't a separate primitive - there's no "move the current pointer back" function in
  the extension. We did it the way every immutable-snapshot lakehouse does it: read the good
  version forward, `CREATE OR REPLACE TABLE x AS SELECT * FROM x AT (VERSION => good)`. That
  produces a brand-new corrective snapshot. The bad snapshot is never erased - we confirmed
  `AT (VERSION => bad)` still returns the corrupted data *after* the "rollback."
- **Cost over time:** every snapshot adds catalog rows and (usually) new Parquet files, so storage
  grows monotonically with every write, even tiny ones. That's exactly why the extension ships
  `ducklake_expire_snapshots`, `ducklake_cleanup_old_files`, `ducklake_delete_orphaned_files`, and
  `ducklake_merge_adjacent_files` - unlimited time travel is a sliding window you actively manage,
  not a free lunch.

## 3. No primary keys/constraints - how we guaranteed uniqueness and quality anyway

All of it lives in the transform SQL, not the engine:

- **Uniqueness**: `sql/20_silver.sql` dedupes with
  `ROW_NUMBER() OVER (PARTITION BY <natural key> ORDER BY ...)` filtered to `rn = 1` - on
  `annotation_id` for COCO, `(video_id, fragment_index)` for VisDrone. Uniqueness is asserted by
  how the query is built, not policed afterward.
- **Quality**: `COALESCE` fills missing values with explicit sentinels (`category_id = -1` for
  "no detected object," `area = 0`, `is_crowd_annotation = false`) and `CAST` fixes types,
  instead of relying on `NOT NULL`/`CHECK` constraints.
- **Gold-layer uniqueness**: `GROUP BY image_id` in `gold.coco_labels` collapses to one row per
  image by construction - a GROUP BY can't emit duplicate keys.

The honest trade-off: none of this is enforced on *future* writes the way a real constraint
would be. The guarantee only holds as of the last time the pipeline script ran - if something
bypassed `20_silver.sql`/`30_gold.sql` and inserted directly, nothing would stop a duplicate.
That's the standard medallion-architecture assumption: silver/gold are owned and recomputed by
trusted pipeline code, not arbitrary ad hoc writes.

## 4. Tracing one INSERT to bytes

Using our real `INSERT INTO raw.coco_annotations SELECT * FROM read_parquet(...)`:

1. DuckDB reads the staged Parquet file from the local staging area (`local-store/`, bind-mounted
   into the container at `/data/local`) - the "local Docker host storage" half of the flow.
2. DuckDB writes the new rows into a fresh Parquet file and uploads it as a new object directly to
   RustFS, under a DuckLake-managed key - we observed the exact shape:
   `raw/coco_annotations/ducklake-<uuid>.parquet`. **That object, in the `lakehouse` bucket in
   RustFS, is the only place the actual bytes live.**
3. DuckLake then writes a handful of small rows into the SQL catalog (`metadata.ducklake`, a
   DuckDB file living on the host, bind-mounted at `/workspace`): a new snapshot row
   (`snapshot_id`, timestamp, a `changes` map like `{tables_inserted_into=[...]}`) and a row
   recording that this new Parquet file belongs to `raw.coco_annotations` starting at that
   snapshot.
4. "The current state of `raw.coco_annotations`" isn't stored anywhere as a single object - it's
   *computed* on every query by joining the catalog's "which files are visible as of the latest
   (or requested) snapshot" against the immutable Parquet files in RustFS.

So: catalog/snapshot state -> tiny SQL rows on the host disk. Data bytes -> one S3 object in
RustFS. There is no single artifact that "is" the table.

## 5. Why a SQL catalog instead of files-alongside-data

File-only formats (Iceberg/Delta in their purest form) store the catalog as a chain of
JSON/Avro metadata files in the *same* object storage as the data, needing an external
atomic-swap mechanism (or a lightweight catalog service) just to flip the "current" pointer,
since most object stores have no native compare-and-swap on a plain object.

Putting the catalog in a real SQL database instead makes **easy**:

- **Real ACID commits** - concurrent writers resolve "who gets the next snapshot" via the
  database's native transactions/locking, not a bespoke rename-and-hope trick against object
  storage.
- **Fast, rich metadata queries** - `ducklake_snapshots`, `ducklake_table_changes`,
  `ducklake_table_info` are ordinary SQL queries against a real schema, not a walk through a
  chain of JSON files in object storage (the thing that makes pure file-catalog formats slow at
  deep history).
- **Multiple engines attaching concurrently** to the exact same metadata with no extra
  infrastructure beyond "can you reach this database."

What it makes **harder**:

- **Portability** - any reader/writer now needs network access and credentials for *that specific
  database*, on top of the object store. A pure file-catalog format can be read by anything that
  understands the file layout and has the object-store credentials, full stop.
- **Operational footprint** - the SQL database is one more service that every single
  reader/writer depends on just to find out which files to open. If it's down, the lakehouse is
  down, even though the actual bytes in RustFS are perfectly fine.

## 6. Why bytes never enter a DuckLake table, and the VisDrone fragment-index query

DuckLake tables are row/columnar Parquet - excellent for millions of small structured rows,
bad for a handful of multi-hundred-KB binary blobs. There's no columnar compression win and no
row-group statistics/pruning benefit for a BLOB column, and every "row" read would mean
materializing a huge binary value through the query engine instead of a plain object GET. RustFS
is built for exactly that GET.

So the actual image/video bytes are uploaded as plain S3 objects under their own prefixes
(`raw/coco/images/<file>.jpg`, `raw/visdrone/<video_id>/<frame>.jpg`) completely outside any
DuckLake table. The tables hold only a `uri` column pointing at that object plus structured
metadata about it - dimensions and bounding boxes for COCO; for VisDrone, a separate
`raw.visdrone_detections` table holds one row per detected object (`label`, `bbox`, `confidence`,
`occlusion`) sourced from the dataset's own per-frame annotations, which we aggregate into
`n_objects` and `classes` on the fragment index itself (`byte_size`, `start_frame`/`end_frame`,
`start_time`/`end_time`, `n_objects`, `classes`). The catalog is the index; RustFS is the blob
store.

The VisDrone fragment index makes "give me the busy fragments" cheap: each row is one frame with
its own real detection-derived stats. A predicate like `WHERE n_objects > threshold` only has to
scan the tiny fragment-index Parquet file (kilobytes) - it never touches a single frame image.
Only after that query returns a short list of `uri`s do we issue targeted `s3.get_object` calls
for exactly those keys. We measured it directly in `build_silver_gold.py`: filtering for fragments
above the 75th-percentile object count (threshold 52, the busiest frame had 110 detected objects -
pedestrians, cars, vans, motorbikes, bicycles) and fetching only those 4 busy frames pulled 1.9MB,
while the same 4 videos hold 28 frames totaling 12.1MB - **15.8%** of the clip bytes were ever
read, because "which fragments matter" was answered entirely against cheap metadata (real per-frame
detection counts, not a file-size proxy) before any expensive byte was touched.
