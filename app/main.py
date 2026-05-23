import json
from pathlib import Path

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Signa API", version="1.0.0")

DATOS_REPORTE_PATH = Path(__file__).resolve().parent / "data" / "datos_reporte.json"


@app.get("/datos_reporte")
def get_datos_reporte():
    try:
        with DATOS_REPORTE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Archivo de reporte no encontrado")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="El archivo de reporte no es JSON válido")
