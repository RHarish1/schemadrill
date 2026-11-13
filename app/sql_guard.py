from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.models import SQLGuardConfig, SQLGuardResult

AUTHORIZATION_DENIED_MESSAGE = "You do not have access to the requested data."
SECURITY_REJECTED_MESSAGE = "The generated query was rejected by the security policy."
DEMO_CHANGE_REJECTED_MESSAGE = (
    "Change scripts are not supported on demo datasets. "
    "Connect your own Neon database to generate change scripts."
)

# These functions expose server files, large objects, or outbound database
# capabilities. Extensions can add more names through SQLGuardConfig.
BLOCKED_FUNCTIONS = {
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "pg_ls_logdir",
    "pg_ls_waldir",
    "pg_ls_archive_statusdir",
    "pg_ls_tmpdir",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_connect",
    "dblink_connect_u",
    "dblink_exec",
    "dblink_send_query",
    "dblink_get_result",
    "set_config",
    "pg_sleep",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
}

_DEMO_DENIED_SCHEMAS = {"vault"}
_DEMO_DENIED_TABLES = {
    "users",
    "public.users",
    "user_datasets",
    "public.user_datasets",
    "schema_embeddings",
    "public.schema_embeddings",
    "tenant_connections",
    "public.tenant_connections",
    "tenant_schema_embeddings",
    "public.tenant_schema_embeddings",
}

_USER_DB_DENIED_CATALOGS = {
    "pg_authid",
    "pg_catalog.pg_authid",
    "pg_shadow",
    "pg_catalog.pg_shadow",
    "pg_user_mapping",
    "pg_catalog.pg_user_mapping",
    "pg_largeobject",
    "pg_catalog.pg_largeobject",
    "pg_largeobject_metadata",
    "pg_catalog.pg_largeobject_metadata",
    "pg_statistic",
    "pg_catalog.pg_statistic",
    "pg_statistic_ext_data",
    "pg_catalog.pg_statistic_ext_data",
    "pg_subscription",
    "pg_catalog.pg_subscription",
    "pg_replication_origin",
    "pg_catalog.pg_replication_origin",
    "pg_seclabel",
    "pg_catalog.pg_seclabel",
    "pg_shseclabel",
    "pg_catalog.pg_shseclabel",
    "pg_auth_members",
    "pg_catalog.pg_auth_members",
    "pg_roles",
    "pg_catalog.pg_roles",
    "pg_user",
    "pg_catalog.pg_user",
}

_USER_DB_ALLOWED_CATALOGS = {
    "information_schema.tables",
    "information_schema.columns",
    "information_schema.table_constraints",
    "information_schema.key_column_usage",
    "information_schema.constraint_column_usage",
    "information_schema.views",
    "information_schema.schemata",
    "pg_catalog.pg_class",
    "pg_catalog.pg_namespace",
    "pg_catalog.pg_attribute",
    "pg_catalog.pg_type",
    "pg_catalog.pg_tables",
    "pg_catalog.pg_views",
    "pg_catalog.pg_index",
    "pg_catalog.pg_constraint",
}

_READ_STATEMENT_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_CHANGE_STATEMENT_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
)
_ALWAYS_DISALLOWED_NODES = (
    exp.Copy,
    exp.Grant,
    exp.Command,
    exp.Into,
    exp.Lock,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)

# Reject mutation, DDL, locking, and session/command AST nodes anywhere in the tree
# (for example, inside data-modifying CTEs like WITH d AS (DELETE ... RETURNING *) SELECT ...).
_DISALLOWED_MUTATION_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Grant,
    exp.Command,
    exp.Into,
    exp.Lock,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
)


def default_guard_config(
    max_limit: int = 1000,
    *,
    mode: str = "demo",
    allow_change_scripts: bool = False,
) -> SQLGuardConfig:
    return SQLGuardConfig(
        max_limit=max_limit,
        blocked_functions=set(BLOCKED_FUNCTIONS),
        mode=mode,  # type: ignore[arg-type]
        allow_change_scripts=allow_change_scripts,
    )


def _canonical_table(table: exp.Table) -> str:
    parts = [part for part in (table.catalog, table.db, table.name) if part]
    return ".".join(parts).lower()


def _physical_tables(statement: exp.Expression) -> list[str]:
    cte_names = {
        cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }
    tables = []
    for table in statement.find_all(exp.Table):
        canonical = _canonical_table(table)
        # A CTE reference is not a physical table; inspect its definition instead.
        if not table.db and not table.catalog and table.name.lower() in cte_names:
            continue
        if canonical and canonical not in tables:
            tables.append(canonical)
    return sorted(tables)


def _blocked_functions(statement: exp.Expression, blocked: Iterable[str]) -> list[str]:
    blocked_names = {name.lower() for name in blocked}
    names = {
        function.name.lower()
        for function in statement.find_all(exp.Func)
        if function.name and function.name.lower() in blocked_names
    }
    return sorted(names)


def _statement_type(statement: exp.Expression) -> str:
    if isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return "SELECT"
    return type(statement).__name__.upper()


