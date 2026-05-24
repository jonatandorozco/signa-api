.PHONY: dev web agent

dev:
	honcho start

web:
	PORT=8000 uv run uvicorn app.main:app --reload --host 0.0.0.0 --port $$PORT

agent:
	uv run python -m app.agent.intake dev
