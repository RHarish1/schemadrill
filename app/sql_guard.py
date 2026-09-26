import sqlglot


def validate_sql(sql: str) -> None:
    sqlglot.parse_one(sql, read="postgres")
