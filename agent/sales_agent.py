"""
Agente de ventas que usa Ollama (API REST /api/chat) y tool calling.

El agente decide cuándo consultar el esquema, cuándo ejecutar SQL y cuándo
exportar un CSV. No contiene consultas SQL predefinidas para responder las
preguntas del usuario.
"""

import difflib
import json
import os
import re
import unicodedata

import requests

from tools.export_tool import exportar_csv
from tools.schema_tool import consultar_esquema
from tools.sql_tool import (
    SQLNoPermitido,
    ejecutar_sql,
    formatear_resultado,
    validar_sql,
)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODELO_POR_DEFECTO = os.getenv(
    "OLLAMA_MODEL", "openbmb/minicpm5:latest"
)
TIEMPO_ESPERA = float(os.getenv("OLLAMA_TIMEOUT", "120"))
MAX_TURNOS = 10
MAX_ERRORES_SQL = 3
MAX_SQL_CARACTERES = 1200

PROMPT_SISTEMA = """Eres un asistente académico de análisis de ventas llamado
SQL Sales Assistant. Trabajas con una base de datos SQLite llamada sales.db.

Herramientas disponibles:
- consultar_esquema: muestra tablas, columnas, tipos y claves foráneas.
- ejecutar_sql: ejecuta una consulta SQL de solo lectura.
- exportar_csv: guarda en un archivo CSV el resultado de la última consulta
  ejecutada correctamente.

Relaciones reales para JOIN (úsalas obligatoriamente):
- SALES.customer_id = CUSTOMERS.customer_id
- SALES.product_id = PRODUCTS.product_id
- PRODUCTS.category_id = CATEGORIES.category_id
Los ingresos se calculan como SALES.quantity * PRODUCTS.price.

Reglas obligatorias:
1. Responde siempre en español, de forma breve, clara y basada en datos reales.
2. Nunca inventes cifras ni resultados. Para responder sobre ventas, clientes,
   productos o categorías debes usar ejecutar_sql y leer el resultado.
3. Si no conoces las tablas o columnas, usa primero consultar_esquema.
4. Usa sintaxis compatible con SQLite. Solo se permiten consultas SELECT o
   WITH ... SELECT. No intentes modificar la base de datos.
5. La fecha de venta está en sale_date con formato de texto YYYY-MM-DD.
   Para calcular ingresos usa quantity * price.
6. Si ejecutar_sql devuelve un error, analízalo, corrige la consulta y vuelve
   a intentarlo. Tienes un máximo de 3 intentos.
7. Si el usuario pide exportar, primero ejecuta la consulta con ejecutar_sql y
   después llama a exportar_csv. El CSV se crea con el último resultado
   exitoso. Informa la ruta del archivo en tu respuesta final.
8. No respondas con una consulta SQL en texto como sustituto de ejecutarla:
   usa la herramienta ejecutar_sql.
9. Usa exactamente los nombres de tablas y columnas que aparezcan en el
   esquema.
10. Al usar JOIN, escribe el alias de la tabla delante de cada columna
    (por ejemplo, s.customer_id, p.price) para evitar columnas ambiguas.
    Evita SELECT * cuando haya varias tablas.
11. No menciones archivos CSV ni rutas si el usuario no pidió exportar.
    Si pidió exportar, indica únicamente la ruta que devolvió la herramienta
    exportar_csv; no inventes rutas.
12. No inventes columnas como id, customer_name, product_price o total.
    Usa únicamente las columnas que aparecen en el esquema.
13. Para unir tablas usa las relaciones indicadas arriba y califica cada
    columna con el alias de su tabla (por ejemplo, c.name, p.price).
14. El usuario nunca debe ver una consulta SQL. Toda consulta debe ejecutarse
    con la herramienta ejecutar_sql.
15. No repitas el esquema completo. Si el usuario pregunta qué datos existen,
    responde con un resumen breve de las tablas y lo que contienen.
16. Consulta el esquema como máximo una vez por pregunta. Si ya está en el
    contexto, no vuelvas a llamar a consultar_esquema.
17. La respuesta final debe basarse exclusivamente en las filas devueltas por
    ejecutar_sql. No inventes cifras ni completes datos que no aparezcan.
18. Cuando vayas a usar una herramienta, hazlo de inmediato con una
    explicación muy breve. No escribas razonamientos largos antes de la
    llamada, para que el JSON de argumentos no se corte.
19. No agregues filtros de país, año, cliente, producto o categoría que el
    usuario no haya mencionado.
20. Para "clientes que más compraron" usa SUM(s.quantity), agrupa por cliente
    y ordena de mayor a menor con LIMIT.
21. Para "producto más vendido" usa SUM(s.quantity), agrupa por producto y
    ordena de mayor a menor con LIMIT.
22. Para "promedio de ventas por categoría" usa AVG(s.quantity * p.price) y
    une SALES -> PRODUCTS -> CATEGORIES.
23. Para exportar ventas de un año, selecciona las columnas de SALES y filtra
    por s.sale_date usando ese año.
24. Si la pregunta menciona un año, incluye obligatoriamente
    s.sale_date >= 'AAAA-01-01' AND s.sale_date < 'AAAA+1-01-01'.
25. Si la pregunta no menciona un año, no agregues filtros de fecha.
26. Para una evolución histórica, agrupa por año con
    strftime('%Y', s.sale_date) AS anio y GROUP BY anio.
27. No repitas columnas ni funciones de agregación; calcula cada métrica una
    sola vez.
28. La consulta debe ser simple, tener una sola estructura SELECT principal
    y no superar 1200 caracteres.
29. Para el gasto de un cliente, filtra por c.name = 'Nombre del cliente' y
    usa SUM(s.quantity * p.price).
30. No acumules SQL de intentos anteriores; genera una consulta nueva.
31. Si el usuario pide todos los registros, no uses LIMIT.
32. Si pregunta cuántos registros hay, usa COUNT.
33. Para unidades usa SUM(s.quantity); para dinero usa
    SUM(s.quantity * p.price). No confundas ambas métricas.
34. Si el nombre de un cliente no coincide exactamente, usa el nombre real
    más cercano solo si la coincidencia es segura; si no, pide aclaración.
35. Nunca devuelvas datos de un cliente distinto al solicitado.
36. Para productos nunca vendidos usa PRODUCTS LEFT JOIN SALES y filtra
    WHERE s.sale_id IS NULL.
"""

EJEMPLOS_SQL = """
Patrones correctos de consulta. No copies filtros que la pregunta no pida.

P: ¿Cuáles son los tres clientes que más compraron?
R: SELECT c.customer_id, c.name, SUM(s.quantity) AS total_cantidad
   FROM CUSTOMERS c
   JOIN SALES s ON c.customer_id = s.customer_id
   GROUP BY c.customer_id, c.name
   ORDER BY total_cantidad DESC
   LIMIT 3;

P: ¿Cuál es el producto más vendido?
R: SELECT p.product_id, p.product_name, SUM(s.quantity) AS total_cantidad
   FROM PRODUCTS p
   JOIN SALES s ON p.product_id = s.product_id
   GROUP BY p.product_id, p.product_name
   ORDER BY total_cantidad DESC
   LIMIT 1;

P: ¿Cuál es el ingreso promedio por categoría?
R: SELECT cat.category_id, cat.category_name,
          AVG(s.quantity * p.price) AS promedio_ingreso
   FROM SALES s
   JOIN PRODUCTS p ON s.product_id = p.product_id
   JOIN CATEGORIES cat ON p.category_id = cat.category_id
   GROUP BY cat.category_id, cat.category_name
   ORDER BY promedio_ingreso DESC;

P: Exporta todas las ventas.
R: SELECT s.sale_id, s.customer_id, s.product_id, s.quantity, s.sale_date
   FROM SALES s
   ORDER BY s.sale_date;
"""

FORMATO_SQL = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": "Una única consulta SELECT válida para SQLite.",
        }
    },
    "required": ["sql"],
}

