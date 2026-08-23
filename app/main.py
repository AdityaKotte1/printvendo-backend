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

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.ratelimit import install_rate_limiting
from app.core.config import Settings, get_settings
from app.core.db import database_is_reachable
from app.core.errors import install_error_handlers
from app.jobs.scheduler import Scheduler

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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start the scheduled sweeps with the app, and stop them with it.

        In the app rather than in cron because there is no `deploy/` yet, and a
        job that runs only when somebody remembers to write a crontab is the
        same gap in a different file. Running it in every worker is safe: they
        contend for one advisory lock per job, and exactly one wins.
        """
        task = None
        if settings.SCHEDULER_ENABLED:
            task = asyncio.create_task(Scheduler(settings).run_forever())
        app.state.scheduler_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                # Awaited, not merely cancelled: without this the shutdown races
                # the job's own transaction and Postgres sees the connection
                # drop mid-statement.
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="PrintVendo API", version=VERSION, lifespan=lifespan)
    app.state.settings = settings

    # Order matters, and reads backwards: the last middleware added is the
    # outermost. Rate limiting must sit *inside* CORS so that a 429 still
    # carries the headers a browser needs to read it -- see
    # install_rate_limiting.
    install_rate_limiting(app, settings)

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
    from app.api.admin import accounts as admin_accounts
    from app.api.admin import billing as admin_billing
    from app.api.admin import kiosks as admin_kiosks
    from app.api.admin import ops as admin_ops
    from app.api.admin import payment_config as admin_payment_config
    from app.api.admin import refunds as admin_refunds
    from app.api.device import agent as device_agent
    from app.api.device import tasks as device_tasks
    from app.api.device import ws as device_ws
    from app.api.owner import billing as owner_billing
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
    app.include_router(owner_billing.router)
    app.include_router(owner_payment_config.router)
    app.include_router(refiller_kiosks.router)
    app.include_router(admin_payment_config.router)
    app.include_router(admin_ops.router)
    app.include_router(admin_kiosks.router)
    app.include_router(admin_billing.router)
    app.include_router(admin_accounts.router)
    app.include_router(admin_refunds.router)
    app.include_router(device_agent.router)
    app.include_router(device_tasks.router)
    app.include_router(device_ws.router)
    app.include_router(webhooks.router)

    @app.get("/health", tags=["meta"])
    def health() -> JSONResponse:
        """Whether this process can serve a request, not whether it is running.

        A probe that answers 200 from the web framework alone is answering the
        one question nobody is asking: uvicorn is up. It stayed green through
        every database outage the old backend had, so the load balancer kept
        sending traffic to a process that could not serve a single request.

        503 rather than 200-with-a-flag, because the thing reading this is a
        load balancer and it reads the status code.
        """
        reachable = database_is_reachable(settings.DATABASE_URL)
        return JSONResponse(
            status_code=200 if reachable else 503,
            content={
                "status": "ok" if reachable else "degraded",
                "database": "ok" if reachable else "unreachable",
                "version": VERSION,
                "env": settings.ENV,
            },
        )

    return app
