"""
Pruebas reales de extremo a extremo con Ollama y MiniCPM5.

Se ejecutan solo cuando se define RUN_OLLAMA_TESTS=1:

    RUN_OLLAMA_TESTS=1 OLLAMA_MODEL=openbmb/minicpm5:latest \
    python -m unittest tests.test_real_e2e -v
"""

import csv
import os
import sqlite3
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from agent.sales_agent import SalesAgent


@unittest.skipUnless(
    os.getenv("RUN_OLLAMA_TESTS") == "1",
    "Pruebas reales desactivadas. Usa RUN_OLLAMA_TESTS=1.",
)
class TestE2EMiniCPM5(unittest.TestCase):
    def setUp(self):
        self.agente = SalesAgent(tiempo_espera=300)

    def test_01_tablas(self):
        respuesta = self.agente.preguntar(
            "¿Qué tablas existen en la base de datos?"
        )
        for tabla in ("CUSTOMERS", "CATEGORIES", "PRODUCTS", "SALES"):
            self.assertIn(tabla, respuesta.upper())

    def test_02_top_clientes_2024(self):
        respuesta = self.agente.preguntar(
            "¿Cuáles son los cinco clientes que más compraron durante 2024?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        filas = self.agente.ultimo_resultado["filas"]
        self.assertIn("sum(s.quantity)", sql.replace(" ", ""))
        self.assertIn("customers", sql)
        self.assertIn("2024", sql)
        self.assertEqual(len(filas), 5)
        self.assertIn("Ana Torres", respuesta)

    def test_03_producto_mas_vendido(self):
        respuesta = self.agente.preguntar("¿Cuál es el producto más vendido?")
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("sum(s.quantity)", sql.replace(" ", ""))
        self.assertIn("products", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 1)
        self.assertIn("Camiseta de Algodón", respuesta)

    def test_04_promedio_por_categoria(self):
        self.agente.preguntar("¿Cuál es el promedio de ventas por categoría?")
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("avg(", sql)
        self.assertIn("categories", sql)
        self.assertIn("products", sql)
        self.assertNotIn("2024", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 5)

    def test_05_exportar_ventas_2024(self):
        self.agente.preguntar(
            "Exporta todas las ventas de 2024 a un archivo CSV."
        )
        ruta = Path(self.agente.ruta_exportada)
        self.assertTrue(ruta.exists())

        try:
            with ruta.open(encoding="utf-8-sig") as archivo:
                filas = list(csv.reader(archivo))
            self.assertEqual(len(filas) - 1, 24)
            self.assertEqual(
                filas[0],
                [
                    "sale_id",
                    "customer_id",
                    "product_id",
                    "quantity",
                    "sale_date",
                ],
            )
            self.assertTrue(all(fila[4].startswith("2024") for fila in filas[1:]))
        finally:
            ruta.unlink()

    def test_06_eliminar_clientes_bloqueado(self):
        conexion = sqlite3.connect(BASE_DIR / "database" / "sales.db")
        antes = conexion.execute("SELECT COUNT(*) FROM CUSTOMERS").fetchone()[0]
        conexion.close()

        respuesta = self.agente.preguntar(
            "Elimina todos los clientes de la base de datos."
        )

        conexion = sqlite3.connect(BASE_DIR / "database" / "sales.db")
        despues = conexion.execute("SELECT COUNT(*) FROM CUSTOMERS").fetchone()[0]
        conexion.close()

        self.assertEqual(antes, 12)
        self.assertEqual(despues, 12)
        self.assertIn("solo lectura", respuesta.lower())

    def test_07_recuperacion_de_errores(self):
        self.agente.preguntar(
            "¿Cuántos clientes hay? Consulta la tabla CLIENTES "
            "(en español) para responder."
        )
        self.assertIn("customers", (self.agente.ultima_sql or "").lower())
        self.assertEqual(self.agente.ultimo_resultado["filas"][0][0], 12)

        agente_error = SalesAgent(tiempo_espera=300)
        respuesta = agente_error.preguntar(
            "Genera una consulta que seleccione únicamente la columna "
            "descuento de la tabla SALES. Si esa columna no existe, corrige "
            "el SQL y muestra una columna válida de SALES."
        )
        self.assertIn(
            "no existe",
            respuesta.lower(),
        )
        self.assertIn("columnas válidas", respuesta.lower())

    def test_08_ingresos_por_categoria_2024(self):
        respuesta = self.agente.preguntar(
            "¿Cuánto dinero se generó en ventas por cada categoría "
            "durante 2024?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("sum(s.quantity * p.price)", sql.replace("\n", " "))
        self.assertIn("sale_date >= '2024-01-01'", sql)
        self.assertIn("sale_date < '2025-01-01'", sql)
        self.assertIn("categories", sql)
        self.assertIn("products", sql)
        self.assertIn("group by", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 5)
        self.assertIn("3950.0", respuesta)

    def test_09_gasto_ana_torres_2024(self):
        respuesta = self.agente.preguntar(
            "¿Cuánto dinero gastó Ana Torres durante 2024 y qué productos "
            "compró?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("c.name = 'ana torres'", sql)
        self.assertIn("sale_date >= '2024-01-01'", sql)
        self.assertIn("sale_date < '2025-01-01'", sql)
        self.assertIn("product_name", sql)
        self.assertIn("sum(s.quantity * p.price)", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 5)
        self.assertIn("Ana Torres", respuesta)
        self.assertIn("660.0", respuesta)

    def test_10_comportamiento_tres_anios(self):
        respuesta = self.agente.preguntar(
            "¿Cuál fue el comportamiento de las ventas durante los tres "
            "años disponibles?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("strftime('%y', s.sale_date)", sql)
        self.assertIn("group by", sql)
        self.assertNotIn("where", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 3)
        for anio in ("2023", "2024", "2025"):
            self.assertIn(anio, respuesta)

    def test_11_columnas_customers(self):
        respuesta = self.agente.preguntar(
            "¿Qué columnas tiene la tabla CUSTOMERS?"
        )
        self.assertIn("Tabla: CUSTOMERS", respuesta)
        self.assertIn("customer_id", respuesta)
        self.assertIn("name", respuesta)
        self.assertIn("email", respuesta)
        self.assertIn("country", respuesta)
        self.assertNotIn("Tabla: SALES", respuesta)

    def test_12_cantidad_y_lista_clientes(self):
        respuesta = self.agente.preguntar(
            "¿Cuántos clientes hay registrados y cuáles son?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("count(", sql)
        self.assertNotIn("limit", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 12)
        self.assertIn("Hay 12 clientes registrados", respuesta)
        self.assertIn("Ana Torres", respuesta)
        self.assertIn("Tomás Navarro", respuesta)

    def test_13_listar_todos_los_productos(self):
        respuesta = self.agente.preguntar(
            "Dime cuáles son los 16 productos registrados."
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("from products", sql)
        self.assertNotIn("limit", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 16)
        self.assertIn("Laptop Pro 15", respuesta)
        self.assertIn("Guía Práctica de Python", respuesta)

    def test_14_productos_nunca_vendidos(self):
        respuesta = self.agente.preguntar(
            "¿Qué productos nunca se vendieron?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("left join", sql)
        self.assertIn("sale_id is null", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 0)
        self.assertIn("al menos una venta", respuesta.lower())

    def test_15_dinero_por_anio(self):
        respuesta = self.agente.preguntar(
            "¿Cuánto dinero se generó durante cada año?"
        )
        sql = (self.agente.ultima_sql or "").lower()
        self.assertIn("strftime('%y', s.sale_date)", sql)
        self.assertIn("group by", sql)
        self.assertNotIn("where", sql)
        self.assertEqual(len(self.agente.ultimo_resultado["filas"]), 3)
        self.assertIn("9768.0", respuesta)
        self.assertIn("10996.0", respuesta)
        self.assertIn("12562.0", respuesta)

    def test_16_gasto_andres_vega_y_typo(self):
        respuesta = self.agente.preguntar(
            "¿Cuánto gastó Andrés Vega en total?"
        )
        self.assertEqual(self.agente.ultimo_resultado["filas"], [["Andrés Vega", 400.0]])
        self.assertIn("400.0", respuesta)

        agente_typo = SalesAgent(tiempo_espera=300)
        respuesta_typo = agente_typo.preguntar(
            "¿Cuánto gastó Andrez Vega en total?"
        )
        self.assertEqual(
            agente_typo.nombre_cliente_resuelto, "Andrés Vega"
        )
        self.assertEqual(
            agente_typo.ultimo_resultado["filas"],
            [["Andrés Vega", 400.0]],
        )
        self.assertIn("400.0", respuesta_typo)

    def test_17_recuperacion_columnas_y_tabla(self):
        respuesta = self.agente.preguntar(
            "Consulta el nombre_completo de todos los clientes."
        )
        self.assertIn("Ana Torres", respuesta)
        self.assertIn("Tomás Navarro", respuesta)

        agente_error = SalesAgent(tiempo_espera=300)
        respuesta_error = agente_error.preguntar(
            "Consulta la columna stock_extra de PRODUCTS."
        )
        self.assertIn("no existe", respuesta_error.lower())
        self.assertIn("columnas válidas", respuesta_error.lower())
        self.assertIsNone(agente_error.ultimo_resultado)



if __name__ == "__main__":
    unittest.main()
