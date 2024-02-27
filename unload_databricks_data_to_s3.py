import argparse
import collections
import math
import time

from pyspark.sql import SparkSession
from pyspark.sql import DataFrame
from pyspark.sql.functions import col

MAX_EVENT_COUNT_PER_OUTPUT_FILE: int = 1_000_000


def parse_table_versions_map_arg(table_versions_map: str) -> dict[str, list[int]]:
    """
    Extract table && version range numbers from input str.
    :param table_versions_map: table versions map. Sample input 'catalog.schema.table=1-2,catalog.schema2.table2=11-12'
    which means table 'catalog.schema.table' with version range [1,2] and table 'catalog.schema2.table2'
    with version range [11,12].
    :return: table to version ranges map. Sample output: {'catalog.schema.table': [1,2]}
    """
    dictionary = collections.defaultdict(list)
    table_and_versions_list = table_versions_map.split(",")
    for table_and_versions in table_and_versions_list:
        table_name = table_and_versions.split("=")[0]
        versions = table_and_versions.split("=")[1].split("-")
        dictionary[table_name].append(int(versions[0]))
        dictionary[table_name].append(int(versions[1]))
    return dictionary


def build_temp_view_name(table_full_name: str) -> str:
    """
    Build temp view name for the table. Wrap table name with '`' to escape '.'. Append `epoch` so view name is very
    unlikely collapse with another table.
    :param table_full_name: table name
    :return: temp view name for the table
    """
    return '`{table}.{epoch}`'.format(table=table_full_name, epoch=int(time.time()))


def build_sql_to_query_table_of_version(table_full_name: str, ending_version: int) -> str:
    return "select * from {table} version as of {version}".format(table=table_full_name, version=ending_version)


def build_sql_to_query_table_between_versions(table_full_name: str, starting_version: int, ending_version: int) -> str:
    return "select * from table_changes(\"{table}\", {starting_version}, {ending_version})".format(
        table=table_full_name, starting_version=starting_version, ending_version=ending_version)


def fetch_data(table_full_name: str, starting_version: int, ending_version: int) -> DataFrame:
    if starting_version == 0:
        return spark.sql(build_sql_to_query_table_of_version(table_full_name, ending_version))
    else:
        # TODO: filter data
        return spark.sql(build_sql_to_query_table_between_versions(table_full_name, starting_version, ending_version))


def filter_data(data_frame: DataFrame, data_type: str) -> DataFrame:
    if "_change_type" in data_frame.columns:
        if data_type == "EVENT":
            # for EVENT, only keep new inserted rows.
            data_frame = data_frame.filter(col("_change_type").isNull() | col("_change_type").eqNullSafe("insert"))
        else:
            # For USER_PROPERTY and GROUP_PROPERTY, keep both insert && updated rows.
            data_frame = data_frame.filter(col("_change_type").isNull() | col("_change_type").eqNullSafe("insert")
                                           | col("_change_type").eqNullSafe("update_postimage"))
        data_frame = data_frame.drop("_commit_version", "_commit_timestamp", "_change_type")
    return data_frame


def get_partition_count(event_count: int, max_event_count_per_output_file: int) -> int:
    return max(1, math.ceil(event_count / max_event_count_per_output_file))


def export_meta_data(event_count: int, partition_count: int):
    meta_data: list = [{'event_count': event_count, 'partition_count': partition_count}]
    spark.createDataFrame(meta_data).write.mode("overwrite").json(args.s3_path + "/meta")



