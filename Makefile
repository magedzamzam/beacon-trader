.PHONY: up down build logs ps seed login health
build: ; docker compose build
up:    ; docker compose up -d --build
down:  ; docker compose down
logs:  ; docker compose logs -f --tail=100
ps:    ; docker compose ps
login: ; docker compose run --rm telegram python login.py
seed:  ; docker compose run --rm api python -m app.seed
health:; curl -s localhost:8000/health | python -m json.tool
