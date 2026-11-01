from psycopg import Connection


def table_ddl(connection: Connection, schema_name: str, table_name: str) -> str:
    columns = connection.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema_name, table_name),
    ).fetchall()
    foreign_keys = connection.execute(
        """
        SELECT kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %s AND tc.table_name = %s
        """,
        (schema_name, table_name),
    ).fetchall()
    lines = [f"CREATE TABLE {schema_name}.{table_name} ("]
    lines.extend(
        f"  {name} {data_type}{' NOT NULL' if nullable == 'NO' else ''},"
        for name, data_type, nullable in columns
    )
    lines.extend(
        f"  FOREIGN KEY ({column}) REFERENCES {target_table}({target_column}),"
        for column, target_table, target_column in foreign_keys
    )
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append(");")
    return "\n".join(lines)
