from fastapi import APIRouter

from app.api import tiles
from app.api.v1 import alerts, health, imagery, jobs, validations, water_bodies

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(tiles.router)

# Versioned product endpoints under /api/v1 (frozen contract; add fields, never rename).
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(water_bodies.router)
v1_router.include_router(alerts.router)
v1_router.include_router(validations.router)
v1_router.include_router(jobs.router)
v1_router.include_router(imagery.router)
