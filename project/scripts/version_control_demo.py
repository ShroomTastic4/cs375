#!/usr/bin/env python3
"""
Version-control demo on the silver.coco_annotations key table: full
snapshot history (with notes), time travel by version and by timestamp,
a two-snapshot diff, and a rollback of a deliberately bad transform.

Run inside the lab container, after build_silver_gold.py:
    docker compose exec lab python scripts/version_control_demo.py
"""

from pathlib import Path

import duckdb

SQL_DIR = Path("sql")


def print_snapshot_history(con):
    print("=== Snapshot history so far (DuckLake's own change log doubles as our notes) ===")
    print(con.sql("""
        SELECT snapshot_id, snapshot_time, schema_version, changes
        FROM ducklake_snapshots('lake')
        ORDER BY snapshot_id
    """))


def demo_time_travel(con):
    print("\n=== Time travel by VERSION: schema before/after the is_crowd rename ===")
    rename_snapshot = con.sql("""
        SELECT min(snapshot_id) FROM ducklake_snapshots('lake')
        WHERE list_contains(map_keys(changes), 'tables_altered')
    """).fetchone()[0]
    before_id = rename_snapshot - 1

    con.execute("USE lake.silver")
    print(f"\nschema AT (VERSION => {before_id}) (before rename):")
    print(con.sql(f"DESCRIBE SELECT * FROM coco_annotations AT (VERSION => {before_id})"))
    print(f"\nschema AT (VERSION => {rename_snapshot}) (after rename):")
    print(con.sql(f"DESCRIBE SELECT * FROM coco_annotations AT (VERSION => {rename_snapshot})"))

    print("\n=== Time travel by TIMESTAMP ===")
    ts = con.sql(f"""
        SELECT snapshot_time FROM ducklake_snapshots('lake') WHERE snapshot_id = {before_id}
    """).fetchone()[0]
    print(f"row count AT (TIMESTAMP => '{ts}'):")
    print(con.sql(f"SELECT count(*) FROM coco_annotations AT (TIMESTAMP => TIMESTAMP '{ts}')"))


def demo_bad_transform_and_rollback(con):
    print("\n=== Exercising version control on silver.coco_annotations ===")
    con.execute("USE lake.silver")

    good_version = con.sql("SELECT max(snapshot_id) FROM ducklake_snapshots('lake')").fetchone()[0]
    good_distinct = con.sql("SELECT count(DISTINCT category_id) FROM coco_annotations").fetchone()[0]
    print(f"Good snapshot {good_version}: {good_distinct} distinct category_id values")

    print("\nRunning a deliberately bad transform (wipes every category_id)...")
    con.execute("UPDATE coco_annotations SET category_id = -999")
    bad_version = con.sql("SELECT max(snapshot_id) FROM ducklake_snapshots('lake')").fetchone()[0]
    bad_distinct = con.sql("SELECT count(DISTINCT category_id) FROM coco_annotations").fetchone()[0]
    print(f"Bad snapshot {bad_version}: {bad_distinct} distinct category_id value(s) (corrupted)")

    print(f"\n=== Comparing snapshot {good_version} vs {bad_version} via ducklake_table_changes ===")
    print(con.sql(f"""
        SELECT change_type, count(*) AS row_count
        FROM ducklake_table_changes('lake', 'silver', 'coco_annotations', {good_version}, {bad_version})
        GROUP BY change_type
    """))

    print(f"\nRolling back: restoring data from snapshot {good_version}...")
    con.execute(f"""
        CREATE OR REPLACE TABLE coco_annotations AS
        SELECT * FROM coco_annotations AT (VERSION => {good_version})
    """)
    restored_version = con.sql("SELECT max(snapshot_id) FROM ducklake_snapshots('lake')").fetchone()[0]
    restored_distinct = con.sql("SELECT count(DISTINCT category_id) FROM coco_annotations").fetchone()[0]
    print(f"Restored snapshot {restored_version}: {restored_distinct} distinct category_id values")

    print(f"\nBad snapshot {bad_version} is still time-travel queryable - history is immutable, "
          f"rollback only adds a new corrective snapshot:")
    print(con.sql(f"""
        SELECT count(DISTINCT category_id) AS still_corrupted_in_history
        FROM coco_annotations AT (VERSION => {bad_version})
    """))


def main():
    con = duckdb.connect()
    con.execute((SQL_DIR / "00_attach.sql").read_text())

    print_snapshot_history(con)
    demo_time_travel(con)
    demo_bad_transform_and_rollback(con)


if __name__ == "__main__":
    main()
