"""
Herramienta 2: ejecutar_sql.

Ejecuta consultas de lectura (SELECT / WITH ... SELECT) sobre la base de
datos SQLite aplicando varias capas de seguridad:

1. La base de datos se abre en modo de solo lectura (URI mode=ro).
2. Se activa PRAGMA query_only = ON.
3. Se valida el texto SQL antes de ejecutarlo.
4. Un authorizer de SQLite bloquea cualquier operación que no sea de lectura.
5. Se rechazan múltiples sentencias en una misma ejecución.
6. Se aplican límites de filas y de tiempo de ejecución.
"""

import re
import sqlite3
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "sales.db"

MAX_FILAS = 100
MAX_SEGUNDOS = 5.0

# Palabras que nunca deben aparecer en una consulta de solo lectura.
PALABRAS_PROHIBIDAS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "TRUNCATE",
}

# Acciones del authorizer de SQLite que sí permitimos.
SQLITE_OK = getattr(sqlite3, "SQLITE_OK", 0)
SQLITE_DENY = getattr(sqlite3, "SQLITE_DENY", 1)
SQLITE_READ = getattr(sqlite3, "SQLITE_READ", 20)
SQLITE_SELECT = getattr(sqlite3, "SQLITE_SELECT", 21)
SQLITE_FUNCTION = getattr(sqlite3, "SQLITE_FUNCTION", 31)
SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
ACCIONES_PERMITIDAS = {SQLITE_READ, SQLITE_SELECT, SQLITE_FUNCTION, SQLITE_RECURSIVE}


class SQLNoPermitido(ValueError):
    """Se lanza cuando la consulta no cumple las reglas de seguridad."""


def _limpiar_sql(sql):
    """
    Sustituye el contenido de cadenas y comentarios por espacios.

    Así podemos buscar palabras prohibidas y puntos y coma sin que aparezcan
    falsos positivos dentro de un texto o de un comentario.
    """
    limpio = []
    i = 0
    estado = "normal"
    error = None

    while i < len(sql):
        caracter = sql[i]
        siguiente = sql[i + 1] if i + 1 < len(sql) else ""

        if estado == "normal":
            if caracter == "'":
                estado = "cadena_simple"
                limpio.append(" ")
            elif caracter == '"':
                estado = "cadena_doble"
                limpio.append(" ")
            elif caracter == "`":
                estado = "identificador_grave"
                limpio.append(" ")
            elif caracter == "[":
                estado = "identificador_corchete"
                limpio.append(" ")
            elif caracter == "-" and siguiente == "-":
                estado = "comentario_linea"
                limpio.extend([" ", " "])
                i += 1
            elif caracter == "/" and siguiente == "*":
                estado = "comentario_bloque"
                limpio.extend([" ", " "])
                i += 1
            else:
                limpio.append(caracter)
        elif estado in {"cadena_simple", "cadena_doble", "identificador_grave"}:
            comilla = {
                "cadena_simple": "'",
                "cadena_doble": '"',
                "identificador_grave": "`",
            }[estado]
            if caracter == comilla:
                if siguiente == comilla:
                    limpio.extend([" ", " "])
                    i += 1
                else:
                    estado = "normal"
                    limpio.append(" ")
            else:
                limpio.append(" ")
        elif estado == "identificador_corchete":
            if caracter == "]":
                estado = "normal"
            limpio.append(" ")
        elif estado == "comentario_linea":
            if caracter == "\n":
                estado = "normal"
                limpio.append("\n")
            else:
                limpio.append(" ")
        elif estado == "comentario_bloque":
            if caracter == "*" and siguiente == "/":
                estado = "normal"
                limpio.extend([" ", " "])
                i += 1
            else:
                limpio.append(" ")

        i += 1

    if estado in {"cadena_simple", "cadena_doble", "identificador_grave",
                  "identificador_corchete", "comentario_bloque"}:
        error = "La consulta tiene una cadena, identificador o comentario sin cerrar."

    return "".join(limpio), error


def validar_sql(sql):
    """Valida la consulta y lanza SQLNoPermitido si no es de solo lectura."""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLNoPermitido("La consulta SQL está vacía.")

    limpio, error = _limpiar_sql(sql)
    if error:
        raise SQLNoPermitido(error)

    texto = limpio.strip()
    if texto.endswith(";"):
        texto = texto[:-1].rstrip()
    if ";" in texto:
        raise SQLNoPermitido(
            "Solo se permite una sentencia SQL por ejecución."
        )

    palabras = re.findall(r"[A-Za-z_]+", texto.upper())
    if not palabras:
        raise SQLNoPermitido("No se encontró una sentencia SQL válida.")

    primera = palabras[0]
    if primera not in {"SELECT", "WITH"}:
        raise SQLNoPermitido(
            "Solo se permiten consultas SELECT o WITH ... SELECT."
        )
    if primera == "WITH" and "SELECT" not in palabras:
        raise SQLNoPermitido(
            "Una consulta WITH debe terminar en una sentencia SELECT."
        )

    prohibidas = sorted(set(palabras) & PALABRAS_PROHIBIDAS)
    if prohibidas:
        raise SQLNoPermitido(
            "La consulta contiene operaciones no permitidas: "
            + ", ".join(prohibidas)
        )

    return sql.strip()


