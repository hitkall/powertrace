.PHONY: install test lint simulate dry-run-export demo-up demo-down

install:
	pip3 install -r requirements.txt -r requirements-dev.txt

test:
	python3 -m pytest

lint:
	python3 -m ruff check .

simulate:
	python3 main.py simulate

dry-run-export:
	python3 export_to_otel.py --dry-run

demo-up:
	docker compose up -d
	@echo "Waiting 20s for services..."
	sleep 20
	python3 main.py simulate
	python3 export_to_otel.py
	@echo "Grafana: http://localhost:3000  (admin / powertrace)"
	@echo "Prometheus: http://localhost:9090"

demo-down:
	docker compose down
