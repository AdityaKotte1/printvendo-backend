"""Alembic environment.

The database URL comes from the environment, never from alembic.ini, so the same
migration set runs against dev, test and production without editing a file.

target_metadata is app.core.db.Base.metadata, and every module's models are
imported below so autogenerate sees the whole schema. A module whose models are
not imported here is invisible to migrations.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL must be set to run migrations")
config.set_main_option("sqlalchemy.url", database_url)

# Every module's models are imported here so autogenerate sees the whole
# schema. A module missing from this list is invisible to migrations, and its
# tables silently never get created.
from app.modules.identity import models as _identity_models  # noqa: F401,E402
from app.modules.kiosks import models as _kiosks_models  # noqa: F401,E402
from app.modules.payments import models as _payments_models  # noqa: F401,E402

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render custom column types as the plain DDL they compile to.

    Without this, autogenerate writes `app.core.db.EnumText(length=16)` into the
    migration and does not import `app` -- so the migration dies with NameError
    the moment it runs.

    Rendering the underlying type is also the right thing independently: a
    migration is a historical record of a schema change, and it must not depend
    on application code that can be renamed, moved or deleted later. A migration
    from a year ago has to keep working against today's codebase.
    """
    if type_ == "type" and hasattr(obj, "impl") and hasattr(obj, "length"):
        # No import needed: script.py.mako always emits `import sqlalchemy as sa`,
        # and adding it again produces a duplicate the linter rejects.
        return f"sa.String(length={obj.length})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
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
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
