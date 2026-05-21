import boto3
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from io import BytesIO
import os

# Configuración de variables de entorno
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "2silver/")
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

def read_silver(bucket, prefix):
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    frames = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet") and obj["Size"] > 0:
                buf = BytesIO(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
                frames.append(pd.read_parquet(buf))
    return pd.concat(frames, ignore_index=True)

def main():
    # 1. Extracción y Limpieza
    df = read_silver(S3_BUCKET, S3_PREFIX)
    df = df.drop_duplicates(subset=["appid"], keep="last")
    
    # 2. Cálculo del Hidden Gem Score (Lógica de Negocio)
    max_rev = df["total_resenas"].max()
    df["quality_score"] = df["resenas_positivas"] / df["total_resenas"].replace(0, 1)
    df["obscurity_score"] = 1 - (df["total_resenas"] / max(max_rev, 1))
    df["price_score"] = df["precio"].apply(lambda p: 1.0 if p == 0 else max(0.0, 1 - (p / 60.0)))
    df["hidden_gem_score"] = (0.50 * df["quality_score"] + 0.30 * df["obscurity_score"] + 0.20 * df["price_score"]).round(4)
    df["tier"] = df["hidden_gem_score"].apply(lambda s: "S" if s >= 0.85 else ("A" if s >= 0.70 else "B"))

    # 3. Carga en Data Mart (RDS PostgreSQL)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS hidden_gems (appid BIGINT PRIMARY KEY, nombre TEXT, precio NUMERIC(8,2), hidden_gem_score NUMERIC(5,4), tier CHAR(1));")
            sql = "INSERT INTO hidden_gems (appid, nombre, precio, hidden_gem_score, tier) VALUES %s ON CONFLICT (appid) DO UPDATE SET nombre=EXCLUDED.nombre;"
            execute_values(cur, sql, [tuple(r) for r in df[["appid", "nombre", "precio", "hidden_gem_score", "tier"]].itertuples(index=False)])
        conn.commit()
        
        # 4. Persistencia en Data Lake (S3 3gold/)
        parquet_buffer = BytesIO()
        df.to_parquet(parquet_buffer, index=False)
        s3 = boto3.client("s3")
        s3.put_object(Bucket=S3_BUCKET, Key="3gold/hidden_gems_final.parquet", Body=parquet_buffer.getvalue())
        
        print("✅ Gold Layer completada exitosamente.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()