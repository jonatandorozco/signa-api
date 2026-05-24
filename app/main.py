import json
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_cors_origins
from app.core.logging_config import configure_logging
from app.routers import escaneo, livekit, models
from app.services.s3 import apply_presigned_model_urls

configure_logging()

app = FastAPI(
    title="Signa API",
    version="2.0.0",
    description="Sube un escaneo (.ply/.stl/.obj) con POST /escaneo y obtén el socket 3D.",
)

cors_origins = get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials="*" not in cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(livekit.router)
app.include_router(escaneo.router)
app.include_router(models.router)

DATOS_REPORTE_PATH = Path(__file__).resolve().parent / "data" / "datos_reporte.json"


@app.get("/datos_reporte")
def get_datos_reporte():
    try:
        with DATOS_REPORTE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Archivo de reporte no encontrado")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="El archivo de reporte no es JSON válido")

    try:
        apply_presigned_model_urls(data)
    except KeyError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Variable de entorno requerida no configurada: {exc.args[0]}",
        )
    except (ClientError, BotoCoreError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudieron generar URLs prefirmadas de S3: {exc}",
        )

    return data
