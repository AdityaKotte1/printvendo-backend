"""Application factory.

create_app takes Settings explicitly so tests can build an app without touching
the environment, and so a future worker process can build one with a different
configuration. The CORS allowlist comes from settings — adding a frontend is a
deploy variable, never a code change.

**There is deliberately no module-level `app = create_app()`.** Building the app
at import time would call get_settings(), so importing this module would require
a fully populated environment — which breaks pytest collection, Alembic and
import-linter, none of which have any business needing production config. Run it
with uvicorn's factory flag instead:

    uvicorn app.main:create_app --factory
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers

VERSION = "0.1.0"


def _configure_logging(settings: Settings) -> None:
    """Make the application's own loggers audible.

    uvicorn configures its access and error loggers and nothing else, so
    anything logged by app.* is discarded by the root logger's default WARNING
    level. That silently broke LoggingNotifier: it claimed a developer could
    finish an email-verification flow locally by reading the log, and the line
    never appeared.

    INFO in dev, WARNING elsewhere -- verification tokens are secrets and have
    no business in a production log.
    """
    level = logging.INFO if settings.ENV == "dev" else logging.WARNING
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)

    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        app_logger.addHandler(handler)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    app = FastAPI(title="PrintVendo API", version=VERSION)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    # Imported inside the factory on purpose: at module scope this would make
    # `import app.main` pull in the whole route tree, and tests/authz builds an
    # app purely to enumerate routes.
    from app.api import webhooks
    from app.api.admin import ops as admin_ops
    from app.api.admin import payment_config as admin_payment_config
    from app.api.device import agent as device_agent
    from app.api.device import tasks as device_tasks
    from app.api.owner import earnings as owner_earnings
    from app.api.owner import kiosks as owner_kiosks
    from app.api.owner import payment_config as owner_payment_config
    from app.api.refiller import kiosks as refiller_kiosks
    from app.api.student import auth as student_auth
    from app.api.student import documents as student_documents
    from app.api.student import kiosks as student_kiosks
    from app.api.student import orders as student_orders
    from app.api.student import staff as student_staff
    from app.api.student import wallet as student_wallet

    app.include_router(student_auth.router)
    app.include_router(student_staff.router)
    app.include_router(student_documents.router)
    app.include_router(student_kiosks.router)
    app.include_router(student_orders.router)
    app.include_router(student_wallet.router)
    app.include_router(owner_kiosks.router)
    app.include_router(owner_earnings.router)
    app.include_router(owner_payment_config.router)
    app.include_router(refiller_kiosks.router)
    app.include_router(admin_payment_config.router)
    app.include_router(admin_ops.router)
    app.include_router(device_agent.router)
    app.include_router(device_tasks.router)
    app.include_router(webhooks.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION, "env": settings.ENV}

    return app
