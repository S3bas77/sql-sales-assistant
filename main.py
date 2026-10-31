"""
Interfaz de terminal del SQL Sales Agent.

Ejecutar desde la raíz del proyecto:
    python main.py
"""

import os
import sys

from agent.sales_agent import (
    MODELO_POR_DEFECTO,
    ErrorOllama,
    SalesAgent,
    verificar_ollama,
)
from database.create_database import DB_PATH, crear_base_de_datos

BANNER = """========================================
       SQL SALES ASSISTANT
========================================

Asistente inteligente de ventas conectado a SQLite.

Escribe tu pregunta o 'salir' para terminar.
"""


def asegurar_base_de_datos():
    if not DB_PATH.exists():
        print("No se encontró la base de datos. Creándola...")
        crear_base_de_datos(verbose=False)
        print(f"Base de datos creada en: {DB_PATH}\n")


def main():
    asegurar_base_de_datos()
    print(BANNER)

    modelo = os.getenv("OLLAMA_MODEL", MODELO_POR_DEFECTO)
    disponible, mensaje = verificar_ollama(modelo=modelo)
    if not disponible:
        print("ERROR: el agente no puede iniciarse.")
        print(mensaje)
        print(
            "\nLas herramientas de SQLite se pueden probar sin Ollama con:"
            "\n    python -m unittest discover -s tests -v"
        )
        return 1

    print(mensaje)
    print("Escribe 'salir' para terminar.\n")

    agente = SalesAgent(modelo=modelo)

    while True:
        try:
            pregunta = input("Usuario: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nHasta luego.")
            break

        if not pregunta:
            continue
        if pregunta.lower() in {"salir", "exit", "quit"}:
            print("Hasta luego.")
            break

        try:
            respuesta = agente.preguntar(pregunta)
        except ErrorOllama as exc:
            print(f"Agente: {exc}\n")
            continue
        except Exception as exc:
            print(f"Agente: ocurrió un error inesperado: {exc}\n")
            continue

        print(f"Agente: {respuesta}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
