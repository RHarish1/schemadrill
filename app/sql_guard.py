from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.models import SQLGuardConfig, SQLGuardResult

AUTHORIZATION_DENIED_MESSAGE = "You do not have access to the requested data."
SECURITY_REJECTED_MESSAGE = "The generated query was rejected by the security policy."

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
}

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


def default_guard_config(max_limit: int = 1000) -> SQLGuardConfig:
    return SQLGuardConfig(max_limit=max_limit, blocked_functions=set(BLOCKED_FUNCTIONS))


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


def guard_sql(
    sql: str,
    allowed_tables: set[str],
    config: SQLGuardConfig | None = None,
) -> SQLGuardResult:
    config = config or default_guard_config()
    normalized_allowed = {table.lower() for table in allowed_tables}
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except ParseError as error:
        return SQLGuardResult(
            allowed=False,
            statement_type="UNKNOWN",
            rejection_reason=f"SQL parse error: {error}",
            user_facing_message=SECURITY_REJECTED_MESSAGE,
        )

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
    unauthorized = sorted(set(tables) - normalized_allowed)
    if unauthorized:
        return SQLGuardResult(
            allowed=False,
            statement_type=statement_type,
            referenced_tables=tables,
            blocked_functions=blocked,
            rejection_reason=f"Unauthorized table: {unauthorized[0]}",
            user_facing_message=AUTHORIZATION_DENIED_MESSAGE,
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
        referenced_tables=tables,
        blocked_functions=blocked,
        limit_injected=limit_injected,
    )


def validate_sql(sql: str) -> None:
    """Backward-compatible syntax-only validation."""
    sqlglot.parse_one(sql, read="postgres")
