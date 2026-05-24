import app.core.env  # noqa: F401 — carga .env antes de servicios LLM

from fastapi import FastAPI

from app.routes.convert import router as convert_router
from app.routes.socket import router as socket_router

app = FastAPI(
    title="Signa API",
    version="2.1.0",
    description=(
        "POST /socket: escaneo → OpenAI → socket.stl + socket.ply. "
        "POST /convert/stl-to-ply: conversión de mallas fuera del pipeline."
    ),
)

app.include_router(socket_router)
app.include_router(convert_router)
