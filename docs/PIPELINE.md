# Signa API — Un solo paso

Sube el escaneo del muñón y recibe el socket 3D. El backend usa `app/data/datos_reporte.json` para el diseño clínico.

```powershell
py -3.12 -m uvicorn app.main:app --reload
```

Swagger: http://127.0.0.1:8000/docs

---

## Endpoint principal

### `POST /socket`

**multipart/form-data:**

| Campo | Tipo | Default | Descripción |
|-------|------|---------|-------------|
| `file` | archivo | — | `.ply`, `.stl` o `.obj` |
| `engine` | string | `rules` | `rules` (sin LLM) o `openai` |
| `generate_stl` | bool | `true` | Generar socket.stl |
| `openai_model` | string | — | Solo si `engine=openai` |
| `fallback_to_rules` | bool | `true` | Si OpenAI falla, usar reglas |

**Flujo interno:**

1. Guarda escaneo en `app/temp_upload/`
2. Analyze → contornos + `quality_gate`
3. Agente socket + `datos_reporte.json`
4. CadQuery → loft → `output/socket_generate/{job_id}/`

**Respuesta:** `job_id`, `status`, `quality_gate`, `download_urls`, `artifacts` (rutas a JSON en disco).

---

## Descargas

| GET | Archivo |
|-----|---------|
| `/socket/{job_id}/stl` | Socket 3D |
| `/socket/{job_id}/step` | STEP (si se exportó) |
| `/socket/{job_id}/report` | `agent_cad_report.json` |
| `/socket/{job_id}/geometry` | `geometry_analysis.json` |
| `/socket/{job_id}/agent` | `agent_response.json` |

---

## Ejemplo curl

```powershell
curl.exe -X POST "http://127.0.0.1:8000/socket" `
  -F "file=@C:\ruta\munon.ply" `
  -F "engine=rules"
```

Descargar STL (usa `job_id` de la respuesta):

```powershell
curl.exe -O "http://127.0.0.1:8000/socket/<job_id>/stl"
```

---

## Salida en disco

```
output/socket_generate/<job_id>/
  geometry_analysis.json
  agent_response.json
  agent_cad_report.json
  socket.stl
  socket.step          (opcional)
```

---

## Estados

| `status` | Significado |
|----------|-------------|
| `production` | Quality gate OK, socket generado |
| `demo` | Escaneo aceptable en modo demo |
| `blocked` | Sin `socket_design` o quality gate bloqueante → sin STL |

---

## OpenAI (opcional)

En `.env`: `AZURE_OPENAI_*` o `OPENAI_API_KEY`. Luego `engine=openai` en el POST.

---

## CLI local (depuración)

```powershell
py -3.12 socket_design.cad.py --agent output/socket_generate/<job_id>/agent_response.json --out-dir ./out
```
