-- Lands the manifests produced by scripts/ingest_raw.py into the raw layer.
-- Run after 00_attach.sql, with the lake already ATTACHed and USEd.

CREATE OR REPLACE TABLE raw.coco_annotations AS
SELECT * FROM read_parquet('/data/local/coco_annotations.parquet');

CREATE OR REPLACE TABLE raw.visdrone_fragments AS
SELECT * FROM read_parquet('/data/local/visdrone_fragments.parquet');

CREATE OR REPLACE TABLE raw.coco_categories AS
SELECT * FROM read_parquet('/data/local/coco_categories.parquet');
