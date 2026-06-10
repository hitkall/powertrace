.PHONY: install test lint simulate dry-run-export demo-up demo-down clean

install:
	pip install -r requirements.txt -r requirements-dev.txt

test:
	pytest

lint:
	ruff check .

simulate:
	python main.py simulate

dry-run-export:
	python export_to_otel.py --dry-run

demo-up:
	docker compose up -d
	@echo "Waiting 20s for services to start..."
	sleep 20
	python main.py simulate
	python export_to_otel.py
	@echo "Grafana:    http://localhost:3000  (admin / powertrace)"
	@echo "Prometheus: http://localhost:9090"

demo-down:
	docker compose down

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -prune -exec rm -rf {} +
