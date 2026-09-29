"""
core/observability.py — error reporting (Vol. 6 §5).

`SENTRY_DSN` sat in `core/config.py` and was forwarded by the prod compose since
the initial commit, and nothing ever called `sentry_sdk.init`. Setting the DSN
did nothing, so the first production error would have been visible only in
container logs. This is the one call site.

Called from `main.py` (API) and `workers/celery_app.py` (workers + beat). The
SDK auto-enables its FastAPI/Starlette and Celery integrations when those
packages are importable, so no integration list is spelled out here.

No DSN means no-op, which is what dev and the test suite get.

`send_default_pii=False` is stated rather than inherited: request bodies here
carry BYOK keys, tool credentials and webhook payloads, and none of that may
leave the box in an error report.
"""

from __future__ import annotations

import logging

from src.core.config import settings

logger = logging.getLogger(__name__)


def init_sentry(service: str) -> bool:
    """Initialise Sentry if a DSN is configured. Returns whether it did."""
    if not settings.SENTRY_DSN:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        send_default_pii=False,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service)
    logger.info("Sentry initialised [service=%s env=%s]", service, settings.APP_ENV)
    return True
