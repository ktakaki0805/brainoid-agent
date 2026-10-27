"""Forward-only, transactional expand migrations. Version 1 is the base format."""

import sqlite3

SCHEMA_VERSION = 2
APPLICATION_ID = 0x42524456

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK(id=1), protocol_version INTEGER NOT NULL CHECK(protocol_version=1),
            retention_seconds INTEGER NOT NULL CHECK(retention_seconds>=604800),
            volume_pct INTEGER NOT NULL CHECK(volume_pct BETWEEN 0 AND 100),
            microphone_muted INTEGER NOT NULL CHECK(microphone_muted IN (0,1)),
            revision INTEGER NOT NULL DEFAULT 0
        ) STRICT""",
        "INSERT INTO settings VALUES (1,1,604800,50,0,0)",
        """CREATE TABLE registration (
            id INTEGER PRIMARY KEY CHECK(id=1), tenant_id TEXT NOT NULL, device_id TEXT NOT NULL,
            epoch TEXT NOT NULL UNIQUE, connection_generation INTEGER NOT NULL DEFAULT 0
                CHECK(connection_generation>=0),
            delivery_cursor INTEGER NOT NULL DEFAULT 0 CHECK(delivery_cursor>=0)
        ) STRICT""",
        """CREATE TABLE credential_refs (
            purpose TEXT PRIMARY KEY CHECK(purpose IN ('cloud_refresh','home_assistant')),
            name TEXT NOT NULL
        ) STRICT""",
        """CREATE TABLE commands (
            command_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
            epoch TEXT NOT NULL REFERENCES registration(epoch),
            payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
            delivery_seq INTEGER NOT NULL CHECK(delivery_seq>0),
            received_generation INTEGER NOT NULL CHECK(received_generation>0),
            start_generation INTEGER CHECK(start_generation>0),
            status TEXT NOT NULL CHECK(status IN
                ('accepted','started','succeeded','failed','expired','outcome_unknown')),
            accepted_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        ) STRICT""",
    ),
    (
        "ALTER TABLE commands ADD COLUMN result_acked INTEGER NOT NULL DEFAULT 0 CHECK(result_acked IN (0,1))",
        "CREATE INDEX command_retention ON commands(status, result_acked, updated_at_ms)",
    ),
)


def validate_format(connection: sqlite3.Connection) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION or application not in {0, APPLICATION_ID}:
        raise sqlite3.DatabaseError("unsupported storage format")
    if (
        version == 0
        and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()
    ):
        raise sqlite3.DatabaseError("unversioned nonempty database")
    if version > 0 and application != APPLICATION_ID:
        raise sqlite3.DatabaseError("unknown storage format")
    return int(version)


def migrate(connection: sqlite3.Connection) -> None:
    version = validate_format(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statements in MIGRATIONS[version:]:
            for statement in statements:
                connection.execute(statement)
            version += 1
            connection.execute(f"PRAGMA user_version={version}")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
