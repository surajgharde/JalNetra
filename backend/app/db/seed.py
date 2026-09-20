"""`make seed` entrypoint. Sections register seeders in SEEDERS as they land
(S1: Pune-district water bodies)."""

import logging
from collections.abc import Callable

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.registry.seed import seed_pune_water_bodies

SEEDERS: dict[str, Callable[[], object]] = {
    "pune_water_bodies": seed_pune_water_bodies,
}

log = logging.getLogger(__name__)


def main() -> None:
    configure_logging(get_settings().log_level)
    if not SEEDERS:
        log.info("no seeders registered yet")
        return
    for name, seeder in SEEDERS.items():
        log.info("running seeder", extra={"seeder": name})
        result = seeder()
        log.info("seeder finished", extra={"seeder": name, "result": str(result)})


if __name__ == "__main__":
    main()
