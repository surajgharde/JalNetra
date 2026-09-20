"""Alembic environment. PostGIS-safe: PostGIS/TimescaleDB-owned objects are
excluded from autogenerate so they are never dropped by a generated migration."""

import logging
from logging.config import fileConfig
from typing import Any

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import engine_from_config, pool

from app.core.config import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url_sync)

target_metadata = Base.metadata

log = logging.getLogger("alembic.env")

# Tables managed by extensions, never by us.
_EXTENSION_TABLES = {"spatial_ref_sys"}
_EXTENSION_SCHEMAS = {"tiger", "tiger_data", "topology", "timescaledb_information"}


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    if type_ == "table":
        if name in _EXTENSION_TABLES:
            return False
        schema = getattr(obj, "schema", None)
        if schema and (schema in _EXTENSION_SCHEMAS or schema.startswith("_timescaledb")):
            return False
    return bool(
        alembic_helpers.include_object(obj, name, type_, reflected, compare_to)  # type: ignore[no-untyped-call]
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        process_revision_directives=alembic_helpers.writer,
        render_item=alembic_helpers.render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            process_revision_directives=alembic_helpers.writer,
            render_item=alembic_helpers.render_item,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
