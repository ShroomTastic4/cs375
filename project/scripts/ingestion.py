import datasets, duckdb

# 1. pull a Parquet-based HF dataset through local Docker storage
ds = datasets.load_dataset("pantelism/cats-vs-dogs", split="train")
ds.to_parquet("/data/local/raw_tmp.parquet")

# 2. attach the lakehouse and land the data in the raw layer
con = duckdb.connect()
con.execute(open("sql/00_attach.sql").read())
con.execute("""
    CREATE TABLE raw.my_dataset AS
    SELECT * FROM read_parquet('/data/local/raw_tmp.parquet');
""")

# 3. confirm a snapshot was recorded and the bytes live in RustFS
print(con.sql("FROM ducklake_snapshots('lake')"))