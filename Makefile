.PHONY: setup run deploy stop install uninstall build install-pre-commit tailscale-status reset emqx-auth emqx-auth-reset

SETUP_SENTINEL := .setup-complete

setup: $(SETUP_SENTINEL)

$(SETUP_SENTINEL):
	chmod +x setup.sh
	./setup.sh

# Run locally (dev mode)
# When TAILSCALE_ENABLED=true: installs Tailscale if needed, connects, configures tailscale serve,
# then binds uvicorn to 127.0.0.1 only (tailscale serve exposes port 8000 on the tailnet)
run: emqx-auth
	docker compose up emqx postgres -d
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ "$${TAILSCALE_ENABLED:-false}" = "true" ]; then \
		echo "[INFO] Tailscale mode: setting up Tailscale for source install..."; \
		if ! command -v tailscale >/dev/null 2>&1; then \
			echo "[INFO] Installing Tailscale..."; \
			curl -fsSL https://tailscale.com/install.sh | sh; \
		fi; \
		if ! tailscale status >/dev/null 2>&1; then \
			echo "[INFO] Connecting to Tailscale network..."; \
			sudo tailscale up --authkey="$${TAILSCALE_AUTH_KEY}" --hostname="$${TAILSCALE_HOSTNAME:-hummingbot-api}" --accept-dns=true; \
		fi; \
		tailscale serve status 2>/dev/null | grep -q ":8000" || \
			sudo tailscale serve --bg http:8000 http://localhost:8000; \
		echo "[INFO] Binding uvicorn to 127.0.0.1 (tailscale serve exposes port 8000 on tailnet)"; \
		conda run --no-capture-output -n hummingbot-api uvicorn main:app --reload --host 127.0.0.1 --port 8000; \
	else \
		conda run --no-capture-output -n hummingbot-api uvicorn main:app --reload; \
	fi

# Deploy with Docker
# When TAILSCALE_ENABLED=true: adds the Tailscale sidecar compose override
deploy: $(SETUP_SENTINEL) emqx-auth
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ "$${TAILSCALE_ENABLED:-false}" = "true" ]; then \
		echo "[INFO] Deploying with Tailscale sidecar..."; \
		docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d; \
	else \
		docker compose up -d; \
	fi

EMQX_AUTH_FILE := .emqx/auth-bootstrap.csv

# Generate the EMQX built-in-database bootstrap file from the broker credentials in .env.
# EMQX ships with anonymous MQTT enabled; this seeds the one account the API and the bots
# use so the broker can reject everything else. The file holds a plaintext password, so it
# is written 0600 and gitignored.
#
# is_superuser is deliberately false: EMQX superusers bypass authorization entirely, which
# would make emqx/acl.conf dead config.
#
# NOTE: EMQX imports the bootstrap file only for users that do not already exist. Changing
# BROKER_PASSWORD in .env therefore has no effect on a broker whose emqx-data volume already
# has the account — run `make emqx-auth-reset` to drop the volume and re-seed.
emqx-auth:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	mkdir -p $(dir $(EMQX_AUTH_FILE)); \
	printf 'user_id,password,is_superuser\n%s,%s,false\n' \
		"$${BROKER_USERNAME:-admin}" "$${BROKER_PASSWORD:-password}" > $(EMQX_AUTH_FILE); \
	chmod 600 $(EMQX_AUTH_FILE); \
	echo "[INFO] Wrote $(EMQX_AUTH_FILE) for broker user $${BROKER_USERNAME:-admin}"

# Compose derives the project name from the directory name unless COMPOSE_PROJECT_NAME is set.
COMPOSE_PROJECT ?= $(notdir $(CURDIR))

# Rotate the broker credentials: wipe the EMQX state volume so the bootstrap file is
# re-imported with the current .env values. Retained messages and broker state are lost;
# bots and the API reconnect on their own. The volume is matched by compose labels rather
# than by name, since other compose projects on the same host also have emqx volumes.
emqx-auth-reset: emqx-auth
	docker compose rm -sf emqx
	@docker volume ls -q \
		--filter "label=com.docker.compose.project=$(COMPOSE_PROJECT)" \
		--filter "label=com.docker.compose.volume=emqx-data" \
		| xargs -r docker volume rm
	docker compose up -d emqx

TAILSCALE_CONTAINER := hummingbot-tailscale

# Show Tailscale connection status (Docker sidecar or local install)
tailscale-status:
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx '$(TAILSCALE_CONTAINER)'; then \
		echo "[INFO] Tailscale sidecar (Docker)"; \
		docker exec $(TAILSCALE_CONTAINER) tailscale status; \
	elif command -v tailscale >/dev/null 2>&1; then \
		echo "[INFO] Tailscale (local)"; \
		tailscale status; \
	else \
		echo "Tailscale is not available."; \
		echo "  Docker deploy: ensure TAILSCALE_ENABLED=true and run 'make deploy'"; \
		echo "  Source run:    use 'make run' with Tailscale enabled (installs locally)"; \
		exit 1; \
	fi

# Stop all services
stop:
	docker compose down

# Install conda environment
install:
	@if ! command -v conda >/dev/null 2>&1; then \
		echo "Error: Conda is not found in PATH. Please install Conda or add it to your PATH."; \
		exit 1; \
	fi
	@if conda env list | grep -q '^hummingbot-api '; then \
		echo "Environment already exists."; \
	else \
		conda env create -f environment.yml; \
	fi
	$(MAKE) install-pre-commit
	$(MAKE) setup

uninstall:
	conda env remove -n hummingbot-api -y
	rm -f $(SETUP_SENTINEL)

install-pre-commit:
	conda run -n hummingbot-api pip install pre-commit
	conda run -n hummingbot-api pre-commit install

# Build Docker image
build:
	docker build -t hummingbot/hummingbot-api:latest .

# Reset to near-origin state:
#   - stops Docker containers (with volume wipe) and/or source uvicorn if running
#   - removes .env and .setup-complete from the project root
#   - removes all credential folders under bots/credentials/ except master_account
#   - removes all .yml files under bots/credentials/master_account/
reset:
	@echo "[INFO] Checking for running hummingbot-api services..."
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'hummingbot-api'; then \
		echo "[INFO] Docker containers running — stopping and wiping volumes..."; \
		docker compose down -v; \
	else \
		echo "[INFO] No Docker containers running."; \
	fi
	@if pgrep -f "uvicorn main[:]app" >/dev/null 2>&1; then \
		echo "[INFO] Source uvicorn process found — stopping..."; \
		pkill -f "uvicorn main[:]app" || true; \
	fi
	@echo "[INFO] Removing .env and .setup-complete..."
	rm -f .env $(SETUP_SENTINEL)
	@echo "[INFO] Clearing credentials..."
	@find bots/credentials -mindepth 1 -maxdepth 1 -type d ! -name master_account -exec rm -rf {} +
	@find bots/credentials/master_account -name "*.yml" -delete
	@rm -f bots/credentials/master_account/.password_verification
	@echo "[INFO] Reset complete."