# Example: python3 ./unload_databricks_data_to_s3.py --table_versions_map test_category_do_not_delete_or_modify.canary_tests.employee=16-16 --data_type EVENT --sql "select unix_millis(current_timestamp()) as time, id as user_id, \"databricks_import_canary_test_event\" as event_type, named_struct('name', name, 'home', home, 'age', age, 'income', income) as user_properties, named_struct('group_type1', ARRAY(\"group_A\", \"group_B\")) as groups, named_struct('group_property', \"group_property_value\") as group_properties from test_category_do_not_delete_or_modify.canary_tests.employee" --secret_scope amplitude_databricks_import --secret_key_name_for_aws_access_key source_destination_55_batch_1350266533_aws_access_key --secret_key_name_for_aws_secret_key source_destination_55_batch_1350266533_aws_secret_key --secret_key_name_for_aws_session_token source_destination_55_batch_1350266533_aws_session_token --s3_region us-west-2 --s3_path s3a://com-amplitude-falcon-stag2/databricks_import/unloaded_data/source_destination_55/batch_1350266533/
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='unload data from databricks using SparkPython')
    # replace 'required=True' with 'nargs='?', default=None' to make it optional.
    parser.add_argument("--table_versions_map", required=True,
                        help="""tables and version ranges where data imported from.
        Format syntax is '[{tableVersion},...,{tableVersion*N}]'. '{tableVersion}' will be
        '{catalogName}.{schemaName}.{tableName}={startingVersion}-{endingVersion}'.
        Example: catalog1.schema1.table1=0-12,catalog2.schema2.table2=10-100 """)
    parser.add_argument("--data_type", required=True,
                        choices=['EVENT', 'USER_PROPERTY', 'GROUP_PROPERTY'],
                        help="""type of data to be imported.
                            Valid values are ['EVENT', 'USER_PROPERTY', 'GROUP_PROPERTY'].""")
    parser.add_argument("--sql", required=True, help="transformation sql")
    parser.add_argument("--secret_scope", required=True, help="databricks secret scope name")
    parser.add_argument("--secret_key_name_for_aws_access_key", required=True,
                        help="databricks secret key name of aws_access_key")
    parser.add_argument("--secret_key_name_for_aws_secret_key", required=True,
                        help="databricks secret key name of aws_secret_key")
    parser.add_argument("--secret_key_name_for_aws_session_token", required=True,
                        help="databricks secret key name of aws_session_token")
    parser.add_argument("--s3_region", required=True, help="s3 region")
    parser.add_argument("--s3_path", required=True, help="s3 path where data will be written into")

    args, unknown = parser.parse_known_args()

    spark = SparkSession.builder.getOrCreate()
    # setup s3 credentials for data export
    aws_access_key = dbutils.secrets.get(scope=args.secret_scope, key=args.secret_key_name_for_aws_access_key)
    aws_secret_key = dbutils.secrets.get(scope=args.secret_scope, key=args.secret_key_name_for_aws_secret_key)
    aws_session_token = dbutils.secrets.get(scope=args.secret_scope, key=args.secret_key_name_for_aws_session_token)
    spark.conf.set("fs.s3a.aws.credentials.provider", "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider")
    spark.conf.set("fs.s3a.access.key", aws_access_key)
    spark.conf.set("fs.s3a.secret.key", aws_secret_key)
    spark.conf.set("fs.s3a.session.token", aws_session_token)
    spark.conf.set("fs.s3a.endpoint.region", args.s3_region)

    sql: str = args.sql

    # Build temp views
    table_to_import_version_range_map: dict[str, list[int]] = parse_table_versions_map_arg(args.table_versions_map)
    for table, import_version_range in table_to_import_version_range_map.items():
        data: DataFrame = fetch_data(table, import_version_range[0], import_version_range[1])
        data = filter_data(data, args.data_type)
        view_name: str = build_temp_view_name(table)
        data.createOrReplaceTempView(view_name)
        # replace table name in sql to get prepared for sql transformation
        sql = sql.replace(table, view_name)

    # run SQL to transform data
    export_data: DataFrame = spark.sql(sql)

    if not export_data.isEmpty():
        event_count = export_data.count()

        partition_count = get_partition_count(event_count, MAX_EVENT_COUNT_PER_OUTPUT_FILE)

        # export meta data
        export_meta_data(event_count, partition_count)

        export_data = export_data.repartition(partition_count)
        # export data
        export_data.write.mode("overwrite").json(args.s3_path)
        print("Unloaded {event_count} events.".format(event_count=event_count))
    else:
        print("No events were exported.")
    # stop spark session
    spark.stop()
