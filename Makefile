COMPOSE := docker compose -f docker-compose.dev.yml

.PHONY: up down build rebuild logs bootstrap demo clean

# Generate .env with a fresh master key on first run; skip if it exists.
.env:
	@python3 -c \
	  "import base64, os; \
	   print('CABAL_MASTER_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())" > .env
	@echo "LIMENAL_CABAL_TOKEN=" >> .env
	@echo "Created .env — CABAL_MASTER_KEY auto-generated."

## up: build images (if needed) and start the dev stack in the background.
up: .env
	$(COMPOSE) up -d --build
	@echo ""
	@echo "Services starting. Run 'make bootstrap' once cabal logs show 'cabal.ready'."
	@echo "Watch progress: make logs"

## down: stop and remove containers (volumes are preserved).
down:
	$(COMPOSE) down

## build: (re)build images without starting.
build: .env
	$(COMPOSE) build

## rebuild: force a clean rebuild ignoring the layer cache.
rebuild: .env
	$(COMPOSE) build --no-cache

## logs: tail logs from all services.
logs:
	$(COMPOSE) logs -f

## bootstrap: exchange the cabal bootstrap code for tokens and wire limenal.
bootstrap:
	@bash scripts/bootstrap.sh

## demo: end-to-end smoke test — scratchpad write + read via limenal.
demo:
	@bash scripts/demo.sh

## clean: stop containers AND delete volumes (wipes cabal DB and secrets).
clean:
	$(COMPOSE) down -v
	@rm -f .env
	@echo "Volumes and .env removed."
