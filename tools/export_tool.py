"""
Herramienta 3: exportar_csv.

Convierte el resultado de una consulta en un archivo CSV dentro de exports/.
El nombre del archivo incluye fecha, hora y un identificador único para no
sobrescribir exportaciones anteriores.
"""

import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORTS_DIR = BASE_DIR / "exports"


def _nombre_seguro(nombre):
    nombre = (nombre or "resultado").strip()
    nombre = re.sub(r"[^A-Za-z0-9_-]+", "_", nombre)
    return nombre.strip("_") or "resultado"


def exportar_csv(resultado, nombre_base="resultado", exports_dir=EXPORTS_DIR):
    """
    Exporta un resultado de ejecutar_sql a CSV y devuelve la ruta del archivo.

    El resultado debe tener la estructura devuelta por ejecutar_sql:
    {"ok": True, "columnas": [...], "filas": [[...], ...], ...}
    """
    if not isinstance(resultado, dict) or not resultado.get("ok"):
        error = resultado.get("error") if isinstance(resultado, dict) else None
        raise ValueError(
            "No hay un resultado válido para exportar."
            + (f" Detalle: {error}" if error else "")
        )

    exports_dir = Path(exports_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)

    marca_tiempo = datetime.now().strftime("%Y%m%d_%H%M%S")
    sufijo = uuid4().hex[:8]
    nombre_archivo = (
        f"{_nombre_seguro(nombre_base)}_{marca_tiempo}_{sufijo}.csv"
    )
    ruta = exports_dir / nombre_archivo

    # Si por alguna razón existiera, se genera otro nombre.
    while ruta.exists():
        sufijo = uuid4().hex[:8]
        nombre_archivo = (
            f"{_nombre_seguro(nombre_base)}_{marca_tiempo}_{sufijo}.csv"
        )
        ruta = exports_dir / nombre_archivo

    dataframe = pd.DataFrame(
        resultado.get("filas", []),
        columns=resultado.get("columnas", []),
    )
    dataframe.to_csv(ruta, index=False, encoding="utf-8-sig")

    return str(ruta.resolve())


if __name__ == "__main__":
    from tools.sql_tool import ejecutar_sql

    resultado = ejecutar_sql("SELECT * FROM CUSTOMERS")
    print("Archivo creado en:", exportar_csv(resultado, "clientes"))
