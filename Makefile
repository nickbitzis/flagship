PORT ?= 8788
URL ?= http://127.0.0.1:$(PORT)

.PHONY: install test lint format run shop bench scan export screenshots check-mobile docker-build compose-up demo-reset clean

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

format:
	uvx ruff format .
	uvx ruff check . --fix

run:  ## server and web UI on http://127.0.0.1:8788, seeded with demo data on first start
	PORT=$(PORT) uv run flagship serve

shop:  ## example app using the SDK, against a running server
	FLAGSHIP_URL=$(URL) uv run python examples/shop/app.py

bench:
	uv run flagship bench

scan:
	uv run flagship scan examples/shop --flags examples/flags-export.json

export:  ## regenerate the flag export the scan step in CI reads
	uv run flagship export --out examples/flags-export.json

screenshots:  ## needs make run in another terminal and Google Chrome
	uv run python scripts/screenshot.py --port $(PORT)

check-mobile:  ## fails if any page overflows sideways at 390px
	cd scripts && uv run python check_mobile.py --port $(PORT)

docker-build:
	docker build --target server -t flagship .
	docker build --target shop -t flagship-shop .

compose-up:
	docker compose up --build

demo-reset:  ## wipe the local database so the demo seeds again (stop the server first)
	rm -f data/flagship.db data/flagship.db-wal data/flagship.db-shm

clean:
	rm -rf .pytest_cache .ruff_cache data .flagship-cache.json
