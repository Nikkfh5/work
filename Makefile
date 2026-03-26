.PHONY: test run logs deploy status stop setup-mcp lint

test:
	pytest tests/ -v

lint:
	ruff check .
	ruff format --check .

run:
	python -m supervisor.main

logs:
	tail -f logs/supervisor.log

status:
	@echo "=== Tests ===" && pytest tests/ -q 2>&1 | tail -1
	@echo "=== Ruff ===" && ruff check . 2>&1 | tail -1

deploy:
	@echo "Usage: ./deploy/deploy.sh user@host"

stop:
	@pkill -f "python -m supervisor.main" || echo "Not running"

setup-mcp:
	@for dir in workers/*/; do \
		echo "Setting up MCP in $$dir"; \
		cd "$$dir" && claude mcp add --transport http --scope project context7 https://mcp.context7.com/mcp 2>/dev/null || true; \
		cd ../..; \
	done
