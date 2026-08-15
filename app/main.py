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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers

VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

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
    from app.api.student import auth as student_auth

    app.include_router(student_auth.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION, "env": settings.ENV}

    return app
