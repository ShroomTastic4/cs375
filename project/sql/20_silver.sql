-- Raw -> silver: fix types, handle missing values, deduplicate.
-- Run after 10_raw.sql, with the lake already ATTACHed and USEd.

CREATE OR REPLACE TABLE silver.coco_annotations AS
SELECT * EXCLUDE (rn) FROM (
    SELECT
        image_id,
        file_name,
        uri,
        CAST(width AS INTEGER) AS width,
        CAST(height AS INTEGER) AS height,
        annotation_id,
        COALESCE(category_id, -1) AS category_id,         -- -1 sentinel: image has no detected object
        bbox,
        COALESCE(area, 0.0) AS area,
        (COALESCE(iscrowd, 0) = 1) AS is_crowd,
        ROW_NUMBER() OVER (
            PARTITION BY COALESCE(CAST(annotation_id AS VARCHAR), 'img-' || image_id)
            ORDER BY image_id
        ) AS rn
    FROM raw.coco_annotations
)
WHERE rn = 1;

-- Schema evolution: rename for clarity after the fact. DuckLake records this as its
-- own snapshot (a metadata-only change, no data rewrite) - the old snapshot still
-- shows the column as `is_crowd` under AT (VERSION => ...).
ALTER TABLE silver.coco_annotations RENAME COLUMN is_crowd TO is_crowd_annotation;

CREATE OR REPLACE TABLE silver.visdrone_fragments AS
SELECT * EXCLUDE (rn) FROM (
    SELECT
        video_id,
        CAST(fragment_index AS INTEGER) AS fragment_index,
        CAST(fragment_count AS INTEGER) AS fragment_count,
        filename,
        uri,
        COALESCE(byte_size, 0) AS byte_size,
        scene_id,
        CAST(start_frame AS INTEGER) AS start_frame,
        CAST(end_frame AS INTEGER) AS end_frame,
        start_time,
        end_time,
        COALESCE(n_objects, 0) AS n_objects,         -- real per-frame detection count
        COALESCE(classes, []) AS classes,            -- distinct detected labels in this frame
        (fragment_index = 1) AS is_keyframe,
        ROW_NUMBER() OVER (PARTITION BY video_id, fragment_index ORDER BY filename) AS rn
    FROM raw.visdrone_fragments
)
WHERE rn = 1;

CREATE OR REPLACE TABLE silver.visdrone_detections AS
SELECT * EXCLUDE (rn) FROM (
    SELECT
        video_id,
        CAST(fragment_index AS INTEGER) AS fragment_index,
        uri,
        label,
        bbox,
        COALESCE(confidence, 1.0) AS confidence,
        COALESCE(visibility, 1) AS visibility,
        COALESCE(occlusion, 0) AS occlusion,
        ROW_NUMBER() OVER (
            PARTITION BY video_id, fragment_index, label, bbox
            ORDER BY video_id
        ) AS rn
    FROM raw.visdrone_detections
)
WHERE rn = 1;