HERRAMIENTAS = [
    {
        "type": "function",
        "function": {
            "name": "consultar_esquema",
            "description": (
                "Devuelve las tablas, columnas, tipos de datos, claves "
                "primarias, claves foráneas y número de filas de la base "
                "de datos. Úsala cuando necesites conocer la estructura."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ejecutar_sql",
            "description": (
                "Ejecuta una consulta SELECT o WITH ... SELECT en SQLite y "
                "devuelve las filas. Si hay un error, devuelve el mensaje "
                "de SQLite para poder corregir la consulta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "Consulta SQL de solo lectura.",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exportar_csv",
            "description": (
                "Exporta a exports/ el resultado de la última consulta "
                "ejecutada correctamente. Devuelve la ruta del archivo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_archivo": {
                        "type": "string",
                        "description": (
                            "Nombre descriptivo opcional para el archivo, "
                            "por ejemplo: ventas_2024."
                        ),
                    }
                },
                "required": [],
            },
        },
    },
]


class ErrorOllama(RuntimeError):
    """Error de comunicación o de configuración con Ollama."""


def verificar_ollama(url=OLLAMA_URL, modelo=MODELO_POR_DEFECTO):
    """Comprueba si Ollama responde y si el modelo está instalado."""
    try:
        respuesta = requests.get(f"{url}/api/tags", timeout=4)
        respuesta.raise_for_status()
        datos = respuesta.json()
    except requests.RequestException as exc:
        return False, (
            f"No se pudo conectar con Ollama en {url}.\n"
            "Inicia Ollama con: ollama serve\n"
            f"Detalle: {exc}"
        )

    disponibles = [item.get("name", "") for item in datos.get("models", [])]
    if modelo not in disponibles:
        lista = ", ".join(disponibles) if disponibles else "ninguno"
        return False, (
            f"Ollama está activo, pero el modelo '{modelo}' no está instalado.\n"
            f"Modelos disponibles: {lista}\n"
            f"Instálalo con: ollama pull {modelo}\n"
            "También puedes usar otro modelo con la variable OLLAMA_MODEL."
        )

    return True, f"Ollama disponible con el modelo {modelo}."


class SalesAgent:
    """Agente conversacional con tool calling sobre la base de ventas."""

    def __init__(self, modelo=None, url=None, tiempo_espera=None):
        self.modelo = modelo or os.getenv("OLLAMA_MODEL", MODELO_POR_DEFECTO)
        self.url = (url or os.getenv("OLLAMA_URL", OLLAMA_URL)).rstrip("/")
        self.tiempo_espera = float(
            tiempo_espera or os.getenv("OLLAMA_TIMEOUT", TIEMPO_ESPERA)
        )
        self.ultimo_resultado = None
        self.ultimo_error_sql = None
        self.ultima_sql = None
        self.errores_sql_consecutivos = 0
        self.limite_alcanzado = False
        self.exportacion_realizada = False
        self.ruta_exportada = None
        self.esquema_consultado = False
        self.consulta_valida_obtenida = False
        self.pregunta_actual = ""
        self.mensajes = []

        # El prompt incluye un esquema compacto y relaciones reales. La
        # herramienta consultar_esquema sigue disponible para el detalle.
        try:
            self.esquema_actual = consultar_esquema()
        except Exception:
            self.esquema_actual = "(El esquema no está disponible)."
        self.columnas_por_tabla = self._extraer_columnas(self.esquema_actual)
        self.nombres_clientes = self._cargar_nombres_clientes()
        self.nombre_cliente_resuelto = None
        self.prompt_sistema = (
            PROMPT_SISTEMA
            + "\n\nEsquema compacto (tabla: columnas):\n"
            + self._esquema_compacto()
        )

    def preguntar(self, pregunta):
        """Procesa una pregunta y devuelve la respuesta final en español."""
        if not isinstance(pregunta, str) or not pregunta.strip():
            return "Por favor, escribe una pregunta válida."

        if self._peticion_peligrosa(pregunta):
            return (
                "No puedo ejecutar operaciones que modifiquen o eliminen "
                "registros. La base de datos es de solo lectura y únicamente "
                "permito consultas SELECT."
            )

        self.pregunta_actual = pregunta.strip()
        self.ultimo_resultado = None
        self.ultimo_error_sql = None
        self.ultima_sql = None
        self.errores_sql_consecutivos = 0
        self.limite_alcanzado = False
        self.exportacion_realizada = False
        self.ruta_exportada = None
        self.esquema_consultado = False
        self.consulta_valida_obtenida = False
        self.nombre_cliente_resuelto = None

        nombre, aclaracion = self._resolver_nombre_cliente(
            self.pregunta_actual
        )
        if aclaracion:
            return aclaracion
        self.nombre_cliente_resuelto = nombre

        self.mensajes = [
            {"role": "system", "content": self.prompt_sistema},
            {"role": "user", "content": pregunta.strip()},
        ]

        # MiniCPM5 es muy limitado con tool calling para SQL complejo. En ese
        # caso se usa generación estructurada de SQL (JSON Schema de Ollama),
        # manteniendo las mismas herramientas y validaciones.
        if self._usar_flujo_directo():
            return self._flujo_directo(pregunta)

        avisos_sin_herramienta = 0
        avisos_exportacion = 0
        avisos_sql_visible = 0
        avisos_datos = 0
        reintentos_json = 0
        pide_exportar = self._pide_exportar(pregunta)
        pregunta_esquema = self._pregunta_sobre_esquema(pregunta)

        for _ in range(MAX_TURNOS):
            try:
                respuesta = self._chat(self.mensajes, HERRAMIENTAS)
            except ErrorOllama as exc:
                detalle = str(exc).lower()
                error_json = (
                    "invalid tool call arguments" in detalle
                    or "unexpected end of json input" in detalle
                )
                if error_json and reintentos_json == 0:
                    reintentos_json += 1
                    self.mensajes.append(
                        {
                            "role": "user",
                            "content": (
                                "La llamada anterior a herramientas no se pudo "
                                "procesar porque sus argumentos JSON estaban "
                                "incompletos. Vuelve a generar la llamada con un "
                                "objeto JSON completo y válido."
                            ),
                        }
                    )
                    continue
                raise

            mensaje = respuesta.get("message") or {}
            tool_calls = mensaje.get("tool_calls") or []
            contenido = (mensaje.get("content") or "").strip()

            if tool_calls:
                llamadas = self._normalizar_llamadas(tool_calls)

                if not llamadas:
                    self.mensajes.append(
                        {"role": "assistant", "content": contenido}
                    )
                    self.mensajes.append(
                        {
                            "role": "user",
                            "content": (
                                "La llamada a herramientas llegó incompleta o "
                                "sin un nombre válido. Genera de nuevo la llamada "
                                "con un objeto JSON completo."
                            ),
                        }
                    )
                    continue

                self.mensajes.append(
                    {
                        "role": "assistant",
                        "content": contenido,
                        "tool_calls": [item["call"] for item in llamadas],
                    }
                )

                for item in llamadas:
                    nombre = item["name"]
                    if item["error"]:
                        texto_resultado = self._error_argumentos(
                            nombre, item["error"]
                        )
                    else:
                        texto_resultado = self._ejecutar_herramienta(
                            nombre, item["arguments"]
                        )

                    self.mensajes.append(
                        {
                            "role": "tool",
                            "content": texto_resultado,
                            "tool_name": nombre,
                        }
                    )

                    if self.limite_alcanzado:
                        return self._respuesta_por_limite()

                # En cuanto existe una consulta válida, se responde con sus
                # datos. Así evitamos que el modelo siga consultando tablas o
                # repitiendo el esquema hasta agotar los pasos.
                if self.consulta_valida_obtenida:
                    if pide_exportar and not self.exportacion_realizada:
                        ruta = self._exportar_ultimo_resultado()
                        if not ruta:
                            return (
                                "La consulta se ejecutó, pero no se pudo "
                                "generar el archivo CSV solicitado."
                            )
                    return self._responder_con_resultado(pregunta)

                continue

            # --- Respuesta después de una consulta SQL exitosa ---
            if self.ultimo_resultado is not None:
                if not contenido:
                    if avisos_datos >= 1:
                        return self._respuesta_con_datos()
                    avisos_datos += 1
                    self.mensajes.append(
                        {
                            "role": "user",
                            "content": (
                                "Responde en español usando exclusivamente los "
                                "resultados reales de ejecutar_sql. No muestres "
                                "la consulta SQL ni repitas el esquema."
                            ),
                        }
                    )
                    continue

                if self._parece_consulta_sql(contenido):
                    if avisos_sql_visible == 0:
                        avisos_sql_visible += 1
                        self.mensajes.append(
                            {"role": "assistant", "content": contenido}
                        )
                        self.mensajes.append(
                            {
                                "role": "user",
                                "content": (
                                    "No muestres la consulta SQL al usuario. "
                                    "Ejecútala ahora con ejecutar_sql y después "
                                    "responde en español usando las filas "
                                    "obtenidas."
                                ),
                            }
                        )
                        continue
                    return self._respuesta_con_datos()

                if pide_exportar and not self.exportacion_realizada:
                    if avisos_exportacion == 0:
                        avisos_exportacion += 1
                        self.mensajes.append(
                            {"role": "assistant", "content": contenido}
                        )
                        self.mensajes.append(
                            {
                                "role": "user",
                                "content": (
                                    "El usuario pidió exportar los resultados. "
                                    "Llama ahora a la herramienta exportar_csv. "
                                    "No inventes una ruta; usa la que devuelva "
                                    "la herramienta."
                                ),
                            }
                        )
                        continue

                    # Red de seguridad: si el modelo no llamó a la
                    # herramienta, el agente exporta el último resultado.
                    ruta = self._exportar_ultimo_resultado()
                    if ruta:
                        return (
                            contenido
                            + "\n\nArchivo CSV generado en: "
                            + ruta
                        )
                    return contenido + (
                        "\n\nNo se pudo generar el archivo CSV solicitado."
                    )

                if (
                    not self.exportacion_realizada
                    and not self._menciona_resultado(
                        contenido, self.ultimo_resultado
                    )
                ):
                    if avisos_datos == 0:
                        avisos_datos += 1
                        self.mensajes.append(
                            {"role": "assistant", "content": contenido}
                        )
                        self.mensajes.append(
                            {
                                "role": "user",
                                "content": (
                                    "Tu respuesta no utiliza los datos reales "
                                    "obtenidos. Responde en español usando "
                                    "exclusivamente las filas del resultado de "
                                    "ejecutar_sql y no repitas el esquema."
                                ),
                            }
                        )
                        continue
                    return self._respuesta_con_datos()

                return contenido

            # --- Respuesta basada solo en el esquema ---
            if contenido and pregunta_esquema and self.esquema_consultado:
                if self._parece_consulta_sql(contenido):
                    if avisos_sql_visible == 0:
                        avisos_sql_visible += 1
                        self.mensajes.append(
                            {"role": "assistant", "content": contenido}
                        )
                        self.mensajes.append(
                            {
                                "role": "user",
                                "content": (
                                    "No muestres SQL. Para preguntas sobre los "
                                    "datos disponibles, resume el esquema en "
                                    "español sin copiarlo completo."
                                ),
                            }
                        )
                        continue
                    return self._resumen_esquema()
                if self._repitio_esquema(contenido):
                    return self._resumen_esquema()
                return contenido

            if not contenido and pregunta_esquema and self.esquema_consultado:
                return self._resumen_esquema()

            # --- Todavía no hay datos reales ---
            if avisos_sin_herramienta >= 2:
                return (
                    "No pude obtener una respuesta basada en datos reales. "
                    "La consulta no llegó a ejecutarse correctamente. "
                    "Puedes reformular la pregunta."
                )

            avisos_sin_herramienta += 1
            if contenido:
                self.mensajes.append(
                    {"role": "assistant", "content": contenido}
                )

            if pregunta_esquema and not self.esquema_consultado:
                instruccion = (
                    "El usuario pregunta por los datos disponibles. Llama una "
                    "sola vez a consultar_esquema y luego responde con un "
                    "resumen breve en español. No copies el esquema completo."
                )
            elif self._parece_consulta_sql(contenido):
                instruccion = (
                    "No muestres la consulta SQL. Ejecútala ahora con "
                    "ejecutar_sql. Si falla, corrige el SQL usando las tablas, "
                    "columnas y relaciones reales, y vuelve a intentarlo."
                )
            else:
                instruccion = (
                    "No respondas todavía ni escribas SQL como texto. Debes usar "
                    "ejecutar_sql para obtener los datos reales. Para JOIN usa "
                    "SALES.customer_id = CUSTOMERS.customer_id, "
                    "SALES.product_id = PRODUCTS.product_id y "
                    "PRODUCTS.category_id = CATEGORIES.category_id. Después "
                    "responde en español usando los resultados."
                )
            self.mensajes.append({"role": "user", "content": instruccion})

        return (
            "No pude completar una respuesta basada en datos reales en el "
            "número máximo de pasos. Intenta reformular la pregunta."
        )

    def _chat(self, mensajes, herramientas, formato=None, max_tokens=None):
        """Llama a POST /api/chat de Ollama."""
        carga = {
            "model": self.modelo,
            "messages": self._preparar_mensajes_para_ollama(mensajes),
            "stream": False,
            # MiniCPM5 usa modo "thinking" por defecto. Al desactivarlo
            # emite tool_calls de forma fiable. Los modelos que no piensan
            # ignoran este parámetro.
            "think": False,
            # El límite debe ser suficientemente amplio para que una tool call
            # no se corte a mitad del JSON. Si se corta, Ollama devuelve
            # "unexpected end of JSON input" al reutilizar el historial.
            "options": {
                "temperature": 0,
                "num_predict": max_tokens or 1024,
            },
        }
        if herramientas:
            carga["tools"] = herramientas
        if formato is not None:
            carga["format"] = formato

        try:
            respuesta = requests.post(
                f"{self.url}/api/chat",
                json=carga,
                timeout=self.tiempo_espera,
            )
        except requests.ConnectionError as exc:
            raise ErrorOllama(
                f"No se pudo conectar con Ollama en {self.url}. "
                "Comprueba que esté iniciado con 'ollama serve'."
            ) from exc
        except requests.Timeout as exc:
            raise ErrorOllama(
                "Ollama tardó demasiado en responder. "
                "Prueba un modelo más pequeño o aumenta OLLAMA_TIMEOUT."
            ) from exc

        if respuesta.status_code >= 400:
            try:
                detalle = respuesta.json().get("error", respuesta.text)
            except ValueError:
                detalle = respuesta.text
            raise ErrorOllama(
                f"Ollama devolvió un error ({respuesta.status_code}): {detalle}"
            )

        try:
            datos = respuesta.json()
        except ValueError as exc:
            raise ErrorOllama("Ollama devolvió una respuesta que no es JSON.") from exc

        if "message" not in datos:
            raise ErrorOllama(
                "La respuesta de Ollama no contiene el campo 'message'."
            )
        return datos

    def _usar_flujo_directo(self):
        return "minicpm5" in (self.modelo or "").lower()

    def _flujo_directo(self, pregunta):
        """
        Flujo compatible con MiniCPM5.

        Este modelo pierde el hilo del tool calling en consultas con JOIN.
        Se usa la salida estructurada de Ollama para generar el SQL, y luego
        se ejecutan las mismas tres herramientas del proyecto.
        """
        if self._pregunta_sobre_esquema(pregunta):
            return self._responder_esquema(pregunta)

        errores = []
        for intento in range(1, MAX_ERRORES_SQL + 1):
            self.errores_sql_consecutivos = intento - 1
            sql_generado = self._generar_sql_estructurado(pregunta, errores)

            if not sql_generado:
                mensaje = "El modelo no devolvió una consulta SQL válida."
                self.ultimo_error_sql = mensaje
                errores.append(f"Intento {intento}: {mensaje}")
                continue

            sql = self._reparar_sql(sql_generado)
            self.ultima_sql = sql
            motivo = self._validar_consulta_para_pregunta(sql)
            if motivo:
                self.ultimo_error_sql = motivo
                errores.append(
                    f"Intento {intento} rechazado: {motivo}. "
                    f"SQL anterior recortado: {sql[:250]}"
                )
                continue

            resultado = ejecutar_sql(sql)
            if resultado.get("ok"):
                if not self._resultado_cliente_correcto(resultado):
                    return (
                        "La consulta no corresponde al cliente solicitado. "
                        "No devolveré datos de otro cliente."
                    )

                self.ultimo_resultado = resultado
                self.ultimo_error_sql = None
                self.errores_sql_consecutivos = 0
                self.consulta_valida_obtenida = True

                if self._pide_exportar(pregunta):
                    ruta = self._exportar_ultimo_resultado()
                    if not ruta:
                        return (
                            "La consulta se ejecutó, pero no se pudo generar "
                            "el archivo CSV solicitado."
                        )
                    return (
                        f"Se exportaron {resultado.get('num_filas', 0)} "
                        "fila(s) a un archivo CSV.\n"
                        f"Archivo generado en: {ruta}"
                    )

                return self._respuesta_con_datos()

            self.ultimo_error_sql = resultado.get(
                "error", "error desconocido"
            )
            errores.append(
                f"Intento {intento} falló: {self.ultimo_error_sql}. "
                + self._sugerencia_error(self.ultimo_error_sql)
                + f" SQL anterior recortado: {sql[:250]}"
            )

        self.limite_alcanzado = True
        self.errores_sql_consecutivos = MAX_ERRORES_SQL
        return self._respuesta_por_limite()

    def _ejemplos_para_pregunta(self, pregunta):
        texto = (pregunta or "").lower()
        anios = re.findall(r"\b(20\d{2})\b", pregunta or "")
        anio = anios[0] if anios else None
        nombre = None
        coincidencia_nombre = re.search(
            r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+)",
            pregunta or "",
        )
        if self.nombre_cliente_resuelto:
            nombre = self.nombre_cliente_resuelto
        elif coincidencia_nombre:
            nombre = coincidencia_nombre.group(1)

        def filtro_fecha(alias="s"):
            if not anio:
                return ""
            siguiente = str(int(anio) + 1)
            return (
                f"{alias}.sale_date >= '{anio}-01-01' AND "
                f"{alias}.sale_date < '{siguiente}-01-01'"
            )

        historico = any(
            palabra in texto
            for palabra in (
                "comportamiento",
                "evolución",
                "evolucion",
                "histórico",
                "historico",
                "por año",
                "por anio",
                "cada año",
                "cada anio",
                "tres años",
                "tres anios",
            )
        )

        if historico and any(
            palabra in texto
            for palabra in ("venta", "dinero", "ingreso", "generó", "genero")
        ):
            return """P: ¿Cuál fue el comportamiento de las ventas durante los tres años disponibles?
R: SELECT strftime('%Y', s.sale_date) AS anio,
          SUM(s.quantity) AS unidades_vendidas,
          SUM(s.quantity * p.price) AS ingresos
   FROM SALES s
   JOIN PRODUCTS p ON s.product_id = p.product_id
   GROUP BY anio
   ORDER BY anio;"""

        if nombre and any(
            palabra in texto
            for palabra in ("gastó", "gasto", "compró", "compro")
        ):
            condicion = f"WHERE c.name = '{nombre}'"
            rango = filtro_fecha("s")
            if rango:
                condicion += " AND " + rango

            if any(
                palabra in texto
                for palabra in ("producto", "productos", "compró", "compro")
            ):
                return f"""P: ¿Cuánto dinero gastó {nombre} durante {anio or 'el período'} y qué productos compró?
R: SELECT c.name AS cliente,
          p.product_name,
          SUM(s.quantity) AS unidades,
          SUM(s.quantity * p.price) AS gasto_producto,
          SUM(SUM(s.quantity * p.price)) OVER () AS gasto_total
   FROM SALES s
   JOIN CUSTOMERS c ON s.customer_id = c.customer_id
   JOIN PRODUCTS p ON s.product_id = p.product_id
   {condicion}
   GROUP BY c.name, p.product_id, p.product_name
   ORDER BY gasto_producto DESC;"""

            return f"""P: ¿Cuánto gastó {nombre} en total?
R: SELECT c.name AS cliente,
          SUM(s.quantity * p.price) AS gasto_total
   FROM SALES s
   JOIN CUSTOMERS c ON s.customer_id = c.customer_id
   JOIN PRODUCTS p ON s.product_id = p.product_id
   {condicion}
   GROUP BY c.name;"""

        if "categoría" in texto or "categoria" in texto:
            rango = filtro_fecha("s")
            where = f"WHERE {rango}" if rango else ""
            if "promedio" in texto:
                return f"""P: ¿Cuál es el ingreso promedio por categoría?
R: SELECT cat.category_id, cat.category_name,
          AVG(s.quantity * p.price) AS promedio_ingreso
   FROM SALES s
   JOIN PRODUCTS p ON s.product_id = p.product_id
   JOIN CATEGORIES cat ON p.category_id = cat.category_id
   {where}
   GROUP BY cat.category_id, cat.category_name
   ORDER BY promedio_ingreso DESC;"""
            return f"""P: ¿Cuánto dinero se generó en ventas por cada categoría durante {anio or 'el período'}?
R: SELECT cat.category_id, cat.category_name,
          SUM(s.quantity * p.price) AS ingresos
   FROM SALES s
   JOIN PRODUCTS p ON s.product_id = p.product_id
   JOIN CATEGORIES cat ON p.category_id = cat.category_id
   {where}
   GROUP BY cat.category_id, cat.category_name
   ORDER BY ingresos DESC;"""

        if "cliente" in texto:
            if any(
                palabra in texto
                for palabra in ("cuántos", "cuantos", "cuántas", "cuantas")
            ) and any(
                palabra in texto
                for palabra in ("cuáles", "cuales", "lista")
            ):
                return """P: ¿Cuántos clientes hay registrados y cuáles son?
R: SELECT COUNT(*) OVER () AS total_clientes,
          customer_id, name, email, country
   FROM CUSTOMERS
   ORDER BY customer_id;"""

            if any(
                palabra in texto
                for palabra in ("todos", "todas", "registrados", "registradas")
            ):
                return """P: Dime todos los clientes registrados.
R: SELECT customer_id, name, email, country
   FROM CUSTOMERS
   ORDER BY customer_id;"""

            rango = filtro_fecha("s")
            where = f"WHERE {rango}" if rango else ""
            return f"""P: ¿Cuáles son los tres clientes que más compraron durante {anio or 'el período'}?
R: SELECT c.customer_id, c.name, SUM(s.quantity) AS total_cantidad
   FROM CUSTOMERS c
   JOIN SALES s ON c.customer_id = s.customer_id
   {where}
   GROUP BY c.customer_id, c.name
   ORDER BY total_cantidad DESC
   LIMIT 3;"""

        if "producto" in texto:
            if any(
                frase in texto
                for frase in (
                    "nunca se vendieron",
                    "nunca se vendió",
                    "nunca se vendio",
                    "no se vendieron",
                    "sin ventas",
                )
            ):
                return """P: ¿Qué productos nunca se vendieron?
R: SELECT p.product_id, p.product_name
   FROM PRODUCTS p
   LEFT JOIN SALES s ON p.product_id = s.product_id
   WHERE s.sale_id IS NULL
   ORDER BY p.product_id;"""

            if any(
                frase in texto
                for frase in ("todos los productos", "todas las productos",
                              "productos registrados", "16 productos",
                              "lista de productos")
            ):
                return """P: Dime cuáles son todos los productos registrados.
R: SELECT product_id, product_name, category_id, price
   FROM PRODUCTS
   ORDER BY product_id;"""

            rango = filtro_fecha("s")
            where = f"WHERE {rango}" if rango else ""
            return f"""P: ¿Cuál es el producto más vendido durante {anio or 'el período'}?
R: SELECT p.product_id, p.product_name, SUM(s.quantity) AS total_cantidad
   FROM PRODUCTS p
   JOIN SALES s ON p.product_id = s.product_id
   {where}
   GROUP BY p.product_id, p.product_name
   ORDER BY total_cantidad DESC
   LIMIT 1;"""

        if self._pide_exportar(pregunta):
            rango = filtro_fecha("s")
            where = f"WHERE {rango}" if rango else ""
            return f"""P: Exporta todas las ventas de {anio or 'el período'}.
R: SELECT s.sale_id, s.customer_id, s.product_id, s.quantity, s.sale_date
   FROM SALES s
   {where}
   ORDER BY s.sale_date;"""

        return EJEMPLOS_SQL

    def _generar_sql_estructurado(self, pregunta, errores):
        sistema = (
            "Eres un generador de SQL para SQLite. Devuelve un objeto JSON "
            "con la clave 'sql'.\n\n"
            "Esquema real:\n"
            + self._esquema_compacto()
            + "\n\nRelaciones obligatorias:\n"
            "- SALES.customer_id = CUSTOMERS.customer_id\n"
            "- SALES.product_id = PRODUCTS.product_id\n"
            "- PRODUCTS.category_id = CATEGORIES.category_id\n\n"
            "Reglas:\n"
            "- Genera una sola consulta SELECT o WITH ... SELECT.\n"
            "- Usa únicamente las tablas y columnas del esquema.\n"
            "- No inventes columnas, alias ni relaciones.\n"
            "- No añadas filtros de año, país, cliente, producto o categoría "
            "que la pregunta no mencione.\n"
            "- Para cantidades usa SUM(s.quantity).\n"
            "- Para ingresos usa SUM(s.quantity * p.price).\n"
            "- Para promedios usa AVG(...).\n"
            "- Para rankings usa ORDER BY ... DESC y LIMIT.\n\n"
            + self._ejemplos_para_pregunta(pregunta)
        )

        if errores:
            sistema += (
                "\n\nError del intento anterior. Corrige exactamente este "
                "problema y genera una consulta nueva y limpia:\n"
                + errores[-1]
            )

        instruccion_nombre = ""
        if self.nombre_cliente_resuelto:
            instruccion_nombre = (
                "\nEl nombre real del cliente es "
                f"'{self.nombre_cliente_resuelto}'. Usa exactamente "
                f"c.name = '{self.nombre_cliente_resuelto}'."
            )

        mensajes = [
            {"role": "system", "content": sistema},
            {
                "role": "user",
                "content": (
                    f"Pregunta: {pregunta}\n"
                    "Genera la consulta SQL que responde exactamente a la "
                    "pregunta." + instruccion_nombre
                ),
            },
        ]

        respuesta = self._chat(
            mensajes, [], formato=FORMATO_SQL, max_tokens=600
        )
        contenido = (respuesta.get("message", {}).get("content") or "").strip()
        if not contenido:
            return None

        try:
            datos = json.loads(contenido)
        except json.JSONDecodeError:
            return self._extraer_sql(contenido)

        if isinstance(datos, dict):
            sql = datos.get("sql")
            if isinstance(sql, str) and sql.strip():
                return sql.strip()

        return self._extraer_sql(contenido)

    @staticmethod
    def _extraer_sql(texto):
        if not texto:
            return None

        bloques = re.findall(
            r"```(?:sql)?\s*(.*?)```", texto, re.IGNORECASE | re.DOTALL
        )
        if bloques:
            texto = bloques[0]

        coincidencia = re.search(
            r"(?is)\b(select|with)\b.*", texto
        )
        if not coincidencia:
            return None

        sql = coincidencia.group(0).strip()
        if ";" in sql:
            sql = sql.split(";", 1)[0].strip() + ";"
        return sql

    def _responder_esquema(self, pregunta):
        try:
            esquema = consultar_esquema()
        except Exception as exc:
            return f"No pude consultar el esquema: {exc}"

        self.esquema_actual = esquema
        self.columnas_por_tabla = self._extraer_columnas(esquema)
        self.esquema_consultado = True

        tabla = self._tabla_mencionada(pregunta)
        if tabla:
            seccion = self._seccion_tabla(esquema, tabla)
            if seccion:
                return seccion

        mensajes = [
            {
                "role": "system",
                "content": (
                    "Responde en español, de forma breve, a la pregunta del "
                    "usuario sobre el esquema. No copies el esquema completo."
                ),
            },
            {
                "role": "user",
                "content": f"Pregunta: {pregunta}\n\nEsquema:\n{esquema}",
            },
        ]
        self.mensajes = mensajes

        try:
            respuesta = self._chat(mensajes, [], max_tokens=300)
            contenido = (respuesta.get("message", {}).get("content") or "").strip()
        except Exception:
            contenido = ""

        if not contenido or self._repitio_esquema(contenido):
            return self._resumen_esquema()
        return contenido

    @staticmethod
    def _tabla_mencionada(pregunta):
        texto = (pregunta or "").lower()
        mapa = (
            ("CUSTOMERS", ("customer", "clientes", "cliente")),
            ("CATEGORIES", ("categoría", "categoria", "categories")),
            ("PRODUCTS", ("producto", "productos", "products")),
            ("SALES", ("venta", "ventas", "sales")),
        )
        encontradas = []
        for tabla, palabras in mapa:
            if any(palabra in texto for palabra in palabras):
                encontradas.append(tabla)
        return encontradas[0] if len(set(encontradas)) == 1 else None

    @staticmethod
    def _seccion_tabla(esquema, tabla):
        lineas = esquema.splitlines()
        resultado = []
        dentro = False
        for linea in lineas:
            if linea.strip().startswith("Tabla: "):
                nombre = (
                    linea.strip()[len("Tabla: "):]
                    .split("(", 1)[0]
                    .strip()
                    .upper()
                )
                if nombre == tabla:
                    dentro = True
                elif dentro:
                    break
            if dentro:
                resultado.append(linea)

        if not resultado:
            return None
        return "Esquema de la tabla " + tabla + ":\n" + "\n".join(resultado)

    @staticmethod
    def _pide_exportar(pregunta):
        texto = (pregunta or "").lower()
        palabras = (
            "exporta",
            "exportar",
            "exportación",
            "exportacion",
            "csv",
        )
        return any(palabra in texto for palabra in palabras)

    def _exportar_ultimo_resultado(self):
        if not self.ultimo_resultado:
            return None
        try:
            ruta = exportar_csv(self.ultimo_resultado, "resultado_ventas")
        except Exception:
            return None
        self.exportacion_realizada = True
        self.ruta_exportada = ruta
        return ruta

    @staticmethod
    def _extraer_columnas(esquema):
        tablas = {}
        actual = None
        seccion = None
        for linea in esquema.splitlines():
            linea = linea.strip()
            if linea.startswith("Tabla: "):
                actual = linea[len("Tabla: "):].split("(", 1)[0].strip().upper()
                tablas[actual] = []
                seccion = "columnas"
            elif linea.startswith("Columnas:"):
                seccion = "columnas"
            elif linea.startswith("Claves foráneas:"):
                seccion = "foraneas"
            elif (
                actual
                and seccion == "columnas"
                and linea.startswith("- ")
            ):
                columna = linea[2:].split(":", 1)[0].strip().lower()
                if columna:
                    tablas[actual].append(columna)
        return tablas

    def _esquema_compacto(self):
        if not self.columnas_por_tabla:
            return "(esquema no disponible)"
        return "\n".join(
            f"- {tabla}({', '.join(columnas)})"
            for tabla, columnas in self.columnas_por_tabla.items()
        )

    def _resultado_cliente_correcto(self, resultado):
        if not self.nombre_cliente_resuelto:
            return True

        columnas = [
            str(columna).lower()
            for columna in resultado.get("columnas", [])
        ]
        indice = None
        for nombre_columna in ("cliente", "customer_name", "name"):
            if nombre_columna in columnas:
                indice = columnas.index(nombre_columna)
                break

        if indice is None:
            return True

        esperado = self._sin_acentos(self.nombre_cliente_resuelto).lower()
        for fila in resultado.get("filas", []):
            if fila[indice] is None:
                continue
            if self._sin_acentos(str(fila[indice])).lower() != esperado:
                return False
        return True

    def _cargar_nombres_clientes(self):
        resultado = ejecutar_sql(
            "SELECT name FROM CUSTOMERS ORDER BY customer_id"
        )
        if not resultado.get("ok"):
            return []
        return [
            str(fila[0])
            for fila in resultado.get("filas", [])
            if fila and fila[0]
        ]

    def _resolver_nombre_cliente(self, pregunta):
        coincidencia = re.search(
            r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+"
            r"(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+)",
            pregunta or "",
        )
        if not coincidencia or not self.nombres_clientes:
            return None, None

        candidato = coincidencia.group(1)
        normalizado = self._sin_acentos(candidato).lower()

        for nombre in self.nombres_clientes:
            if self._sin_acentos(nombre).lower() == normalizado:
                return nombre, None

        ratios = []
        for nombre in self.nombres_clientes:
            ratio = difflib.SequenceMatcher(
                None,
                normalizado,
                self._sin_acentos(nombre).lower(),
            ).ratio()
            ratios.append((ratio, nombre))
        ratios.sort(reverse=True)

        mejor_ratio, mejor_nombre = ratios[0]
        segundo_ratio = ratios[1][0] if len(ratios) > 1 else 0.0
        contexto_cliente = any(
            palabra in (pregunta or "").lower()
            for palabra in (
                "cliente",
                "gastó",
                "gasto",
                "compró",
                "compro",
                "compra",
            )
        )

        if mejor_ratio >= 0.82 and (mejor_ratio - segundo_ratio) >= 0.06:
            return mejor_nombre, None

        if mejor_ratio >= 0.82:
            return None, (
                "No pude identificar con seguridad a ese cliente. "
                "Escribe el nombre completo y correcto."
            )

        if contexto_cliente:
            return None, (
                f"No encontré un cliente con el nombre '{candidato}'. "
                "Escribe el nombre completo y correcto."
            )

        return None, None

    @staticmethod
    def _peticion_peligrosa(pregunta):
        texto = (pregunta or "").lower()
        patron = re.compile(
            r"\b(elimina|eliminar|elimines|borra|borrar|borres|actualiza|"
            r"actualizar|actualices|modifica|modificar|modifiques|inserta|"
            r"insertar|insertes|crea|crear|crees|drop|delete|update|insert|"
            r"alter|truncate)\b"
        )
        return bool(patron.search(texto))

    @staticmethod
    def _tiene_filtro_anio(sql, anio):
        texto = re.sub(r"\s+", " ", sql.lower())
        siguiente = int(anio) + 1
        inicio = f"{anio}-01-01"
        fin = f"{siguiente}-01-01"
        rango = (
            "sale_date" in texto
            and f">= '{inicio}'" in texto
            and f"< '{fin}'" in texto
        )
        like = f"like '{anio}%" in texto
        return rango or like

    def _validar_consulta_para_pregunta(self, sql):
        pregunta = (self.pregunta_actual or "").lower()
        if not pregunta or self._pregunta_sobre_esquema(pregunta):
            return None

        try:
            validar_sql(sql)
        except SQLNoPermitido as exc:
            return str(exc)

        problemas = []
        if len(sql) > MAX_SQL_CARACTERES:
            problemas.append(
                f"La consulta tiene {len(sql)} caracteres y supera el máximo "
                f"de {MAX_SQL_CARACTERES}. Genera una consulta más sencilla."
            )

        error_repetencias = self._validar_repetencias_sql(sql)
        if error_repetencias:
            problemas.append(error_repetencias)

        error_estructura = self._validar_estructura_sql(sql)
        if error_estructura:
            problemas.append(error_estructura)

        sql_lower = sql.lower()
        anios_pregunta = re.findall(r"\b(20\d{2})\b", pregunta)

        if anios_pregunta:
            faltantes = [
                anio
                for anio in sorted(set(anios_pregunta))
                if not self._tiene_filtro_anio(sql, anio)
            ]
            for anio in faltantes:
                siguiente = int(anio) + 1
                problemas.append(
                    f"La pregunta menciona el año {anio} pero falta el filtro "
                    f"s.sale_date >= '{anio}-01-01' AND "
                    f"s.sale_date < '{siguiente}-01-01'."
                )
        elif re.search(
            r"(?is)sale_date\s*(?:>=|<=|>|<|=|like)\s*'?20\d{2}",
            sql,
        ):
            problemas.append(
                "La pregunta no menciona ningún año, pero la consulta filtra "
                "por fecha; elimina ese filtro."
            )

        if "promedio" in pregunta:
            if not (
                re.search(r"\bavg\s*\(", sql_lower)
                or ("sum(" in sql_lower and "/" in sql_lower)
            ):
                problemas.append(
                    "La pregunta pide un promedio; usa AVG(...)."
                )

        ranking = any(
            frase in pregunta
            for frase in (
                "más compraron",
                "mas compraron",
                "compraron más",
                "compraron mas",
                "más vendido",
                "mas vendido",
                "más vendidos",
                "mas vendidos",
                "vendido más",
                "vendido mas",
                "top ",
                "mayor cantidad",
            )
        )
        if ranking:
            if not re.search(r"\bsum\s*\(", sql_lower):
                problemas.append(
                    "Para un ranking de cantidades usa SUM(s.quantity)."
                )
            if "order by" not in sql_lower:
                problemas.append(
                    "Para un ranking usa ORDER BY ... DESC."
                )
            if "limit" not in sql_lower:
                problemas.append(
                    "Para un ranking usa LIMIT con el número solicitado."
                )

        if "categoría" in pregunta or "categoria" in pregunta:
            if "categor" not in sql_lower:
                problemas.append(
                    "La pregunta menciona categorías; usa la tabla CATEGORIES."
                )
            if "products" not in sql_lower:
                problemas.append(
                    "Para llegar de SALES a CATEGORIES debes unir con PRODUCTS "
                    "(s.product_id = p.product_id y "
                    "p.category_id = cat.category_id)."
                )

        if "cliente" in pregunta and "customers" not in sql_lower:
            problemas.append(
                "La pregunta menciona clientes; debes unir SALES con CUSTOMERS "
                "y usar c.name."
            )

        if "producto" in pregunta and "products" not in sql_lower:
            problemas.append(
                "La pregunta menciona productos; debes unir SALES con PRODUCTS."
            )

        if (
            any(
                palabra in pregunta
                for palabra in ("cuántos", "cuantos", "cuántas", "cuantas")
            )
            and "count(" not in sql_lower
        ):
            problemas.append("La pregunta pide una cantidad; usa COUNT(...).")

        if any(
            frase in pregunta
            for frase in (
                "cuánto dinero",
                "cuanto dinero",
                "ingresos",
                "ingreso",
                "generó",
                "genero",
                "gastó",
                "gasto",
            )
        ) and not self._pide_exportar(pregunta):
            if "sum(" not in sql_lower:
                problemas.append(
                    "Para calcular dinero usa SUM(s.quantity * p.price)."
                )

        if self.nombre_cliente_resuelto:
            if self.nombre_cliente_resuelto.lower() not in sql_lower:
                problemas.append(
                    "La pregunta menciona a "
                    f"{self.nombre_cliente_resuelto}; filtra por "
                    f"c.name = '{self.nombre_cliente_resuelto}'."
                )
        else:
            nombres = re.findall(
                r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+"
                r"(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+)",
                self.pregunta_actual or "",
            )
            for nombre in nombres:
                if nombre.lower() not in sql_lower:
                    problemas.append(
                        f"La pregunta menciona a {nombre}; filtra por "
                        f"c.name = '{nombre}'."
                    )

        if "producto" in pregunta and any(
            palabra in pregunta
            for palabra in ("compró", "compro", "gastó", "gasto")
        ):
            if "product_name" not in sql_lower:
                problemas.append(
                    "Incluye p.product_name para listar los productos."
                )

        if (
            "por categoría" in pregunta
            or "por categoria" in pregunta
            or "cada categoría" in pregunta
            or "cada categoria" in pregunta
        ) and "group by" not in sql_lower:
            problemas.append(
                "Agrupa por categoría con GROUP BY cat.category_id, "
                "cat.category_name."
            )

        historico = any(
            palabra in pregunta
            for palabra in (
                "comportamiento",
                "evolución",
                "evolucion",
                "histórico",
                "historico",
                "por año",
                "por anio",
                "cada año",
                "cada anio",
                "tres años",
                "tres anios",
            )
        )
        if historico and any(
            palabra in pregunta
            for palabra in ("venta", "dinero", "ingreso", "generó", "genero")
        ):
            if not re.search(
                r"(?is)strftime\s*\(\s*'%y'\s*,\s*(?:s\.)?sale_date\s*\)",
                sql,
            ) and not re.search(
                r"(?is)substr\s*\(\s*(?:s\.)?sale_date\s*,\s*1\s*,\s*4\s*\)",
                sql,
            ):
                problemas.append(
                    "Para la evolución histórica agrupa por año con "
                    "strftime('%Y', s.sale_date) AS anio."
                )
            if "group by" not in sql_lower:
                problemas.append(
                    "Incluye GROUP BY anio para la evolución histórica."
                )
            if "sum(" not in sql_lower:
                problemas.append(
                    "Calcula las métricas históricas con SUM(...)."
                )

        pide_todos = any(
            frase in pregunta
            for frase in (
                "todos los",
                "todas las",
                "todos",
                "todas",
                "registrados",
                "registradas",
                "lista todos",
                "lista todas",
            )
        )
        if pide_todos and "limit" in sql_lower:
            problemas.append(
                "El usuario pidió todos los registros; elimina el LIMIT "
                "para no truncar los resultados."
            )

        if any(
            frase in pregunta
            for frase in ("cuántas unidades", "cuantas unidades", "unidades")
        ):
            if not re.search(r"sum\s*\(\s*s\.quantity\s*\)", sql_lower):
                problemas.append(
                    "La pregunta pide unidades; usa SUM(s.quantity) y no "
                    "cantidades monetarias."
                )

        if any(
            frase in pregunta
            for frase in (
                "nunca se vendieron",
                "nunca se vendió",
                "nunca se vendio",
                "no se vendieron",
                "sin ventas",
            )
        ):
            if "left join" not in sql_lower or "sale_id is null" not in sql_lower:
                problemas.append(
                    "Para productos nunca vendidos usa PRODUCTS LEFT JOIN "
                    "SALES y filtra WHERE s.sale_id IS NULL."
                )

        if "select *" in sql_lower and not self._pide_exportar(pregunta):
            if any(
                palabra in pregunta
                for palabra in ("más", "mas", "promedio", "total", "cuánt", "cuant")
            ):
                problemas.append(
                    "No uses SELECT * para una pregunta analítica; selecciona "
                    "columnas y usa SUM/AVG/COUNT."
                )

        return " ".join(problemas) if problemas else None

    @staticmethod
    def _validar_repetencias_sql(sql):
        coincidencia = re.search(
            r"(?is)\bselect\b(.*?)\bfrom\b", sql
        )
        if coincidencia:
            lista = coincidencia.group(1)
            partes = []
            actual = ""
            nivel = 0
            for caracter in lista:
                if caracter == "(":
                    nivel += 1
                elif caracter == ")":
                    nivel = max(0, nivel - 1)

                if caracter == "," and nivel == 0:
                    partes.append(actual)
                    actual = ""
                else:
                    actual += caracter
            if actual.strip():
                partes.append(actual)

            normalizadas = [
                re.sub(r"\s+", " ", parte).strip().lower()
                for parte in partes
                if parte.strip()
            ]
            if len(normalizadas) != len(set(normalizadas)):
                return (
                    "La consulta repite columnas o expresiones en el SELECT. "
                    "Elimina los duplicados."
                )

        agregados = re.findall(
            r"(?is)\b(sum|avg|count|min|max)\s*\([^()]*\)", sql
        )
        normalizados = [
            re.sub(r"\s+", " ", agregado).strip().lower()
            for agregado in agregados
        ]
        if normalizados:
            maximo = max(
                normalizados.count(agregado)
                for agregado in set(normalizados)
            )
            if maximo > 3:
                return (
                    "La consulta repite demasiadas veces la misma función de "
                    "agregación. Calcula cada métrica una sola vez."
                )

        if sql.upper().count("SELECT") > 4:
            return (
                "La consulta repite demasiadas subconsultas. Genera una "
                "consulta más sencilla."
            )

        return None

    def _validar_relaciones_sql(self, sql, alias_map):
        relaciones = {
            ("SALES", "customer_id", "CUSTOMERS", "customer_id"),
            ("SALES", "product_id", "PRODUCTS", "product_id"),
            ("PRODUCTS", "category_id", "CATEGORIES", "category_id"),
        }

        def permitida(tabla1, columna1, tabla2, columna2):
            return (
                (tabla1, columna1, tabla2, columna2) in relaciones
                or (tabla2, columna2, tabla1, columna1) in relaciones
            )

        patron_join = re.compile(
            r"(?is)\bjoin\s+([A-Za-z_]\w*)\s*(?:as\s+)?"
            r"(?:(?!(?:on|where|group|order|limit|inner|left|right|full|"
            r"cross|union|having|and|or|using|natural|offset)\b)"
            r"([A-Za-z_]\w*))?\s+on\s+(.*?)"
            r"(?=\b(?:inner|left|right|full|cross)?\s*join\b|\bwhere\b|"
            r"\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)"
        )
        tablas = set(self.columnas_por_tabla)

        for coincidencia in patron_join.finditer(sql):
            tabla_join = coincidencia.group(1).upper()
            if tabla_join not in tablas:
                continue

            alias_join = coincidencia.group(2) or tabla_join
            alias_join = alias_join.upper()
            condicion = coincidencia.group(3)

            igualdades = re.findall(
                r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*"
                r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b",
                condicion,
            )
            if not igualdades:
                return (
                    f"El JOIN con {tabla_join} debe usar una relación real "
                    "en la cláusula ON."
                )

            relacion_valida = False
            for alias1, columna1, alias2, columna2 in igualdades:
                tabla1 = alias_map.get(alias1.upper())
                tabla2 = alias_map.get(alias2.upper())
                if not tabla1 or not tabla2:
                    continue
                if not permitida(
                    tabla1, columna1.lower(), tabla2, columna2.lower()
                ):
                    return (
                        f"JOIN inválido: {alias1}.{columna1} = "
                        f"{alias2}.{columna2}. Usa solo estas relaciones: "
                        "SALES.customer_id = CUSTOMERS.customer_id, "
                        "SALES.product_id = PRODUCTS.product_id, "
                        "PRODUCTS.category_id = CATEGORIES.category_id."
                    )
                if alias_join in {alias1.upper(), alias2.upper()}:
                    relacion_valida = True

            if not relacion_valida:
                return (
                    f"El JOIN con {tabla_join} no usa una relación que "
                    "involucre esa tabla."
                )

        return None

    def _mapa_alias(self, sql):
        tablas = set(self.columnas_por_tabla)
        palabras_clave = {
            "where",
            "group",
            "order",
            "limit",
            "on",
            "join",
            "inner",
            "left",
            "right",
            "full",
            "cross",
            "union",
            "having",
            "and",
            "or",
            "using",
            "natural",
            "offset",
        }
        alias_map = {}
        patron = re.compile(
            r"(?is)\b(from|join)\s+([A-Za-z_]\w*)\s*(?:as\s+)?"
            r"(?:(?!(?:where|group|order|limit|on|join|inner|left|right|full|"
            r"cross|union|having|and|or|using|natural|offset)\b)"
            r"([A-Za-z_]\w*))?"
        )
        for coincidencia in patron.finditer(sql):
            tabla = coincidencia.group(2).upper()
            if tabla not in tablas:
                continue
            alias = coincidencia.group(3)
            if not alias or alias.lower() in palabras_clave:
                alias = tabla
            alias_map[alias.upper()] = tabla
        return alias_map

    @staticmethod
    def _pregunta_es_historica(pregunta):
        texto = (pregunta or "").lower()
        marcas = (
            "comportamiento",
            "evolución",
            "evolucion",
            "histórico",
            "historico",
            "por año",
            "por anio",
            "cada año",
            "cada anio",
            "tres años",
            "tres anios",
        )
        return any(marca in texto for marca in marcas) and any(
            palabra in texto
            for palabra in ("venta", "dinero", "ingreso", "generó", "genero")
        )

    @staticmethod
    def _quitar_filtro_fecha(sql):
        """Elimina un filtro de fecha cuando se piden todos los años."""
        patron_dos = re.compile(
            r"(?is)\s+WHERE\s+\(?\s*(?:s\.)?sale_date\s*"
            r"(?:>=|<=|>|<|=|LIKE)\s*'[^']*'\s+(?:AND|OR)\s+"
            r"(?:s\.)?sale_date\s*(?:>=|<=|>|<|=|LIKE)\s*'[^']*'\s*\)?"
            r"\s*(?=GROUP\s+BY|ORDER\s+BY|LIMIT|$)"
        )
        patron_uno = re.compile(
            r"(?is)\s+WHERE\s+\(?\s*(?:s\.)?sale_date\s*"
            r"(?:>=|<=|>|<|=|LIKE)\s*'[^']*'\s*\)?"
            r"\s*(?=GROUP\s+BY|ORDER\s+BY|LIMIT|$)"
        )
        return patron_uno.sub(" ", patron_dos.sub(" ", sql))

    @staticmethod
    def _deduplicar_clausulas(sql):
        patron = re.compile(
            r"(?is)\b(group\s+by|order\s+by)\s+(.*?)"
            r"(?=\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)"
        )

        def reemplazo(coincidencia):
            clausula = coincidencia.group(1)
            contenido = coincidencia.group(2)
            termina_punto_coma = contenido.strip().endswith(";")
            elementos = [
                elemento.strip().rstrip(";").strip()
                for elemento in contenido.split(",")
                if elemento.strip().rstrip(";").strip()
            ]
            vistos = set()
            unicos = []
            for elemento in elementos:
                clave = re.sub(r"\s+", " ", elemento).lower()
                if clave not in vistos:
                    vistos.add(clave)
                    unicos.append(elemento)
            resultado = clausula + " " + ", ".join(unicos)
            if termina_punto_coma:
                resultado += ";"
            return resultado + " "

        return patron.sub(reemplazo, sql)

    @staticmethod
    def _aplicar_fuera_de_cadenas(texto, patron, reemplazo):
        """Aplica una sustitución solo fuera de los literales de texto."""
        partes = re.split(r"('(?:''|[^'])*')", texto)
        for indice in range(0, len(partes), 2):
            partes[indice] = re.sub(
                patron, reemplazo, partes[indice], flags=re.IGNORECASE
            )
        return "".join(partes)

    def _reparar_sql(self, sql):
        """Corrige errores habituales del modelo antes de ejecutar el SQL."""
        texto = sql

        reemplazos = (
            (r"\bCLIENTES\b", "CUSTOMERS"),
            (r"\bCLIENTS\b", "CUSTOMERS"),
            (r"\bVENTAS\b", "SALES"),
            (r"\bPRODUCTOS\b", "PRODUCTS"),
            (r"\bCATEGORIAS\b", "CATEGORIES"),
            (r"\bcustomer_name\b", "name"),
            (r"\bnombre_completo\b", "name"),
            (r"\bfull_name\b", "name"),
            (r"\bnombre_cliente\b", "name"),
            (r"\bproduct_price\b", "price"),
            (r"\bprecio\b", "price"),
            (r"\bcantidad\b", "quantity"),
            (r"\bfecha_venta\b", "sale_date"),
            (r"\bcliente_id\b", "customer_id"),
            (r"\bproducto_id\b", "product_id"),
            (r"\bcategoria_id\b", "category_id"),
        )
        for patron, reemplazo in reemplazos:
            texto = self._aplicar_fuera_de_cadenas(
                texto, patron, reemplazo
            )

        if (
            self._pregunta_es_historica(self.pregunta_actual)
            and not re.search(r"\b20\d{2}\b", self.pregunta_actual or "")
        ):
            texto = self._quitar_filtro_fecha(texto)

        texto = self._deduplicar_clausulas(texto)

        alias_map = self._mapa_alias(texto)
        claves = {
            "CUSTOMERS": "customer_id",
            "SALES": "sale_id",
            "PRODUCTS": "product_id",
            "CATEGORIES": "category_id",
        }

        for alias, tabla in alias_map.items():
            clave = claves.get(tabla)
            if not clave:
                continue
            texto = self._aplicar_fuera_de_cadenas(
                texto,
                rf"\b{re.escape(alias)}\.id\b",
                f"{alias}.{clave}",
            )

        tablas_unicas = set(alias_map.values())
        if len(tablas_unicas) == 1:
            tabla = next(iter(tablas_unicas))
            clave = claves.get(tabla)
            if clave:
                texto = self._aplicar_fuera_de_cadenas(
                    texto, r"\bid\b", clave
                )

        return texto.strip()

    def _validar_estructura_sql(self, sql):
        if not self.columnas_por_tabla:
            return None

        # Ignora literales de texto para no confundir su contenido con
        # nombres de tablas o columnas.
        sql_analisis = re.sub(r"'(?:''|[^'])*'", "''", sql)
        tablas = set(self.columnas_por_tabla)
        ctes = {
            coincidencia.group(1).upper()
            for coincidencia in re.finditer(
                r"(?i)(?:\bwith|,)\s*([A-Za-z_]\w*)\s+as\s*\(", sql_analisis
            )
        }
        palabras_clave = {
            "where",
            "group",
            "order",
            "limit",
            "on",
            "join",
            "inner",
            "left",
            "right",
            "full",
            "cross",
            "union",
            "having",
            "and",
            "or",
            "using",
            "natural",
            "offset",
        }
        alias_map = {}
        patron = re.compile(
            r"(?is)\b(from|join)\s+([A-Za-z_]\w*)\s*(?:as\s+)?"
            r"(?:(?!(?:where|group|order|limit|on|join|inner|left|right|full|"
            r"cross|union|having|and|or|using|natural|offset)\b)"
            r"([A-Za-z_]\w*))?"
        )

        for coincidencia in patron.finditer(sql_analisis):
            tabla_original = coincidencia.group(2)
            tabla = tabla_original.upper()
            alias = coincidencia.group(3)

            if tabla not in tablas:
                if tabla in ctes:
                    continue
                return (
                    f"La tabla {tabla_original} no existe. Usa solo CUSTOMERS, "
                    "CATEGORIES, PRODUCTS o SALES."
                )

            if not alias or alias.lower() in palabras_clave:
                alias = tabla
            alias = alias.upper()

            if alias in alias_map:
                return (
                    f"El alias {alias} está repetido para {tabla}. Usa un "
                    "alias único para cada tabla del JOIN."
                )
            alias_map[alias] = tabla

        for tabla in tablas:
            alias_map.setdefault(tabla, tabla)

        for coincidencia in re.finditer(
            r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", sql_analisis
        ):
            alias = coincidencia.group(1).upper()
            columna = coincidencia.group(2).lower()
            if alias not in alias_map:
                continue
            tabla = alias_map[alias]
            if tabla not in tablas:
                continue
            if columna not in self.columnas_por_tabla[tabla]:
                if columna == "category_id" and tabla == "SALES":
                    return (
                        "SALES no tiene category_id. La categoría se obtiene "
                        "con PRODUCTS.category_id; une SALES con PRODUCTS por "
                        "product_id."
                    )
                if columna == "id":
                    return (
                        f"{tabla} no tiene id. Usa customer_id, product_id, "
                        "category_id o sale_id según corresponda."
                    )
                if columna == "product_price":
                    return "PRODUCTS no tiene product_price; usa price."
                disponibles = ", ".join(
                    sorted(self.columnas_por_tabla[tabla])
                )
                return (
                    f"{tabla} no tiene la columna {columna}. Columnas "
                    f"disponibles: {disponibles}."
                )

        return self._validar_relaciones_sql(sql_analisis, alias_map)

    def _responder_con_resultado(self, pregunta):
        if not self.ultimo_resultado:
            return "No hay datos reales disponibles para responder."

        if self.ruta_exportada:
            return (
                f"Se exportaron {self.ultimo_resultado.get('num_filas', 0)} "
                "fila(s) a un archivo CSV.\n"
                f"Archivo generado en: {self.ruta_exportada}"
            )

        # Para una sola fila conviene una respuesta determinista y exacta.
        if len(self.ultimo_resultado.get("filas", [])) == 1:
            return self._respuesta_con_datos()

        datos_texto = formatear_resultado(self.ultimo_resultado)
        mensajes = [
            {
                "role": "system",
                "content": (
                    "Eres un asistente de ventas. Responde en español, de "
                    "forma breve, usando exclusivamente los datos reales "
                    "proporcionados. No muestres SQL ni el esquema. No "
                    "inventes cifras."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Pregunta: {pregunta}\n\nDatos reales obtenidos:\n"
                    f"{datos_texto}\n\nResponde a la pregunta usando estos "
                    "datos."
                ),
            },
        ]

        try:
            respuesta = self._chat(mensajes, [], max_tokens=300)
            contenido = (respuesta.get("message", {}).get("content") or "").strip()
        except Exception:
            contenido = ""

        if (
            not contenido
            or len(contenido) > 800
            or contenido.lstrip().startswith(("{", "[", "```"))
            or self._parece_consulta_sql(contenido)
            or not self._menciona_resultado(contenido, self.ultimo_resultado)
            or not self._numeros_coherentes(contenido, self.ultimo_resultado)
        ):
            contenido = self._respuesta_con_datos()

        return contenido

    @staticmethod
    def _parsear_argumentos(argumentos):
        """Convierte los argumentos de una tool call en un diccionario seguro."""
        if argumentos is None:
            return {}, None

        if isinstance(argumentos, dict):
            try:
                json.dumps(argumentos)
            except (TypeError, ValueError):
                return {}, "Los argumentos no se pueden convertir a JSON."
            return argumentos, None

        if isinstance(argumentos, str):
            texto = argumentos.strip()
            if not texto:
                return {}, None
            try:
                valor = json.loads(texto)
            except json.JSONDecodeError as exc:
                return {}, (
                    "El JSON de argumentos está incompleto o malformado "
                    f"({exc.msg}, posición {exc.pos})."
                )
            if not isinstance(valor, dict):
                return {}, "Los argumentos deben ser un objeto JSON."
            return valor, None

        return {}, "El formato de los argumentos no es válido."

    @staticmethod
    def _normalizar_argumentos(argumentos):
        """Compatibilidad: devuelve solo el diccionario de argumentos."""
        valor, _ = SalesAgent._parsear_argumentos(argumentos)
        return valor

    def _normalizar_llamadas(self, tool_calls):
        """Limpia las tool calls para no reenviar JSON incompleto a Ollama."""
        normalizadas = []
        if not isinstance(tool_calls, list):
            return normalizadas

        for llamada in tool_calls:
            if not isinstance(llamada, dict):
                continue

            funcion = llamada.get("function")
            if isinstance(funcion, str):
                try:
                    funcion = json.loads(funcion)
                except (json.JSONDecodeError, TypeError):
                    funcion = {}
            if not isinstance(funcion, dict):
                funcion = {}

            nombre = funcion.get("name")
            if not isinstance(nombre, str) or not nombre.strip():
                continue
            nombre = nombre.strip()

            argumentos, error = self._parsear_argumentos(
                funcion.get("arguments")
            )
            if error is None and nombre.lower() == "ejecutar_sql":
                sql = argumentos.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    error = (
                        "Falta el parámetro obligatorio 'sql' o no es una "
                        "cadena de texto."
                    )
            llamada_limpia = {
                "function": {
                    "name": nombre,
                    "arguments": argumentos if error is None else {},
                }
            }
            identificador = llamada.get("id")
            if isinstance(identificador, str) and identificador:
                llamada_limpia["id"] = identificador

            normalizadas.append(
                {
                    "call": llamada_limpia,
                    "name": nombre,
                    "arguments": argumentos,
                    "error": error,
                }
            )

        return normalizadas

    def _preparar_mensajes_para_ollama(self, mensajes):
        """
        Sanitiza el historial antes de enviarlo.

        Si quedó una tool call con argumentos no válidos, se reemplazan por {}
        para que Ollama nunca reciba JSON incompleto.
        """
        preparados = []
        for mensaje in mensajes:
            if not isinstance(mensaje, dict):
                continue

            nuevo = {
                "role": mensaje.get("role", "user"),
                "content": mensaje.get("content") or "",
            }
            if mensaje.get("role") == "tool" and mensaje.get("tool_name"):
                nuevo["tool_name"] = mensaje["tool_name"]

            if mensaje.get("tool_calls"):
                llamadas = self._normalizar_llamadas(mensaje["tool_calls"])
                if llamadas:
                    nuevo["tool_calls"] = [item["call"] for item in llamadas]

            preparados.append(nuevo)

        return preparados

    def _error_argumentos(self, nombre, detalle):
        """Devuelve al modelo un error controlado por argumentos incompletos."""
        nombre_lower = (nombre or "").lower()

        if nombre_lower == "ejecutar_sql":
            self.errores_sql_consecutivos += 1
            self.ultimo_error_sql = detalle

            if self.errores_sql_consecutivos >= MAX_ERRORES_SQL:
                self.limite_alcanzado = True
                return (
                    "Error: la llamada a ejecutar_sql llegó con argumentos "
                    "incompletos o inválidos. "
                    + detalle
                    + "\nSe alcanzó el máximo de 3 intentos. No ejecutes otra "
                    "consulta."
                )

            restantes = MAX_ERRORES_SQL - self.errores_sql_consecutivos
            return (
                "Error: la llamada a ejecutar_sql llegó con argumentos "
                "incompletos o inválidos. "
                + detalle
                + '\nVuelve a generar la llamada con el objeto JSON completo, '
                'por ejemplo {"sql": "SELECT ..."}. '
                + f"Te quedan {restantes} intento(s)."
            )

        return (
            f"Error: la llamada a {nombre} llegó incompleta o inválida. "
            + detalle
            + " Vuelve a generar la llamada con argumentos válidos."
        )

    def _ejecutar_herramienta(self, nombre, argumentos):
        """Ejecuta una herramienta y devuelve el texto para el modelo."""
        nombre = (nombre or "").lower()

        if nombre == "consultar_esquema":
            if self.consulta_valida_obtenida:
                return (
                    "Ya tienes datos reales de la consulta. No consultes el "
                    "esquema otra vez; responde al usuario con esos resultados."
                )
            if self.esquema_consultado:
                return (
                    "El esquema ya fue consultado y está en el contexto "
                    "anterior. No lo repitas. Usa ejecutar_sql con los nombres "
                    "reales. Si el usuario solo pregunta qué datos existen, "
                    "responde con un resumen breve."
                )
            try:
                texto_esquema = consultar_esquema()
            except Exception as exc:
                return f"Error al consultar el esquema: {exc}"
            self.esquema_consultado = True
            return (
                texto_esquema
                + "\n\nUsa estas tablas, columnas y relaciones para generar "
                "la consulta. No repitas el esquema completo en la respuesta "
                "final."
            )

        if nombre == "ejecutar_sql":
            if self.consulta_valida_obtenida:
                return (
                    "Ya se obtuvo una consulta válida para esta pregunta. No "
                    "ejecutes otra consulta; responde con los resultados "
                    "obtenidos."
                )

            sql = argumentos.get("sql")
            if not sql:
                return "Error: falta el parámetro 'sql' para ejecutar la consulta."

            sql = self._reparar_sql(sql)
            self.ultima_sql = sql
            motivo = self._validar_consulta_para_pregunta(sql)
            if motivo:
                self.errores_sql_consecutivos += 1
                self.ultimo_error_sql = motivo
                if self.errores_sql_consecutivos >= MAX_ERRORES_SQL:
                    self.limite_alcanzado = True
                    return (
                        "La consulta no cumple los requisitos de la pregunta: "
                        + motivo
                        + "\nSe alcanzó el máximo de 3 intentos. No ejecutes "
                        "otra consulta."
                    )
                restantes = MAX_ERRORES_SQL - self.errores_sql_consecutivos
                return (
                    "La consulta no cumple los requisitos de la pregunta: "
                    + motivo
                    + "\nCorrige el SQL y vuelve a intentarlo. "
                    f"Te quedan {restantes} intento(s)."
                )

            resultado = ejecutar_sql(sql)

            if resultado.get("ok"):
                self.ultimo_resultado = resultado
                self.ultimo_error_sql = None
                self.errores_sql_consecutivos = 0
                self.consulta_valida_obtenida = True
                return (
                    formatear_resultado(resultado)
                    + "\n\nUsa exclusivamente estas filas para responder en "
                    "español. No muestres la consulta SQL ni repitas el "
                    "esquema."
                )

            self.errores_sql_consecutivos += 1
            self.ultimo_error_sql = resultado.get("error", "error desconocido")

            if self.errores_sql_consecutivos >= MAX_ERRORES_SQL:
                self.limite_alcanzado = True
                return (
                    "Error de SQLite: "
                    + self.ultimo_error_sql
                    + "\nSe alcanzó el máximo de 3 intentos. No ejecutes otra "
                    "consulta."
                    + self._sugerencia_error(self.ultimo_error_sql)
                )

            restantes = MAX_ERRORES_SQL - self.errores_sql_consecutivos
            return (
                "Error de SQLite: "
                + self.ultimo_error_sql
                + self._sugerencia_error(self.ultimo_error_sql)
                + f"\nCorrige la consulta y vuelve a intentarlo. "
                f"Te quedan {restantes} intento(s)."
            )

        if nombre == "exportar_csv":
            if not self.ultimo_resultado:
                return (
                    "Error: no hay un resultado para exportar. Primero debes "
                    "ejecutar una consulta con ejecutar_sql."
                )
            nombre_archivo = (
                argumentos.get("nombre_archivo")
                or argumentos.get("nombre_base")
                or "resultado_ventas"
            )
            try:
                ruta = exportar_csv(self.ultimo_resultado, nombre_archivo)
            except Exception as exc:
                return f"Error al exportar el CSV: {exc}"
            self.exportacion_realizada = True
            self.ruta_exportada = ruta
            return (
                "El archivo CSV se guardó correctamente en: "
                + ruta
                + "\nInforma esta ruta al usuario en tu respuesta final."
            )

        return (
            f"Error: herramienta desconocida '{nombre}'. "
            "Usa consultar_esquema, ejecutar_sql o exportar_csv."
        )

    @staticmethod
    def _sugerencia_error(error):
        """Devuelve pistas concretas para corregir errores SQL habituales."""
        error_lower = (error or "").lower()
        sugerencias = []

        if "ambiguous column name" in error_lower:
            sugerencias.append(
                "Califica las columnas con el alias de su tabla, por ejemplo "
                "s.customer_id, p.price o c.name."
            )
        if "no such table" in error_lower:
            sugerencias.append(
                "Usa solo las tablas reales: CUSTOMERS, CATEGORIES, PRODUCTS "
                "y SALES."
            )
            for nombre_malo, nombre_bueno in (
                ("clientes", "CUSTOMERS"),
                ("ventas", "SALES"),
                ("productos", "PRODUCTS"),
                ("categorias", "CATEGORIES"),
            ):
                if nombre_malo in error_lower:
                    sugerencias.append(
                        f"Usa {nombre_bueno} en lugar de {nombre_malo}."
                    )
        if "no such column" in error_lower:
            sugerencias.append(
                "Usa las columnas exactas del esquema: customer_id, product_id, "
                "category_id, sale_id, name, price, quantity y sale_date."
            )
            if ".id" in error_lower or " id" in error_lower:
                sugerencias.append(
                    "No uses 'id'; usa customer_id, product_id, category_id o "
                    "sale_id según corresponda."
                )
            if "price" in error_lower:
                sugerencias.append("El precio está en PRODUCTS.price.")
        if "syntax error" in error_lower:
            sugerencias.append(
                "Revisa las comas, los alias y la cláusula GROUP BY; no uses "
                "palabras reservadas como nombres de columna."
            )
        if not sugerencias:
            sugerencias.append(
                "Revisa el esquema y corrige la consulta antes de reintentarla."
            )

        sugerencias.append(
            "Relaciones: SALES.customer_id = CUSTOMERS.customer_id; "
            "SALES.product_id = PRODUCTS.product_id; "
            "PRODUCTS.category_id = CATEGORIES.category_id."
        )
        return "\nSugerencia: " + " ".join(sugerencias)

    @staticmethod
    def _pregunta_sobre_esquema(pregunta):
        texto = (pregunta or "").lower()
        frases = (
            "qué tablas",
            "que tablas",
            "cuáles tablas",
            "cuales tablas",
            "qué columnas",
            "que columnas",
            "esquema",
            "estructura de la base",
            "estructura de base",
            "qué datos existen",
            "que datos existen",
            "qué datos hay",
            "que datos hay",
            "qué información hay",
            "que informacion hay",
            "qué información contiene",
            "que informacion contiene",
            "qué se puede consultar",
            "que se puede consultar",
            "qué contiene la base",
            "que contiene la base",
        )
        return any(frase in texto for frase in frases)

    @staticmethod
    def _parece_consulta_sql(texto):
        if not texto:
            return False

        if "```sql" in texto.lower():
            return True

        patron = re.compile(
            r"(?is)\b(select|with)\b.{0,300}\b"
            r"(from|where|join|group\s+by|order\s+by|limit)\b"
        )
        if patron.search(texto):
            return True

        for linea in texto.splitlines():
            if re.match(r"^\s*(select|with)\b", linea, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _menciona_resultado(texto, resultado):
        """Comprueba que la respuesta use al menos un valor real consultado."""
        if not isinstance(resultado, dict):
            return True

        filas = resultado.get("filas") or []
        if not filas:
            return True

        texto_normalizado = SalesAgent._sin_acentos(texto or "").lower()
        numeros_texto = re.findall(r"-?\d+(?:[.,]\d+)*", texto or "")

        for fila in filas:
            for valor in fila:
                if valor is None:
                    continue

                if isinstance(valor, (int, float)) and not isinstance(valor, bool):
                    objetivo = abs(float(valor))
                    for token in numeros_texto:
                        for candidato in SalesAgent._candidatos_numericos(token):
                            tolerancia = max(0.01, objetivo * 0.001)
                            if abs(candidato - objetivo) <= tolerancia:
                                return True
                else:
                    candidato = SalesAgent._sin_acentos(str(valor)).strip().lower()
                    if len(candidato) >= 2 and candidato in texto_normalizado:
                        return True
                    if re.match(r"^\d{4}-\d{2}-\d{2}", candidato):
                        if candidato[:4] in texto_normalizado:
                            return True

        return False

    def _numeros_coherentes(self, texto, resultado):
        """Rechaza respuestas que inventen cifras que no están en el resultado."""
        filas = resultado.get("filas") or []
        if not filas:
            return True

        valores = set()
        anios = set()
        for fila in filas:
            for valor in fila:
                if isinstance(valor, (int, float)) and not isinstance(valor, bool):
                    valores.add(abs(float(valor)))
                elif isinstance(valor, str):
                    for coincidencia in re.finditer(r"\b(20\d{2})\b", valor):
                        anios.add(float(coincidencia.group(1)))

        for coincidencia in re.finditer(
            r"\b(20\d{2})\b", self.pregunta_actual or ""
        ):
            anios.add(float(coincidencia.group(1)))

        limite_pequeno = max(10, len(filas))
        for token in re.findall(r"-?\d+(?:[.,]\d+)*", texto or ""):
            candidatos = SalesAgent._candidatos_numericos(token)
            if not candidatos:
                continue

            aceptado = False
            for candidato in candidatos:
                valor = abs(candidato)
                if valor <= limite_pequeno:
                    aceptado = True
                    break
                if any(
                    abs(valor - esperado) <= max(0.01, esperado * 0.001)
                    for esperado in valores
                ):
                    aceptado = True
                    break
                if valor in anios:
                    aceptado = True
                    break

            if not aceptado:
                return False

        return True

    @staticmethod
    def _candidatos_numericos(token):
        token = token.strip().replace(" ", "")
        if not token:
            return set()

        if "," in token and "." in token:
            variantes = [token.replace(".", "").replace(",", ".")]
        elif "," in token:
            variantes = [token.replace(",", "."), token.replace(",", "")]
        elif token.count(".") > 1:
            variantes = [token.replace(".", "")]
        elif "." in token:
            variantes = [token, token.replace(".", "")]
        else:
            variantes = [token]

        candidatos = set()
        for variante in variantes:
            try:
                candidatos.add(float(variante))
            except ValueError:
                pass
        return candidatos

    @staticmethod
    def _sin_acentos(texto):
        return "".join(
            caracter
            for caracter in unicodedata.normalize("NFKD", texto)
            if not unicodedata.combining(caracter)
        )

    def _respuesta_con_datos(self):
        if not self.ultimo_resultado:
            return "No hay datos reales disponibles para responder."

        filas = self.ultimo_resultado.get("filas") or []
        columnas = self.ultimo_resultado.get("columnas") or []

        if not filas:
            pregunta = (self.pregunta_actual or "").lower()
            if any(
                frase in pregunta
                for frase in ("nunca", "sin ventas", "no se vendieron")
            ):
                return (
                    "Todos los productos registrados tienen al menos una "
                    "venta; no hay productos sin vender."
                )
            return (
                "La consulta se ejecutó correctamente, pero no devolvió "
                "filas."
            )

        if len(filas) == 1 and columnas:
            pares = "; ".join(
                f"{columna}: {filas[0][indice]}"
                for indice, columna in enumerate(columnas)
            )
            return "Resultado real: " + pares + "."

        def indice(*nombres):
            for posicion, columna in enumerate(columnas):
                if columna.lower() in nombres:
                    return posicion
            return None

        idx_categoria = indice("category_name", "categoria", "categoría")
        idx_cliente = indice("cliente", "customer_name", "name")
        idx_producto = indice("product_name", "producto")
        idx_anio = indice("anio", "año", "year")
        idx_ingresos = indice("ingresos", "ingreso", "total_ingresos")
        idx_gasto = indice("gasto_producto", "spending", "gasto")
        idx_gasto_total = indice("gasto_total", "total_spending")
        idx_unidades = indice(
            "unidades",
            "unidades_vendidas",
            "total_cantidad",
            "cantidad",
            "quantity",
        )

        idx_total_clientes = indice("total_clientes", "total")

        if idx_total_clientes is not None and idx_cliente is not None:
            total = filas[0][idx_total_clientes]
            nombres = [str(fila[idx_cliente]) for fila in filas]
            return (
                f"Hay {total} clientes registrados:\n- "
                + "\n- ".join(nombres)
            )

        if idx_anio is not None and idx_ingresos is not None:
            lineas = []
            for fila in filas:
                unidades = (
                    f", {fila[idx_unidades]} unidades vendidas"
                    if idx_unidades is not None
                    else ""
                )
                lineas.append(
                    f"- {fila[idx_anio]}: ingresos {fila[idx_ingresos]}"
                    f"{unidades}"
                )
            return "Comportamiento de las ventas por año:\n" + "\n".join(
                lineas
            )

        if (
            idx_cliente is not None
            and idx_producto is not None
            and idx_gasto is not None
        ):
            cliente = filas[0][idx_cliente]
            if idx_gasto_total is not None:
                total = filas[0][idx_gasto_total]
            else:
                total = sum(
                    float(fila[idx_gasto])
                    for fila in filas
                    if isinstance(fila[idx_gasto], (int, float))
                )
            lineas = []
            for fila in filas:
                detalle = f"- {fila[idx_producto]}: {fila[idx_gasto]}"
                if idx_unidades is not None:
                    detalle += f" ({fila[idx_unidades]} unidades)"
                lineas.append(detalle)
            return (
                f"{cliente} gastó {total} en total.\n"
                "Productos comprados:\n" + "\n".join(lineas)
            )

        if (
            idx_producto is not None
            and idx_categoria is None
            and idx_anio is None
            and idx_gasto is None
            and idx_ingresos is None
        ):
            idx_precio = indice("price", "precio")
            lineas = []
            for fila in filas:
                detalle = f"- {fila[idx_producto]}"
                if idx_precio is not None:
                    detalle += f": {fila[idx_precio]}"
                lineas.append(detalle)
            return "Productos registrados:\n" + "\n".join(lineas)

        if idx_categoria is not None and idx_ingresos is not None:
            lineas = [
                f"- {fila[idx_categoria]}: {fila[idx_ingresos]}"
                for fila in filas
            ]
            return "Ingresos por categoría:\n" + "\n".join(lineas)

        if idx_categoria is not None:
            metricas = [
                posicion
                for posicion in range(len(columnas))
                if posicion != idx_categoria
                and not columnas[posicion].lower().endswith("_id")
                and isinstance(filas[0][posicion], (int, float))
            ]
            if metricas:
                lineas = []
                for fila in filas:
                    detalles = ", ".join(
                        f"{columnas[posicion]}={fila[posicion]}"
                        for posicion in metricas
                    )
                    lineas.append(f"- {fila[idx_categoria]}: {detalles}")
                return "Resultados por categoría:\n" + "\n".join(lineas)

        if (
            idx_cliente is not None
            and idx_producto is None
            and idx_anio is None
            and idx_unidades is None
            and not any(
                isinstance(filas[0][posicion], (int, float))
                and not columnas[posicion].lower().endswith("_id")
                for posicion in range(len(columnas))
            )
        ):
            nombres = [str(fila[idx_cliente]) for fila in filas]
            return "Clientes registrados:\n- " + "\n- ".join(nombres)

        if (
            idx_cliente is not None
            and idx_unidades is not None
            and idx_producto is None
            and idx_anio is None
        ):
            lineas = [
                f"{posicion}. {fila[idx_cliente]}: {fila[idx_unidades]}"
                for posicion, fila in enumerate(filas, 1)
            ]
            return "Clientes con mayor cantidad comprada:\n" + "\n".join(
                lineas
            )

        return (
            "Resultados reales obtenidos de la base de datos:\n"
            + formatear_resultado(self.ultimo_resultado)
        )

    @staticmethod
    def _repitio_esquema(texto):
        if not texto:
            return False
        marcas = (
            "claves foráneas",
            "customer_id:",
            "product_id:",
            "sale_date:",
            "primary key",
        )
        return (
            len(texto) > 1000
            or sum(marca in texto.lower() for marca in marcas) >= 3
        )

    def _resumen_esquema(self):
        tablas = []
        for linea in self.esquema_actual.splitlines():
            linea = linea.strip()
            if not linea.startswith("Tabla: "):
                continue
            resto = linea[len("Tabla: "):]
            nombre = resto.split("(", 1)[0].strip()
            filas = ""
            if "(" in resto and ")" in resto:
                filas = resto.split("(", 1)[1].split(")", 1)[0].strip()
            tablas.append(f"{nombre} ({filas})" if filas else nombre)

        if not tablas:
            return (
                "La base de datos contiene información de clientes, categorías, "
                "productos y ventas."
            )

        return (
            "La base de datos contiene las tablas "
            + ", ".join(tablas)
            + ". Incluye clientes, categorías, productos y ventas. Puedes "
            "preguntar por ventas por año, clientes, productos, categorías o "
            "países."
        )

    def _respuesta_por_limite(self):
        error = self.ultimo_error_sql or ""
        error_lower = error.lower()
        extra = ""

        if "no such column" in error_lower:
            columna = ""
            if ":" in error:
                columna = error.split(":", 1)[1].strip()
            tablas = "; ".join(
                f"{tabla}({', '.join(columnas)})"
                for tabla, columnas in self.columnas_por_tabla.items()
            )
            extra = (
                f" La columna solicitada ({columna or 'desconocida'}) no "
                f"existe. Columnas válidas: {tablas}."
            )
        elif "no such table" in error_lower:
            extra = (
                " Las tablas válidas son CUSTOMERS, CATEGORIES, PRODUCTS y "
                "SALES."
            )

        return (
            "No pude completar la consulta después de 3 intentos.\n"
            f"Última consulta: {self.ultima_sql}\n"
            f"Último error de SQLite: {self.ultimo_error_sql}\n"
            + extra
            + " Puedes reformular la pregunta o pedir ayuda sobre los datos "
            "disponibles."
        )
