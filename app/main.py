from fastapi import FastAPI

from app.routes.socket import router as socket_router

app = FastAPI(
    title="Signa API",
    version="2.0.0",
    description="Sube un escaneo (.ply/.stl/.obj) con POST /socket y obtén el socket 3D.",
)

app.include_router(socket_router)
