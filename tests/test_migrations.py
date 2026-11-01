"""Migration tests (roadmap step 2, specification §8; step 5 claim tracking).

`alembic upgrade head` must create all application tables on an empty
database with every spec §8 uniqueness constraint, the §9.2 account-slot
exclusivity index, the §9.3 `publication_tasks.claimed_at` column, and the
`engine_state` heartbeat table; `downgrade base` must drop them. The
migrations run through the Alembic command API against a temporary SQLite
file — no live external database (AGENTS.md §5).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from publisher.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
HEAD_REVISION = "b3d1c7e94a52"
APP_TABLES = {
    "platforms",
    "posts",
    "accounts",
    "publishing_schedules",
    "publication_tasks",
    "publication_attempts",
    "engine_state",
}


def _config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture
def alembic_config(tmp_path, monkeypatch):
    """Point DATABASE_URL at a fresh SQLite file and migrate it to head."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    config = _config()
    command.upgrade(config, "head")
    return config, f"sqlite:///{db_path}"


def test_upgrade_head_creates_all_app_tables(alembic_config) -> None:
    config, url = alembic_config
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert APP_TABLES <= tables
        claimed_at = {
            c["name"] for c in inspect(engine).get_columns("publication_tasks")
        }
        assert "claimed_at" in claimed_at  # stale-claim recovery (spec §9.3)
        version = engine.connect().execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert version == HEAD_REVISION
    finally:
        engine.dispose()


def test_upgraded_schema_columns_match_models(alembic_config) -> None:
    """The migration schema must mirror the model metadata column-for-column."""
    config, url = alembic_config
    migrated = create_engine(url)
    modeled = create_engine("sqlite://")
    try:
        Base.metadata.create_all(modeled)
        migrated_inspector = inspect(migrated)
        model_inspector = inspect(modeled)
        for table in sorted(APP_TABLES):
            migrated_columns = {c["name"] for c in migrated_inspector.get_columns(table)}
            model_columns = {c["name"] for c in model_inspector.get_columns(table)}
            assert migrated_columns == model_columns, f"column drift in {table}"
    finally:
        modeled.dispose()
        migrated.dispose()


def test_spec_uniqueness_constraints_and_status_check_present(alembic_config) -> None:
    config, url = alembic_config
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        uniques = {
            constraint["name"]: set(constraint["column_names"])
            for table in APP_TABLES
            for constraint in inspector.get_unique_constraints(table)
            if constraint.get("name")
        }
        assert uniques["uq_posts_source_external_id"] == {"source", "external_id"}
        assert uniques["uq_accounts_platform_key"] == {"platform_id", "key"}
        assert uniques["uq_publication_tasks_post_account"] == {"post_id", "account_id"}
        assert uniques["uq_publication_attempts_task_number"] == {"task_id", "attempt_number"}
        assert "key" in [c["name"] for c in inspector.get_columns("platforms")]
        # SQLite inspectors do not reflect inline column UNIQUE constraints;
        # verify platforms.key uniqueness behaviorally.
        platforms_ddl = engine.connect().execute(
            text("SELECT sql FROM sqlite_master WHERE name='platforms'")
        ).scalar_one()
        assert "UNIQUE" in platforms_ddl.upper()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO platforms (id, key, display_name, enabled, created_at) "
                    "VALUES ('p1', 'telegram', 'T', 1, '2026-01-01 00:00:00')"
                )
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO platforms (id, key, display_name, enabled, created_at) "
                        "VALUES ('p2', 'telegram', 'T2', 1, '2026-01-01 00:00:00')"
                    )
                )

        ddl = engine.connect().execute(
            text("SELECT sql FROM sqlite_master WHERE name='publication_tasks'")
        ).scalar_one()
        assert "ck_publication_tasks_status" in ddl
    finally:
        engine.dispose()


def test_slot_exclusivity_index_present(alembic_config) -> None:
    """Spec §9.2: the unique (account_id, scheduled_at) index exists."""
    config, url = alembic_config
    engine = create_engine(url)
    try:
        indexes = {
            index["name"]: set(index["column_names"])
            for index in inspect(engine).get_indexes("publication_tasks")
        }
        assert indexes["uq_publication_tasks_account_slot"] == {
            "account_id",
            "scheduled_at",
        }
        index_ddl = engine.connect().execute(
            text("SELECT sql FROM sqlite_master WHERE name='uq_publication_tasks_account_slot'")
        ).scalar_one()
        assert "UNIQUE" in index_ddl.upper()
    finally:
        engine.dispose()


def test_slot_exclusivity_index_rejects_duplicate_account_slots(alembic_config) -> None:
    config, url = alembic_config
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO platforms (id, key, display_name, enabled, created_at) "
                    "VALUES ('p1', 'telegram', 'T', 1, '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO accounts (id, platform_id, key, display_name, "
                    "credentials_ref, configuration_json, active, created_at, updated_at) "
                    "VALUES ('a1', 'p1', 'english_channel', 'A', 'TELEGRAM_BOT_TOKEN', '{}', 1, "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO posts (id, source, external_id, content_json, post_type, "
                    "received_at, created_at) VALUES ('po1', 's', 'e1', '{}', 'dialogue', "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO posts (id, source, external_id, content_json, post_type, "
                    "received_at, created_at) VALUES ('po2', 's', 'e2', '{}', 'dialogue', "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
            task_sql = (
                "INSERT INTO publication_tasks (id, post_id, account_id, status, scheduled_at, "
                "attempt_count, created_at, updated_at) VALUES ('{tid}', '{pid}', 'a1', "
                "'scheduled', '2026-11-02 04:00:00', 0, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
            )
            conn.execute(text(task_sql.format(tid="t1", pid="po1")))
            with pytest.raises(IntegrityError):
                conn.execute(text(task_sql.format(tid="t2", pid="po2")))
            # NULL scheduled_at rows are unaffected by the unique index.
            conn.execute(
                text(
                    "INSERT INTO publication_tasks (id, post_id, account_id, status, "
                    "attempt_count, created_at, updated_at) VALUES ('t3', 'po2', 'a1', "
                    "'pending', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
    finally:
        engine.dispose()


def test_downgrade_base_drops_all_application_tables(alembic_config) -> None:
    config, url = alembic_config
    command.downgrade(config, "base")
    engine = create_engine(url)
    try:
        remaining = APP_TABLES & set(inspect(engine).get_table_names())
        assert remaining == set()
    finally:
        engine.dispose()


def test_upgrade_at_head_is_idempotent(alembic_config) -> None:
    config, url = alembic_config
    command.upgrade(config, "head")  # already at head: must be a no-op
    engine = create_engine(url)
    try:
        version = engine.connect().execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert version == HEAD_REVISION
    finally:
        engine.dispose()
