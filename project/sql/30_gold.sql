-- Silver -> gold: ML-ready tables. Run after 20_silver.sql, with the lake ATTACHed and USEd.

-- One row per image: URI + labels + a deterministic train/val split.
CREATE OR REPLACE TABLE gold.coco_labels AS
SELECT
    a.image_id,
    a.uri,
    a.file_name,
    a.width,
    a.height,
    list_sort(array_distinct(list(c.name) FILTER (WHERE c.name IS NOT NULL))) AS labels,
    arg_max(c.name, a.area) FILTER (WHERE c.name IS NOT NULL) AS primary_label,
    count(*) FILTER (WHERE a.annotation_id IS NOT NULL) AS object_count,
    bool_or(a.is_crowd_annotation) AS has_crowd_annotation,
    CASE WHEN a.image_id % 5 = 0 THEN 'val' ELSE 'train' END AS split
FROM silver.coco_annotations a
LEFT JOIN raw.coco_categories c ON c.category_id = a.category_id
GROUP BY a.image_id, a.uri, a.file_name, a.width, a.height;

-- One row per video fragment (frame): per-fragment stats for selective reads,
-- plus a per-video (not per-frame) train/val split so a clip never straddles both.
CREATE OR REPLACE TABLE gold.visdrone_training AS
SELECT
    video_id,
    fragment_index,
    fragment_count,
    uri,
    byte_size,
    is_keyframe,
    fragment_index / fragment_count::DOUBLE AS relative_position,
    CASE WHEN hash(video_id) % 5 = 0 THEN 'val' ELSE 'train' END AS split
FROM silver.visdrone_fragments;
