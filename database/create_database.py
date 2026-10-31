"""
Crea la base de datos SQLite del proyecto SQL Sales Agent.

Uso:
    python database/create_database.py

El script borra y vuelve a crear las cuatro tablas para que los datos
de prueba sean siempre los mismos (generación reproducible).
"""

import random
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "database" / "sales.db"

CUSTOMERS = [
    (1, "Ana Torres", "ana.torres@example.com", "México"),
    (2, "Carlos Ramírez", "carlos.ramirez@example.com", "Colombia"),
    (3, "Lucía Fernández", "lucia.fernandez@example.com", "España"),
    (4, "Mateo González", "mateo.gonzalez@example.com", "Argentina"),
    (5, "Sofía Herrera", "sofia.herrera@example.com", "Chile"),
    (6, "Diego Morales", "diego.morales@example.com", "Perú"),
    (7, "Valentina Silva", "valentina.silva@example.com", "Brasil"),
    (8, "Javier Ortiz", "javier.ortiz@example.com", "México"),
    (9, "Camila Castro", "camila.castro@example.com", "Colombia"),
    (10, "Andrés Vega", "andres.vega@example.com", "Ecuador"),
    (11, "Isabella Romero", "isabella.romero@example.com", "España"),
    (12, "Tomás Navarro", "tomas.navarro@example.com", "Uruguay"),
]

CATEGORIES = [
    (1, "Electrónica"),
    (2, "Hogar"),
    (3, "Ropa"),
    (4, "Deportes"),
    (5, "Libros"),
]

PRODUCTS = [
    (1, "Laptop Pro 15", 1, 1200.00),
    (2, "Smartphone X", 1, 800.00),
    (3, "Auriculares Bluetooth", 1, 120.00),
    (4, 'Monitor 27"', 1, 350.00),
    (5, "Cafetera Automática", 2, 150.00),
    (6, "Aspiradora Compacta", 2, 220.00),
    (7, "Juego de Sábanas", 2, 45.00),
    (8, "Lámpara LED", 2, 30.00),
    (9, "Chaqueta Impermeable", 3, 95.00),
    (10, "Zapatillas Urbanas", 3, 80.00),
    (11, "Camiseta de Algodón", 3, 25.00),
    (12, "Bicicleta Mountain", 4, 650.00),
    (13, "Mancuernas 10 kg", 4, 60.00),
    (14, "Balón de Fútbol", 4, 35.00),
    (15, "Novela Código Abierto", 5, 22.00),
    (16, "Guía Práctica de Python", 5, 40.00),
]


def generar_ventas():
    """Genera 72 ventas ficticias repartidas entre 2023, 2024 y 2025."""
    random.seed(2024)
    ventas = []
    sale_id = 1

    for year in (2023, 2024, 2025):
        # 24 ventas por año, con fechas variadas.
        for _ in range(24):
            customer_id = random.randint(1, len(CUSTOMERS))
            product_id = random.randint(1, len(PRODUCTS))
            quantity = random.randint(1, 4)
            month = random.randint(1, 12)
            day = random.randint(1, 28)
            sale_date = f"{year}-{month:02d}-{day:02d}"
            ventas.append((sale_id, customer_id, product_id, quantity, sale_date))
            sale_id += 1

    return ventas


SALES = generar_ventas()


def crear_base_de_datos(db_path=DB_PATH, verbose=True):
    """Crea (o recrea) la base de datos y devuelve la ruta del archivo."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conexion = sqlite3.connect(db_path)
    conexion.execute("PRAGMA foreign_keys = ON")

    cursor = conexion.cursor()
    cursor.executescript(
        """
        DROP TABLE IF EXISTS SALES;
        DROP TABLE IF EXISTS PRODUCTS;
        DROP TABLE IF EXISTS CATEGORIES;
        DROP TABLE IF EXISTS CUSTOMERS;

        CREATE TABLE CUSTOMERS (
            customer_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            country TEXT NOT NULL
        );

        CREATE TABLE CATEGORIES (
            category_id INTEGER PRIMARY KEY,
            category_name TEXT NOT NULL
        );

        CREATE TABLE PRODUCTS (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (category_id) REFERENCES CATEGORIES(category_id)
        );

        CREATE TABLE SALES (
            sale_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            sale_date TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES CUSTOMERS(customer_id),
            FOREIGN KEY (product_id) REFERENCES PRODUCTS(product_id)
        );
        """
    )

    cursor.executemany(
        "INSERT INTO CUSTOMERS (customer_id, name, email, country) VALUES (?, ?, ?, ?)",
        CUSTOMERS,
    )
    cursor.executemany(
        "INSERT INTO CATEGORIES (category_id, category_name) VALUES (?, ?)",
        CATEGORIES,
    )
    cursor.executemany(
        """
        INSERT INTO PRODUCTS (product_id, product_name, category_id, price)
        VALUES (?, ?, ?, ?)
        """,
        PRODUCTS,
    )
    cursor.executemany(
        """
        INSERT INTO SALES (sale_id, customer_id, product_id, quantity, sale_date)
        VALUES (?, ?, ?, ?, ?)
        """,
        SALES,
    )

    conexion.commit()

    if verbose:
        print("Base de datos creada correctamente.")
        print(f"Ubicación: {db_path}")
        print(f"Clientes:   {len(CUSTOMERS)}")
        print(f"Categorías: {len(CATEGORIES)}")
        print(f"Productos:  {len(PRODUCTS)}")
        print(f"Ventas:     {len(SALES)}")

    conexion.close()
    return db_path


if __name__ == "__main__":
    crear_base_de_datos()
