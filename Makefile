.PHONY: demo up down seed run worker test eval lint mcp

demo:    ## clean clone → working demo: Mongo, seed, API, end-to-end walkthrough
	./scripts/demo.sh

up:      ## start MongoDB + Kafka
	docker compose up -d

down:
	docker compose down

seed:    ## load synthetic data
	python -m scripts.seed

run:     ## start the API
	uvicorn src.api.main:app --reload

worker:  ## start the Kafka worker
	python -m src.worker.consumer

mcp:     ## run the MCP server standalone (for MCP Inspector)
	python -m src.mcp_server.server

test:    ## mechanics — no API key, no Docker, no broker
	pytest -q

eval:    ## judgement — needs a real model (MODEL_API_KEY in .env)
	python -m evals.runner

lint:
	ruff check . && ruff format --check .
