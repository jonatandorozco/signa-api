"""Carga variables desde .env en la raíz del proyecto."""

from dotenv import load_dotenv

from app.core.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")
