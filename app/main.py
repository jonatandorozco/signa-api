import json
import uuid
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, File, HTTPException, UploadFile

from app.services.s3 import (
    apply_presigned_model_urls,
    upload_fileobj_to_r2,
    validate_scan_extension,
)

app = FastAPI(title="Signa API", version="1.0.0")

DATOS_REPORTE_PATH = Path(__file__).resolve().parent / "data" / "datos_reporte.json"
ESCANEO_PREFIX = "escaneos"


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


@app.post("/escaneo")
async def subir_escaneo(archivo: UploadFile = File(...)):
    try:
        extension = validate_scan_extension(archivo.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not archivo.filename:
        raise HTTPException(status_code=400, detail="El archivo debe tener nombre")

    object_key = f"{ESCANEO_PREFIX}/{uuid.uuid4().hex}{extension}"

    try:
        upload_fileobj_to_r2(archivo.file, object_key)
    except KeyError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Variable de entorno requerida no configurada: {exc.args[0]}",
        )
    except (ClientError, BotoCoreError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo subir el escaneo a R2: {exc}",
        )

    return {
        "object_key": object_key,
        "filename": archivo.filename,
        "format": extension.lstrip("."),
    }
