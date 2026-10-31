"""
Pruebas del proyecto SQL Sales Agent.

Ejecutar desde la raíz del proyecto:
    python -m unittest discover -s tests -v

Las pruebas de herramientas no necesitan Ollama. La prueba de integración
con el modelo se activa con RUN_OLLAMA_TESTS=1.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from database.create_database import DB_PATH, crear_base_de_datos
from tools.export_tool import exportar_csv
from tools.schema_tool import consultar_esquema
from tools.sql_tool import ejecutar_sql, validar_sql, SQLNoPermitido
from agent.sales_agent import SalesAgent


class TestBaseDeDatos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "sales_test.db"
        crear_base_de_datos(cls.db, verbose=False)
        cls.conexion = sqlite3.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conexion.close()
        cls.tmp.cleanup()

    def test_01_creacion_y_cantidad_de_datos(self):
        tablas = {
            fila[0]
            for fila in self.conexion.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(
            tablas, {"CUSTOMERS", "CATEGORIES", "PRODUCTS", "SALES"}
        )

        clientes = self.conexion.execute(
            "SELECT COUNT(*) FROM CUSTOMERS"
        ).fetchone()[0]
        categorias = self.conexion.execute(
            "SELECT COUNT(*) FROM CATEGORIES"
        ).fetchone()[0]
        productos = self.conexion.execute(
            "SELECT COUNT(*) FROM PRODUCTS"
        ).fetchone()[0]
        ventas = self.conexion.execute(
            "SELECT COUNT(*) FROM SALES"
        ).fetchone()[0]

        self.assertGreaterEqual(clientes, 10)
        self.assertGreaterEqual(categorias, 4)
        self.assertGreaterEqual(productos, 12)
        self.assertGreaterEqual(ventas, 40)

        anios = {
            fila[0]
            for fila in self.conexion.execute(
                "SELECT DISTINCT substr(sale_date, 1, 4) FROM SALES"
            )
        }
        self.assertEqual(anios, {"2023", "2024", "2025"})

        paises = {
            fila[0]
            for fila in self.conexion.execute(
                "SELECT DISTINCT country FROM CUSTOMERS"
            )
        }
        self.assertGreaterEqual(len(paises), 4)

    def test_02_tablas_relacionadas_con_claves_foraneas(self):
        foraneas_ventas = {
            (fila[2], fila[3], fila[4])
            for fila in self.conexion.execute(
                "PRAGMA foreign_key_list(SALES)"
            )
        }
        self.assertIn(("CUSTOMERS", "customer_id", "customer_id"), foraneas_ventas)
        self.assertIn(("PRODUCTS", "product_id", "product_id"), foraneas_ventas)

        foraneas_productos = {
            (fila[2], fila[3], fila[4])
            for fila in self.conexion.execute(
                "PRAGMA foreign_key_list(PRODUCTS)"
            )
        }
        self.assertIn(
            ("CATEGORIES", "category_id", "category_id"), foraneas_productos
        )


class TestHerramientas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DB_PATH.exists():
            crear_base_de_datos(DB_PATH, verbose=False)

    def test_03_consultar_esquema(self):
        esquema = consultar_esquema()
        for texto in (
            "CUSTOMERS",
            "CATEGORIES",
            "PRODUCTS",
            "SALES",
            "customer_id",
            "sale_date",
            "Claves foráneas",
        ):
            self.assertIn(texto, esquema)

    def test_04_select_permitido(self):
        resultado = ejecutar_sql("SELECT COUNT(*) AS total FROM CUSTOMERS")
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["filas"][0][0], 12)

    def test_05_with_select_permitido(self):
        resultado = ejecutar_sql(
            "WITH clientes AS (SELECT * FROM CUSTOMERS) "
            "SELECT COUNT(*) FROM clientes"
        )
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["filas"][0][0], 12)

    def test_06_rechaza_insert(self):
        resultado = ejecutar_sql(
            "INSERT INTO CUSTOMERS VALUES (99, 'X', 'x@x.com', 'X')"
        )
        self.assertFalse(resultado["ok"])

    def test_07_rechaza_delete(self):
        resultado = ejecutar_sql("DELETE FROM SALES")
        self.assertFalse(resultado["ok"])

    def test_08_rechaza_drop(self):
        resultado = ejecutar_sql("DROP TABLE SALES")
        self.assertFalse(resultado["ok"])

    def test_09_rechaza_multiples_sentencias_y_pragma(self):
        casos = [
            "SELECT 1; DROP TABLE SALES",
            "PRAGMA table_info(CUSTOMERS)",
            "WITH x AS (SELECT 1) DELETE FROM SALES",
            "SELECT * FROM CUSTOMERS; SELECT * FROM SALES",
        ]
        for sql in casos:
            with self.subTest(sql=sql):
                self.assertFalse(ejecutar_sql(sql)["ok"])

    def test_10_cadenas_y_comentarios_no_generan_falsos_positivos(self):
        self.assertTrue(
            ejecutar_sql("SELECT 'DELETE; DROP TABLE SALES' AS texto")["ok"]
        )
        self.assertTrue(
            ejecutar_sql("SELECT 1 -- ; DELETE FROM SALES\n")["ok"]
        )

    def test_11_limite_de_filas(self):
        resultado = ejecutar_sql(
            "WITH RECURSIVE numeros(x) AS ("
            "SELECT 1 UNION ALL SELECT x + 1 FROM numeros WHERE x < 150"
            ") SELECT x FROM numeros"
        )
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["num_filas"], 100)
        self.assertTrue(resultado["truncado"])

    def test_12_limite_de_tiempo(self):
        resultado = ejecutar_sql(
            "WITH RECURSIVE infinito(x) AS ("
            "SELECT 1 UNION ALL SELECT x + 1 FROM infinito"
            ") SELECT COUNT(*) FROM infinito",
            max_segundos=0.05,
        )
        self.assertFalse(resultado["ok"])
        self.assertIn("tiempo", resultado["error"].lower())

    def test_13_exportar_csv(self):
        resultado = ejecutar_sql(
            "SELECT customer_id, name, country FROM CUSTOMERS "
            "ORDER BY customer_id LIMIT 3"
        )
        ruta = Path(exportar_csv(resultado, "prueba_clientes"))
        try:
            self.assertTrue(ruta.exists())
            self.assertEqual(ruta.parent, BASE_DIR / "exports")
            datos = pd.read_csv(ruta)
            self.assertEqual(len(datos), 3)
            self.assertEqual(
                list(datos.columns), ["customer_id", "name", "country"]
            )
            self.assertEqual(datos.iloc[0]["name"], "Ana Torres")
        finally:
            if ruta.exists():
                ruta.unlink()

    def test_14_exportar_rechaza_resultado_invalido(self):
        with self.assertRaises(ValueError):
            exportar_csv({"ok": False, "error": "consulta fallida"})

    def test_15_validar_sql_no_modifica_la_base(self):
        antes = ejecutar_sql("SELECT COUNT(*) FROM CUSTOMERS")["filas"][0][0]
        for sql in ("DELETE FROM CUSTOMERS", "UPDATE CUSTOMERS SET name='X'"):
            with self.assertRaises(SQLNoPermitido):
                validar_sql(sql)
        despues = ejecutar_sql("SELECT COUNT(*) FROM CUSTOMERS")["filas"][0][0]
        self.assertEqual(antes, despues)


class TestCorreccionAutomatica(unittest.TestCase):
    def _agente_con_respuestas(self, respuestas):
        agente = SalesAgent(modelo="modelo-de-prueba")
        pendientes = list(respuestas)

        def chat_falso(mensajes, herramientas):
            if not pendientes:
                return {"message": {"role": "assistant", "content": "Fin."}}
            return pendientes.pop(0)

        agente._chat = chat_falso
        return agente

    @staticmethod
    def _llamada(nombre, argumentos):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": nombre,
                            "arguments": argumentos,
                        }
                    }
                ],
            }
        }

    def test_16_corrige_una_consulta_con_error(self):
        agente = self._agente_con_respuestas(
            [
                self._llamada(
                    "ejecutar_sql",
                    {"sql": "SELECT columna_inexistente FROM SALES"},
                ),
                self._llamada(
                    "ejecutar_sql",
                    {"sql": "SELECT COUNT(*) AS total FROM CUSTOMERS"},
                ),
                {
                    "message": {
                        "role": "assistant",
                        "content": "La base de datos tiene 12 clientes.",
                    }
                },
            ]
        )

        respuesta = agente.preguntar(
            "Consulta la columna columna_inexistente y luego corrige."
        )

        self.assertIn("12", respuesta)
        self.assertTrue(agente.ultimo_resultado["ok"])
        self.assertIsNone(agente.ultimo_error_sql)
        self.assertEqual(agente.errores_sql_consecutivos, 0)
        textos = [m.get("content", "") for m in agente.mensajes]
        self.assertTrue(any("no such column" in t for t in textos))

    def test_17_limita_a_tres_intentos(self):
        agente = self._agente_con_respuestas(
            [
                self._llamada("ejecutar_sql", {"sql": "SELECT * FROM NO_EXISTE_1"}),
                self._llamada("ejecutar_sql", {"sql": "SELECT * FROM NO_EXISTE_2"}),
                self._llamada("ejecutar_sql", {"sql": "SELECT * FROM NO_EXISTE_3"}),
            ]
        )

        respuesta = agente.preguntar("Consulta imposible")

        self.assertIn("3 intentos", respuesta)
        self.assertTrue(agente.limite_alcanzado)
        self.assertEqual(agente.errores_sql_consecutivos, 3)

    def test_18_flujo_de_exportacion_desde_el_agente(self):
        agente = self._agente_con_respuestas(
            [
                self._llamada(
                    "ejecutar_sql",
                    {"sql": "SELECT name, country FROM CUSTOMERS LIMIT 2"},
                ),
                self._llamada(
                    "exportar_csv", {"nombre_archivo": "prueba_agente"}
                ),
                {
                    "message": {
                        "role": "assistant",
                        "content": "Exportación completada.",
                    }
                },
            ]
        )

        respuesta = agente.preguntar("Exporta dos clientes a CSV")
        self.assertTrue(
            "Exportación" in respuesta
            or "exportaron" in respuesta.lower()
            or "Archivo generado en:" in respuesta
        )
        self.assertTrue(agente.exportacion_realizada)
        self.assertTrue(agente.ruta_exportada)
        ruta = Path(agente.ruta_exportada)
        try:
            self.assertTrue(ruta.exists())
        finally:
            if ruta.exists():
                ruta.unlink()

    def test_19_red_de_seguridad_para_exportar(self):
        agente = self._agente_con_respuestas(
            [
                self._llamada(
                    "ejecutar_sql",
                    {"sql": "SELECT name, country FROM CUSTOMERS LIMIT 2"},
                ),
                {
                    "message": {
                        "role": "assistant",
                        "content": "Listo, información exportada.",
                    }
                },
                {
                    "message": {
                        "role": "assistant",
                        "content": "Listo, archivo generado.",
                    }
                },
            ]
        )

        respuesta = agente.preguntar("Exporta dos clientes a CSV")
        self.assertTrue(agente.exportacion_realizada)
        self.assertTrue(
            "Archivo CSV generado en:" in respuesta
            or "Archivo generado en:" in respuesta
        )
        marcador = (
            "Archivo CSV generado en:"
            if "Archivo CSV generado en:" in respuesta
            else "Archivo generado en:"
        )
        ruta = Path(respuesta.split(marcador, 1)[1].splitlines()[0].strip())
        try:
            self.assertTrue(ruta.exists())
        finally:
            if ruta.exists():
                ruta.unlink()


@unittest.skipUnless(
    os.getenv("RUN_OLLAMA_TESTS") == "1",
    "Prueba de integración con Ollama desactivada. Usa RUN_OLLAMA_TESTS=1.",
)
class TestIntegracionOllama(unittest.TestCase):
    def test_20_agente_consulta_datos_reales(self):
        agente = SalesAgent()
        respuesta = agente.preguntar(
            "¿Cuántos clientes hay registrados? Responde con el número."
        )
        self.assertIn("12", respuesta)


if __name__ == "__main__":
    unittest.main()