def _autorizador(accion, arg1, arg2, nombre_db, disparador):
    """Authorizer de SQLite: permite únicamente operaciones de lectura."""
    if accion in ACCIONES_PERMITIDAS:
        if accion == SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
            return SQLITE_DENY
        return SQLITE_OK
    return SQLITE_DENY


def ejecutar_sql(sql, max_filas=MAX_FILAS, max_segundos=MAX_SEGUNDOS,
                 db_path=DB_PATH):
    """
    Ejecuta una consulta de lectura y devuelve un diccionario con el resultado.

    Estructura de éxito:
        {"ok": True, "columnas": [...], "filas": [...], "num_filas": n,
         "truncado": bool, "sql": "..."}

    Estructura de error:
        {"ok": False, "error": "mensaje", "sql": "..."}
    """
    try:
        sql_validado = validar_sql(sql)
    except SQLNoPermitido as exc:
        return {"ok": False, "error": str(exc), "sql": sql}

    db_path = Path(db_path)
    if not db_path.exists():
        return {
            "ok": False,
            "error": (
                f"No existe la base de datos en {db_path}. "
                "Ejecuta: python database/create_database.py"
            ),
            "sql": sql_validado,
        }

    tiempo_limite = time.monotonic() + max_segundos
    conexion = None

    def progreso():
        return 1 if time.monotonic() > tiempo_limite else 0

    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conexion = sqlite3.connect(uri, uri=True, timeout=5)
        conexion.execute("PRAGMA query_only = ON")
        conexion.set_authorizer(_autorizador)
        conexion.set_progress_handler(progreso, 1000)

        cursor = conexion.execute(sql_validado)
        columnas = [descripcion[0] for descripcion in cursor.description or []]
        filas = cursor.fetchmany(max_filas + 1)
        truncado = len(filas) > max_filas
        filas = filas[:max_filas]

        return {
            "ok": True,
            "columnas": columnas,
            "filas": [list(fila) for fila in filas],
            "num_filas": len(filas),
            "truncado": truncado,
            "sql": sql_validado,
        }
    except sqlite3.Error as exc:
        mensaje = str(exc)
        if "interrupted" in mensaje and time.monotonic() >= tiempo_limite:
            mensaje = (
                f"Se superó el tiempo máximo de ejecución "
                f"({max_segundos} segundos)."
            )
        return {"ok": False, "error": mensaje, "sql": sql_validado}
    finally:
        if conexion is not None:
            conexion.close()


def formatear_resultado(resultado, max_filas_texto=50):
    """Convierte el diccionario de resultado en texto para el modelo."""
    if not resultado.get("ok"):
        return "Error de SQLite: " + str(resultado.get("error", "error desconocido"))

    columnas = resultado.get("columnas", [])
    filas = resultado.get("filas", [])

    if not filas:
        return (
            "La consulta se ejecutó correctamente pero no devolvió filas. "
            f"Columnas: {', '.join(columnas) if columnas else 'ninguna'}"
        )

    def celda(valor):
        texto = "NULL" if valor is None else str(valor)
        texto = texto.replace("\n", " ")
        return texto if len(texto) <= 80 else texto[:77] + "..."

    lineas = [
        f"Resultado: {resultado.get('num_filas', len(filas))} fila(s) devuelta(s).",
        " | ".join(str(columna) for columna in columnas),
        "-+-".join("-" * max(3, len(str(columna))) for columna in columnas),
    ]

    for fila in filas[:max_filas_texto]:
        lineas.append(" | ".join(celda(valor) for valor in fila))

    if len(filas) > max_filas_texto:
        lineas.append(
            f"... ({len(filas) - max_filas_texto} fila(s) más no mostradas)"
        )
    if resultado.get("truncado"):
        lineas.append(
            f"(La consulta devolvió más de {MAX_FILAS} filas; "
            "el resultado fue truncado.)"
        )

    return "\n".join(lineas)


if __name__ == "__main__":
    import sys

    consulta = sys.argv[1] if len(sys.argv) > 1 else "SELECT * FROM CUSTOMERS LIMIT 5"
    print(formatear_resultado(ejecutar_sql(consulta)))
