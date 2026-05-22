import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from awsgluedq.transforms import EvaluateDataQuality

# Parse job arguments and initialize the Glue/Spark context
args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# Fallback ruleset applied to any target node that enables data quality
DEFAULT_DATA_QUALITY_RULESET = """
    Rules = [
        ColumnCount > 0
    ]
"""

# Read raw JSON files recursively from the Bronze layer in S3
AmazonS3_node1775380540426 = glueContext.create_dynamic_frame.from_options(format_options={"multiLine": "false"}, connection_type="s3", format="json", connection_options={"paths": ["s3://unam-2026-ingenieriadedatos-equipo3-660864588540-us-east-2-an/1bronce/"], "recurse": True}, transformation_ctx="AmazonS3_node1775380540426")

# Map source field names and types to the canonical Silver schema
ChangeSchema_node1775380814193 = ApplyMapping.apply(frame=AmazonS3_node1775380540426, mappings=[("appid", "int", "appid", "int"), ("nombre", "string", "nombre", "string"), ("jugadores_actuales_api", "int", "jugadores_actuales_api", "int"), ("desarrolladores", "string", "desarrolladores", "string"), ("editores", "string", "editores", "string"), ("generos", "string", "generos", "string"), ("fecha_lanzamiento", "string", "fecha_lanzamiento", "string"), ("precio", "string", "precio", "string"), ("metacritic_score", "int", "metacritic_score", "int"), ("metacritic_url", "string", "metacritic_url", "string"), ("descripcion_corta", "string", "descripcion_corta", "string"), ("total_resenas", "int", "total_resenas", "int"), ("resenas_positivas", "int", "resenas_positivas", "int"), ("resenas_negativas", "int", "resenas_negativas", "int"), ("puntuacion", "int", "puntuacion", "int"), ("descripcion_puntuacion", "string", "descripcion_puntuacion", "string")], transformation_ctx="ChangeSchema_node1775380814193")

# Data quality rules for the Silver layer: completeness and value range checks
EvaluateDataQuality_node1775381082267_ruleset = """
    Rules = [

      IsComplete "nombre",
      IsComplete "appid",
      IsComplete "precio",
      IsComplete "total_resenas",
      IsComplete "resenas_positivas",
      IsComplete "resenas_negativas",
      IsComplete "puntuacion",

      ColumnValues "metacritic_score" between 0 and 100,
      ColumnValues "puntuacion" between 0 and 100,
      ColumnValues "jugadores_actuales_api" >= 0,
      ColumnValues "resenas_positivas" >= 0,
      ColumnValues "resenas_negativas" >= 0,

      ColumnValues "total_resenas" >= 10
    ]
"""

# Evaluate data quality and route rows; publishes metrics to CloudWatch
EvaluateDataQuality_node1775381082267 = EvaluateDataQuality().process_rows(frame=ChangeSchema_node1775380814193, ruleset=EvaluateDataQuality_node1775381082267_ruleset, publishing_options={"dataQualityEvaluationContext": "EvaluateDataQuality_node1775381082267", "enableDataQualityCloudWatchMetrics": True, "enableDataQualityResultsPublishing": True}, additional_options={"observations.scope":"ALL","performanceTuning.caching":"CACHE_NOTHING"})

# Extract only the rows that passed all quality rules
originalData_node1775381253539 = SelectFromCollection.apply(dfc=EvaluateDataQuality_node1775381082267, key="originalData", transformation_ctx="originalData_node1775381253539")

# Run the default (column count) ruleset as a final check before writing to Silver
EvaluateDataQuality().process_rows(frame=originalData_node1775381253539, ruleset=DEFAULT_DATA_QUALITY_RULESET, publishing_options={"dataQualityEvaluationContext": "EvaluateDataQuality_node1775380445400", "enableDataQualityResultsPublishing": True}, additional_options={"dataQualityResultsPublishing.strategy": "BEST_EFFORT", "observations.scope": "ALL"})

# Coalesce to a single Parquet file only when rows exist, then write Snappy-compressed output to Silver
if (originalData_node1775381253539.count() >= 1):
   originalData_node1775381253539 = originalData_node1775381253539.coalesce(1)
AmazonS3_node1775381409922 = glueContext.write_dynamic_frame.from_options(frame=originalData_node1775381253539, connection_type="s3", format="glueparquet", connection_options={"path": "s3://unam-2026-ingenieriadedatos-equipo3-660864588540-us-east-2-an/2silver/", "partitionKeys": []}, format_options={"compression": "snappy"}, transformation_ctx="AmazonS3_node1775381409922")

job.commit()
