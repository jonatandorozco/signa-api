from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_ROOT.parent
TEMP_UPLOAD_DIR = APP_ROOT / "temp_upload"
SOCKET_OUTPUT_ROOT = PROJECT_ROOT / "output" / "socket_generate"
DATOS_REPORTE_PATH = APP_ROOT / "data" / "datos_reporte.json"
