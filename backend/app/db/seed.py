"""`make seed` entrypoint. Sections register seeders in SEEDERS as they land
(S1: Pune-district water bodies)."""

import logging
from collections.abc import Callable

from app.core.config import get_settings
from app.core.logging import configure_logging

SEEDERS: dict[str, Callable[[], None]] = {}

log = logging.getLogger(__name__)


def main() -> None:
    configure_logging(get_settings().log_level)
    if not SEEDERS:
        log.info("no seeders registered yet")
        return
    for name, seeder in SEEDERS.items():
        log.info("running seeder", extra={"seeder": name})
        seeder()


if __name__ == "__main__":
    main()
