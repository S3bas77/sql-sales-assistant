"""
Herramienta 1: consultar_esquema.

Devuelve una descripción legible del esquema de la base de datos:
tablas, columnas, tipos, claves primarias, claves foráneas y número de filas.
"""

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "sales.db"


def _conectar_solo_lectura(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _identificador(nombre):
    """Escapa un nombre de tabla para usarlo en una sentencia PRAGMA."""
    return '"' + nombre.replace('"', '""') + '"'


def consultar_esquema(db_path=DB_PATH):
    """Devuelve el esquema completo en texto para que lo entienda el modelo."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(
            f"No existe la base de datos en {db_path}. "
            "Ejecuta: python database/create_database.py"
        )

    conexion = _conectar_solo_lectura(db_path)
    try:
        tablas = conexion.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        lineas = ["Esquema de la base de datos SQLite:", ""]

        for (tabla,) in tablas:
            filas = conexion.execute(
                f"SELECT COUNT(*) FROM {_identificador(tabla)}"
            ).fetchone()[0]
            columnas = conexion.execute(
                f"PRAGMA table_info({_identificador(tabla)})"
            ).fetchall()
            foraneas = conexion.execute(
                f"PRAGMA foreign_key_list({_identificador(tabla)})"
            ).fetchall()

            lineas.append(f"Tabla: {tabla} ({filas} filas)")
            lineas.append("  Columnas:")
            for columna in columnas:
                _, nombre, tipo, no_nulo, por_defecto, es_pk = columna
                detalles = [tipo or "SIN TIPO"]
                if es_pk:
                    detalles.append("PRIMARY KEY")
                if no_nulo:
                    detalles.append("NOT NULL")
                if por_defecto is not None:
                    detalles.append(f"DEFAULT {por_defecto}")
                lineas.append(f"    - {nombre}: {', '.join(detalles)}")

            if foraneas:
                lineas.append("  Claves foráneas:")
                for fk in foraneas:
                    _, _, tabla_destino, columna_origen, columna_destino, *_ = fk
                    lineas.append(
                        f"    - {columna_origen} -> "
                        f"{tabla_destino}({columna_destino})"
                    )
            else:
                lineas.append("  Claves foráneas: ninguna")

            lineas.append("")

        lineas.append("Relaciones principales:")
        lineas.append("  CUSTOMERS.customer_id <- SALES.customer_id")
        lineas.append("  PRODUCTS.product_id <- SALES.product_id")
        lineas.append("  CATEGORIES.category_id <- PRODUCTS.category_id")

        return "\n".join(lineas)
    finally:
        conexion.close()


if __name__ == "__main__":
    print(consultar_esquema())
