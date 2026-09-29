from contextlib import contextmanager

from app.config import Settings
from app.models import DDLBlock
from app.retrieval import PgVectorRetrievalProvider, _retrieve_with_fk_expansion


class FakeCursor:
    def __init__(self, neighbors, ddl_rows):
        self.neighbors = neighbors
        self.ddl_rows = ddl_rows
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params):
        if "information_schema.table_constraints" in query:
            self.rows = self.neighbors
        else:
            self.rows = self.ddl_rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, neighbors, ddl_rows):
        self.neighbors = neighbors
        self.ddl_rows = ddl_rows

    def cursor(self, row_factory=None):
        return FakeCursor(self.neighbors, self.ddl_rows)


def _block(schema_name, table_name):
    return DDLBlock(
        schema_name=schema_name,
        table_name=table_name,
        ddl_text=f"CREATE TABLE {schema_name}.{table_name};",
    )


def _fk_row(table_name, referenced_table):
    return {
        "table_schema": "chinook",
        "table_name": table_name,
        "referenced_schema": "chinook",
        "referenced_table": referenced_table,
    }


def _ddl_row(table_name):
    return {
        "schema_name": "chinook",
        "table_name": table_name,
        "ddl_text": f"CREATE TABLE chinook.{table_name};",
    }


def _stub_connection(monkeypatch, neighbors, ddl_rows):
    @contextmanager
    def connection():
        yield FakeConnection(neighbors, ddl_rows)

    monkeypatch.setattr("app.retrieval.readonly_connection", connection)


def test_expansion_disabled_returns_original_vector_results(monkeypatch):
    original = [_block("chinook", "artist")]
    monkeypatch.setattr("app.retrieval._retrieve_pgvector", lambda question, db, top_k: original)

    result = PgVectorRetrievalProvider(Settings(enable_fk_expansion=False)).retrieve(
        "question", "chinook", 3
    )

    assert result is original


def test_enabled_expansion_adds_outgoing_fk_neighbor(monkeypatch):
    _stub_connection(
        monkeypatch,
        [_fk_row("album", "artist")],
        [_ddl_row("album")],
    )

    result = _retrieve_with_fk_expansion([_block("chinook", "artist")], "chinook")

    assert result.fk_added == ["chinook.album"]
    assert [(block.schema_name, block.table_name) for block in result.blocks] == [
        ("chinook", "artist"),
        ("chinook", "album"),
    ]


def test_enabled_expansion_adds_incoming_fk_neighbor(monkeypatch):
    _stub_connection(
        monkeypatch,
        [_fk_row("track", "album")],
        [_ddl_row("track")],
    )

    result = _retrieve_with_fk_expansion([_block("chinook", "album")], "chinook")

    assert result.fk_added == ["chinook.track"]


def test_already_retrieved_fk_neighbor_is_not_duplicated(monkeypatch):
    _stub_connection(
        monkeypatch,
        [_fk_row("album", "artist")],
        [],
    )

    result = _retrieve_with_fk_expansion(
        [_block("chinook", "artist"), _block("chinook", "album")], "chinook"
    )

    assert result.fk_added == []
    assert len(result.blocks) == 2


def test_expansion_is_one_hop_only(monkeypatch):
    _stub_connection(
        monkeypatch,
        [
            _fk_row("album", "artist"),
            _fk_row("track", "album"),
        ],
        [_ddl_row("album")],
    )

    result = _retrieve_with_fk_expansion([_block("chinook", "artist")], "chinook")

    assert result.fk_added == ["chinook.album"]
    assert all(block.table_name != "track" for block in result.blocks)


def test_expanded_tables_are_sorted_deterministically(monkeypatch):
    _stub_connection(
        monkeypatch,
        [
            _fk_row("zebra", "artist"),
            _fk_row("album", "artist"),
        ],
        [
            _ddl_row("zebra"),
            _ddl_row("album"),
        ],
    )

    result = _retrieve_with_fk_expansion([_block("chinook", "artist")], "chinook")

    assert result.fk_added == ["chinook.album", "chinook.zebra"]
    assert [block.table_name for block in result.blocks] == ["artist", "album", "zebra"]