def _disallowed_mutation_operation(statement: exp.Expression) -> str | None:
    for node in statement.walk():
        if isinstance(node, _DISALLOWED_MUTATION_NODES):
            return type(node).__name__.upper()
    return None


def _limit_value(statement: exp.Expression) -> int | None:
    limit = statement.args.get("limit")
    if limit is None:
        return None
    expression = limit.args.get("expression") or limit.args.get("count")
    if not isinstance(expression, exp.Literal) or not expression.is_int:
        return -1
    return int(expression.this)


def _check_table_policy(
    tables: list[str],
    normalized_allowed: set[str],
    mode: str,
    created_tables: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (rejection_reason, user_facing_message) or (None, None) if allowed."""
    created = created_tables or set()
    for tbl in tables:
        parts = tbl.split(".")
        schema_part = parts[-2] if len(parts) >= 2 else ""
        if mode == "demo":
            if schema_part in _DEMO_DENIED_SCHEMAS or tbl in _DEMO_DENIED_TABLES:
                return f"Unauthorized table: {tbl}", AUTHORIZATION_DENIED_MESSAGE
            if tbl not in normalized_allowed:
                return f"Unauthorized table: {tbl}", AUTHORIZATION_DENIED_MESSAGE
        else:
            # user_db mode
            if tbl in _USER_DB_DENIED_CATALOGS:
                return f"Unauthorized catalog table: {tbl}", AUTHORIZATION_DENIED_MESSAGE
            if schema_part in ("information_schema", "pg_catalog"):
                if tbl not in _USER_DB_ALLOWED_CATALOGS:
                    return f"Unauthorized catalog table: {tbl}", AUTHORIZATION_DENIED_MESSAGE
                continue
            if tbl in created:
                if schema_part in ("information_schema", "pg_catalog", "vault"):
                    return f"Unauthorized schema for CREATE: {tbl}", AUTHORIZATION_DENIED_MESSAGE
                continue
            if tbl not in normalized_allowed:
                return f"Unauthorized table: {tbl}", AUTHORIZATION_DENIED_MESSAGE
    return None, None


def _collect_change_warnings(statements: list[exp.Expression]) -> list[str]:
    warnings: list[str] = []
    for stmt in statements:
        if isinstance(stmt, exp.Update) and stmt.args.get("where") is None:
            msg = "UPDATE statement has no WHERE clause and will modify all rows."
            if msg not in warnings:
                warnings.append(msg)
        elif isinstance(stmt, exp.Delete) and stmt.args.get("where") is None:
            msg = "DELETE statement has no WHERE clause and will delete all rows."
            if msg not in warnings:
                warnings.append(msg)
        elif isinstance(stmt, exp.Drop):
            msg = "DROP statement permanently deletes database objects."
            if msg not in warnings:
                warnings.append(msg)
        elif isinstance(stmt, exp.TruncateTable):
            msg = "TRUNCATE statement removes all rows from the target table."
            if msg not in warnings:
                warnings.append(msg)
        elif isinstance(stmt, exp.Alter):
            if any(
                isinstance(d, exp.Drop)
                and (
                    str(d.args.get("kind") or "").upper() == "COLUMN"
                    or isinstance(d.this, exp.Column)
                )
                for d in stmt.find_all(exp.Drop)
            ):
                msg = "ALTER statement drops one or more columns, permanently deleting data."
                if msg not in warnings:
                    warnings.append(msg)
    return warnings


def guard_sql(
    sql: str,
    allowed_tables: set[str],
    config: SQLGuardConfig | None = None,
) -> SQLGuardResult:
    config = config or default_guard_config()
    normalized_allowed = {table.lower() for table in allowed_tables}
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as error:
        return SQLGuardResult(
            allowed=False,
            statement_type="UNKNOWN",
            rejection_reason=f"SQL parse error: {error}",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )

    if not statements:
        return SQLGuardResult(
            allowed=False,
            statement_type="UNKNOWN",
            rejection_reason="Empty SQL statement",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )

    is_read_list = [isinstance(s, _READ_STATEMENT_TYPES) for s in statements]
    is_change_list = [isinstance(s, _CHANGE_STATEMENT_TYPES) for s in statements]

    # Check if this is a change script (only allowed when allow_change_scripts=True)
    if any(is_change_list):
        if not config.allow_change_scripts:
            stmt_type = _statement_type(statements[0])
            tables = _physical_tables(statements[0])
            return SQLGuardResult(
                allowed=False,
                statement_type=stmt_type,
                intent="change",
                referenced_tables=tables,
                rejection_reason="Change scripts are not allowed in this context",
                user_facing_message=SECURITY_REJECTED_MESSAGE,
            )
        if any(is_read_list) or not all(is_change_list):
            return SQLGuardResult(
                allowed=False,
                statement_type="MIXED",
                intent="change",
                rejection_reason="Cannot mix read and change statements in one response",
                user_facing_message=SECURITY_REJECTED_MESSAGE,
            )
        if len(statements) > config.max_change_statements:
            return SQLGuardResult(
                allowed=False,
                statement_type="MULTIPLE",
                intent="change",
                rejection_reason=(
                    f"Change scripts are limited to {config.max_change_statements} statements"
                ),
                user_facing_message=SECURITY_REJECTED_MESSAGE,
            )

        all_tables: list[str] = []
        created_tables: set[str] = set()
        all_blocked: list[str] = []
        for stmt in statements:
            for node in stmt.walk():
                if isinstance(node, _ALWAYS_DISALLOWED_NODES):
                    op_name = type(node).__name__.upper()
                    return SQLGuardResult(
                        allowed=False,
                        statement_type=op_name,
                        intent="change",
                        rejection_reason=f"Disallowed SQL operation: {op_name}",
                        user_facing_message=SECURITY_REJECTED_MESSAGE,
                    )
            if isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Schema):
                if isinstance(stmt.this.this, exp.Table):
                    created_tables.add(_canonical_table(stmt.this.this))
            elif isinstance(stmt, exp.Create) and isinstance(stmt.this, exp.Table):
                created_tables.add(_canonical_table(stmt.this))

            for t in _physical_tables(stmt):
                if t not in all_tables:
                    all_tables.append(t)
            for fn in _blocked_functions(stmt, config.blocked_functions or BLOCKED_FUNCTIONS):
                if fn not in all_blocked:
                    all_blocked.append(fn)

        if all_blocked:
            return SQLGuardResult(
                allowed=False,
                statement_type="CHANGE",
                intent="change",
                referenced_tables=sorted(all_tables),
                blocked_functions=sorted(all_blocked),
                rejection_reason="Blocked PostgreSQL function",
                user_facing_message=SECURITY_REJECTED_MESSAGE,
            )

        reason, user_msg = _check_table_policy(
            all_tables, normalized_allowed, config.mode, created_tables=created_tables
        )
        if reason is not None:
            return SQLGuardResult(
                allowed=False,
                statement_type="CHANGE",
                intent="change",
                referenced_tables=sorted(all_tables),
                rejection_reason=reason,
                user_facing_message=user_msg,
            )

        warnings = _collect_change_warnings(statements)
        normalized_sql = ";\n".join(stmt.sql(dialect="postgres") for stmt in statements) + ";"
        return SQLGuardResult(
            allowed=True,
            sql=normalized_sql,
            statement_type="CHANGE" if len(statements) > 1 else _statement_type(statements[0]),
            intent="change",
            referenced_tables=sorted(all_tables),
            warnings=warnings,
            limit_injected=False,
        )

    # Read path: must be exactly 1 statement
    if len(statements) != 1:
        return SQLGuardResult(
            allowed=False,
            statement_type="MULTIPLE",
            rejection_reason="Multiple SQL statements are not allowed",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )

    statement = statements[0]
    statement_type = _statement_type(statement)
    tables = _physical_tables(statement)
    blocked = _blocked_functions(statement, config.blocked_functions or BLOCKED_FUNCTIONS)
    # Apply structural policy before authorization and result-shaping policy.
    if statement_type != "SELECT":
        return SQLGuardResult(
            allowed=False,
            statement_type=statement_type,
            referenced_tables=tables,
            blocked_functions=blocked,
            rejection_reason="Only SELECT statements are allowed",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )
    disallowed_op = _disallowed_mutation_operation(statement)
    if disallowed_op is not None:
        return SQLGuardResult(
            allowed=False,
            statement_type=disallowed_op,
            referenced_tables=tables,
            blocked_functions=blocked,
            rejection_reason=f"Disallowed SQL operation: {disallowed_op}",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )
    if blocked:
        return SQLGuardResult(
            allowed=False,
            statement_type=statement_type,
            referenced_tables=tables,
            blocked_functions=blocked,
            rejection_reason="Blocked PostgreSQL function",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )

    reason, user_msg = _check_table_policy(tables, normalized_allowed, config.mode)
    if reason is not None:
        return SQLGuardResult(
            allowed=False,
            statement_type=statement_type,
            referenced_tables=tables,
            blocked_functions=blocked,
            rejection_reason=reason,
            user_facing_message=user_msg,
        )

    limit = _limit_value(statement)
    if limit == -1:
        return SQLGuardResult(
            allowed=False,
            statement_type=statement_type,
            referenced_tables=tables,
            rejection_reason="LIMIT must be a non-negative integer literal",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )
    if limit is not None and limit > config.max_limit:
        return SQLGuardResult(
            allowed=False,
            statement_type=statement_type,
            referenced_tables=tables,
            rejection_reason=f"LIMIT exceeds configured maximum of {config.max_limit}",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )

    limit_injected = limit is None
    guarded_sql = (
        statement.limit(config.max_limit).sql(dialect="postgres")
        if limit_injected
        else statement.sql(dialect="postgres")
    )
    return SQLGuardResult(
        allowed=True,
        sql=guarded_sql,
        statement_type=statement_type,
        intent="read",
        referenced_tables=tables,
        blocked_functions=blocked,
        limit_injected=limit_injected,
    )


def validate_sql(sql: str) -> None:
    """Backward-compatible syntax-only validation."""
    sqlglot.parse_one(sql, read="postgres")
