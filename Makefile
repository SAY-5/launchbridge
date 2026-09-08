GIT_SHA   ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo local)
IMAGE_REPO ?= launchbridge
IMAGE     ?= $(IMAGE_REPO):$(GIT_SHA)
BASE_URL  ?= http://localhost:8080
RECEIVER_URL ?= http://localhost:8081
SMOKE_SOURCE ?= smoke
SMOKE_SECRET ?= smoke-dev-secret
ADMIN_API_KEY ?= dev-admin-key
COMPOSE   := IMAGE=$(IMAGE) docker compose
TF_DIR    := deploy/terraform

.PHONY: install lint fmt test build up down logs migrate smoke demo tf-fmt tf-validate tf-plan ci clean

install:
	uv sync --python 3.12 --extra dev

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest -q

build:
	docker build --build-arg GIT_SHA=$(GIT_SHA) -t $(IMAGE) -t $(IMAGE_REPO):latest .
	@echo "built $(IMAGE)"

up: build
	$(COMPOSE) up -d --wait
	@echo "api $(BASE_URL)  receiver $(RECEIVER_URL)  docs $(BASE_URL)/docs"

down:
	$(COMPOSE) down -v --remove-orphans

logs:
	$(COMPOSE) logs -f api worker

migrate:
	$(COMPOSE) run --rm migrate

smoke:
	BASE_URL=$(BASE_URL) RECEIVER_URL=$(RECEIVER_URL) SMOKE_SOURCE=$(SMOKE_SOURCE) \
	SMOKE_SECRET=$(SMOKE_SECRET) ADMIN_API_KEY=$(ADMIN_API_KEY) \
	uv run python -m smoke.smoke

demo: up
	BASE_URL=$(BASE_URL) RECEIVER_URL=$(RECEIVER_URL) ADMIN_API_KEY=$(ADMIN_API_KEY) \
	uv run python scripts/demo.py

tf-fmt:
	terraform -chdir=$(TF_DIR) fmt -check -recursive

tf-validate:
	terraform -chdir=$(TF_DIR) init -backend=false -input=false >/dev/null
	terraform -chdir=$(TF_DIR) validate

tf-plan:
	terraform -chdir=$(TF_DIR) init -input=false
	terraform -chdir=$(TF_DIR) plan -input=false -var image_tag=$(GIT_SHA)

ci: lint test build tf-fmt tf-validate

clean:
	rm -rf .pytest_cache .ruff_cache demo-summary.txt
