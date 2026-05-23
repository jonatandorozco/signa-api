import json
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException

from app.services.s3 import apply_presigned_model_urls

app = FastAPI(title="Signa API", version="1.0.0")

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
