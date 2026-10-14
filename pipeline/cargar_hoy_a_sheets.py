"""
cargar_hoy_a_sheets.py — Sube al Sheet las propiedades con es_nuevo=1 en SQLite.

Uso:
    python pipeline/cargar_hoy_a_sheets.py

Comportamiento:
  - Toma todas las filas con es_nuevo=1 de la DB local.
  - Llama a sincronizar_sheets() que:
      * Si el ID ya existe en el Sheet → actualiza solo precio/es_nuevo (no toca estado ni nota).
      * Si el ID es nuevo → lo agrega al final con estado='Sin llamar'.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scraper_diario import DB_PATH, sincronizar_sheets

COLUMNAS = [
    "id_zonaprop", "url", "fecha_primera_vez", "fecha_actualizacion",
    "precio_actual", "moneda", "expensas",
    "m2_totales", "m2_cubiertos", "m2_descubiertos", "m2_tasables",
    "precio_por_m2_total", "precio_por_m2_tasable",
    "ambientes", "dormitorios", "banos", "toilettes", "cocheras",
    "antiguedad", "disposicion", "orientacion", "luminosidad",
    "direccion", "barrio", "barrio_simple", "orden_barrio",
    "descripcion", "tipo_operacion", "tipo_propiedad",
    "es_nuevo", "activa", "bajo_precio", "precio_anterior",
]


def cargar_nuevos_de_db() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cols_existentes = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}
    cols = [c for c in COLUMNAS if c in cols_existentes]

    sql = f"SELECT {', '.join(cols)} FROM propiedades WHERE es_nuevo = 1"
    rows = conn.execute(sql).fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append(dict(zip(cols, row)))
    return result


if __name__ == "__main__":
    insertados = cargar_nuevos_de_db()

    if not insertados:
        print("No hay propiedades con es_nuevo=1 en la DB. Nada que subir.")
        sys.exit(0)

    print(f"Encontradas {len(insertados)} propiedades con es_nuevo=1")
    sincronizar_sheets(insertados, actualizados=[])
    print("\nListo.")
