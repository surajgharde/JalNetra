# JalNetra developer entrypoints. Every target that touches services runs
# inside Docker Compose so a clean clone only needs Docker.
COMPOSE ?= docker compose
UV      ?= uv

.PHONY: up down logs migrate revision test test-integration lint fmt seed load mgrs-grid shell psql clean

up:            ## Build and start the full local stack
	$(COMPOSE) up -d --build

down:          ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs:          ## Tail all service logs
	$(COMPOSE) logs -f --tail=100

migrate:       ## Apply Alembic migrations
	$(COMPOSE) run --rm api alembic upgrade head

revision:      ## Autogenerate a migration: make revision m="add zones"
	$(COMPOSE) run --rm api alembic revision --autogenerate -m "$(m)"

test:          ## Run the backend unit tests inside the api image
	$(COMPOSE) run --rm api pytest

test-integration: ## Run unit + integration tests against the live stack
	$(COMPOSE) run --rm -e JALNETRA_INTEGRATION=1 api pytest

lint:          ## ruff + mypy (local, via uv)
	cd backend && $(UV) run ruff check . && $(UV) run ruff format --check . && $(UV) run mypy .

fmt:           ## Auto-format with ruff
	cd backend && $(UV) run ruff format . && $(UV) run ruff check --fix .

seed:          ## Load seed data (S1: 30 Pune-district water bodies + zones)
	$(COMPOSE) run --rm api python -m app.db.seed

load:          ## Load a GeoJSON/shapefile: make load f=path.geojson d=Pune
	$(COMPOSE) run --rm -v "$(abspath $(f))":/data/in$(suffix $(f)):ro api 		python -m app.services.registry.load /data/in$(suffix $(f)) --district "$(d)"

mgrs-grid:     ## Download ESA's Sentinel-2 tiling grid and build the exact MGRS cache
	cd backend && $(UV) run python -m app.services.registry.mgrs download

shell:         ## Shell inside the api container
	$(COMPOSE) run --rm api bash

psql:          ## psql into the database
	$(COMPOSE) exec postgres psql -U jalnetra -d jalnetra

clean:         ## Stop the stack and delete volumes
	$(COMPOSE) down -v --remove-orphans
