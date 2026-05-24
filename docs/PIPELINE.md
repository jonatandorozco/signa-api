# Signa API — Un solo paso

Sube el escaneo del muñón y recibe el socket 3D. El backend usa `app/data/datos_reporte.json` y **siempre OpenAI**.

```powershell
py -3.12 -m uvicorn app.main:app --reload
```

Swagger: http://127.0.0.1:8000/docs

---

## Endpoint principal

### `POST /socket`

**multipart/form-data:**

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `file` | archivo | `.ply`, `.stl` o `.obj` |

**Flujo interno:**

1. Guarda escaneo en `app/temp_upload/`
2. Analyze → contornos + `quality_gate`
3. OpenAI + `datos_reporte.json`
4. CadQuery → loft → `output/socket_generate/{job_id}/` (**socket.stl** + **socket.ply**)

**Respuesta:** `job_id`, `status`, `quality_gate`, `download_urls`, `artifacts`.

---

## Conversión STL → PLY (fuera del pipeline)

### `POST /convert/stl-to-ply`

Sube un `.stl` y descarga el `.ply` convertido. No usa analyze ni OpenAI.

CLI equivalente:

```powershell
py -3.12 scripts/stl_to_ply.py output/socket_generate/<job_id>/socket.stl
```

---

## Descargas

| GET | Archivo |
|-----|---------|
| `/socket/{job_id}/stl` | Socket 3D (STL) |
| `/socket/{job_id}/ply` | Socket 3D (PLY) |
| `/socket/{job_id}/step` | STEP (si se exportó) |
| `/socket/{job_id}/report` | `agent_cad_report.json` |
| `/socket/{job_id}/geometry` | `geometry_analysis.json` |
| `/socket/{job_id}/agent` | `agent_response.json` |

---

## Ejemplo curl

```powershell
curl.exe -X POST "http://127.0.0.1:8000/socket" `
  -F "file=@C:\ruta\munon.ply"
```

Descargar STL:

```powershell
curl.exe -O "http://127.0.0.1:8000/socket/<job_id>/stl"
```

---

## OpenAI (obligatorio)

En `.env`: `AZURE_OPENAI_*` o `OPENAI_API_KEY`. Sin fallback a reglas.
