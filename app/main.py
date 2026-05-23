from fastapi import FastAPI

from app.data.reporte import DATOS_REPORTE

app = FastAPI(title="Signa API", version="1.0.0")


@app.get("/datos_reporte")
def get_datos_reporte():
    return DATOS_REPORTE
