COMPOSE := docker compose -f docker-compose.dev.yml
# Refresh the Dockerfile's apt upgrade layer once a day (see APT_REFRESH there).
export APT_REFRESH ?= $(shell date +%F)

.PHONY: up down build rebuild logs bootstrap demo test-docker test-unit test-integration clean check-techne

# Generate .env with a fresh master key on first run; skip if it exists.
.env:
	@python3 -c \
	  "import base64, os; \
	   print('CABAL_MASTER_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())" > .env
	@echo "LIMINAL_CABAL_TOKEN=" >> .env
	@echo "Created .env — CABAL_MASTER_KEY auto-generated."

## check-techne: ../techne is at the tag the Dockerfile pins (TECHNE_ANY=1 skips).
check-techne:
	@bash scripts/check-techne-sibling.sh

## up: build images (if needed) and start the dev stack in the background.
up: .env check-techne
	$(COMPOSE) up -d --build
	@echo ""
	@echo "Services starting. Run 'make bootstrap' once cabal logs show 'cabal.ready'."
	@echo "Watch progress: make logs"

## down: stop and remove containers (volumes are preserved).
down:
	$(COMPOSE) down

## build: (re)build images without starting.
build: .env check-techne
	$(COMPOSE) build

## rebuild: force a clean rebuild ignoring the layer cache.
rebuild: .env check-techne
	$(COMPOSE) build --no-cache

## logs: tail logs from all services.
logs:
	$(COMPOSE) logs -f

## bootstrap: exchange the cabal bootstrap code for tokens and wire liminal.
bootstrap:
	@bash scripts/bootstrap.sh

## demo: end-to-end smoke test — scratchpad write + read via liminal.
demo:
	@bash scripts/demo.sh

## test-unit: run unit tests only (no sibling deps required; CI unit job).
test-unit:
	pytest -q

## test-integration: run full suite (sibling deps must be installed).
test-integration:
	pytest -q

## test-docker: run the full demo flow in a clean Ubuntu 22.04 container.
test-docker:
	@bash scripts/test-docker.sh

## clean: stop containers AND delete volumes (wipes cabal DB and secrets).
clean:
	$(COMPOSE) down -v
	@rm -f .env
	@echo "Volumes and .env removed."
