"""
Identifica y elimina registros de cocheras/garajes de SQLite y Google Sheets.

Uso:
  # Solo mostrar cuántos registros serían eliminados (sin borrar nada):
  python pipeline/limpiar_cocheras.py

  # Ejecutar el borrado real en SQLite y Google Sheets:
  python pipeline/limpiar_cocheras.py --confirmar

Criterio: tipo_propiedad = 'Cochera' (campo extraído del h2 del detalle de ZonaProp).
Los registros scraped antes de agregar este campo tendrán tipo_propiedad = NULL
y no serán afectados.
"""

import argparse
import sqlite3
import time

import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
DB_PATH = "data/zonaprop.db"
CREDENTIALS_PATH = "credentials.json"
SHEET_ID = "13fWYaAwwe9qyVqfb08Qhu_zusak_uVvTQv3-1rhfwDA"
WORKSHEET_NAME = "Hoja 1"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def fetch_cocheras(conn: sqlite3.Connection) -> list[dict]:
    """Devuelve los registros con tipo_propiedad = 'Cochera'."""
    rows = conn.execute(
        """
        SELECT id_zonaprop, direccion, barrio, descripcion,
               ambientes, m2_totales, precio_actual, moneda, tipo_propiedad
        FROM propiedades
        WHERE tipo_propiedad = 'Cochera'
        """
    ).fetchall()

    cols = ["id_zonaprop", "direccion", "barrio", "descripcion",
            "ambientes", "m2_totales", "precio_actual", "moneda", "tipo_propiedad"]

    return [dict(zip(cols, row)) for row in rows]


def borrar_de_sqlite(conn: sqlite3.Connection, ids: list[str]) -> int:
    """Elimina los ids dados de la tabla propiedades. Devuelve cantidad borrada."""
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"DELETE FROM propiedades WHERE id_zonaprop IN ({placeholders})", ids
    )
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
def connect_sheets():
    """Conecta a Google Sheets y devuelve el worksheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    return spreadsheet.worksheet(WORKSHEET_NAME)


def borrar_de_sheets(ws, ids_a_borrar: set[str]) -> int:
    """
    Elimina del Sheet las filas cuyos id_zonaprop estén en ids_a_borrar.
    Borra de abajo hacia arriba para que los índices no se desplacen.
    Devuelve cantidad de filas eliminadas.
    """
    all_values = ws.get_all_values()  # incluye la fila de headers en index 0
    if len(all_values) <= 1:
        return 0

    # Columna A = id_zonaprop (índice 0)
    # all_values[0] = headers (fila 1 del sheet)
    # all_values[i] = fila i+1 del sheet  →  fila gspread = i+1
    rows_to_delete = []
    for i, row in enumerate(all_values):
        if i == 0:
            continue  # saltar headers
        id_zp = row[0] if row else ""
        if id_zp in ids_a_borrar:
            sheet_row_number = i + 1  # gspread usa base-1
            rows_to_delete.append(sheet_row_number)

    if not rows_to_delete:
        return 0

    # Borrar de abajo hacia arriba para preservar índices
    rows_to_delete.sort(reverse=True)
    deleted = 0
    for row_num in rows_to_delete:
        ws.delete_rows(row_num)
        deleted += 1
        # Pequeña pausa para no saturar la API
        time.sleep(0.3)

    return deleted


# ---------------------------------------------------------------------------
# Helpers de presentacion
# ---------------------------------------------------------------------------
def motivo(row: dict) -> str:
    return f"tipo_propiedad = '{row['tipo_propiedad']}'"


def mostrar_ejemplos(cocheras: list[dict], n: int = 5) -> None:
    print(f"\nEjemplos (mostrando {min(n, len(cocheras))} de {len(cocheras)}):")
    print("-" * 80)
    for row in cocheras[:n]:
        precio = f"{row['moneda']} {row['precio_actual']:,}" if row["precio_actual"] else "-"
        print(f"  ID:        {row['id_zonaprop']}")
        print(f"  Dirección: {row['direccion'] or '-'}")
        print(f"  Barrio:    {row['barrio'] or '-'}")
        print(f"  m2:        {row['m2_totales']}  |  ambientes: {row['ambientes']}  |  precio: {precio}")
        print(f"  Motivo:    {motivo(row)}")
        desc = (row["descripcion"] or "")[:120]
        if desc:
            print(f"  Desc:      {desc}...")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Limpieza de cocheras en SQLite y Google Sheets")
    parser.add_argument(
        "--confirmar",
        action="store_true",
        help="Ejecuta el borrado real. Sin este flag solo muestra el diagnóstico.",
    )
    args = parser.parse_args()

    # --- Diagnóstico ---
    conn = sqlite3.connect(DB_PATH)
    cocheras = fetch_cocheras(conn)
    ids = [r["id_zonaprop"] for r in cocheras]

    total_db = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]
    print(f"\nRegistros totales en SQLite: {total_db}")
    print(f"Identificados como cocheras: {len(cocheras)}")

    if not cocheras:
        print("\nNo se encontraron cocheras. Nada que hacer.")
        conn.close()
        return

    mostrar_ejemplos(cocheras)

    if not args.confirmar:
        print("=" * 60)
        print("MODO SIMULACIÓN — no se borró nada.")
        print("Para borrar de verdad, corré con --confirmar")
        print("=" * 60)
        conn.close()
        return

    # --- Borrado real ---
    print("=" * 60)
    print("EJECUTANDO BORRADO REAL...")
    print("=" * 60)

    # 1. Borrar de SQLite
    print(f"\n[1/2] Borrando {len(ids)} registros de SQLite...")
    borrados_db = borrar_de_sqlite(conn, ids)
    conn.close()
    print(f"      ✓ {borrados_db} registros eliminados de SQLite")

    # 2. Borrar de Google Sheets
    print(f"\n[2/2] Borrando filas del Google Sheet...")
    print("      Conectando...")
    ws = connect_sheets()
    ids_set = set(ids)
    borrados_sheet = borrar_de_sheets(ws, ids_set)
    print(f"      ✓ {borrados_sheet} filas eliminadas del Sheet")

    print(f"\n{'=' * 60}")
    print("LIMPIEZA COMPLETA")
    print(f"  SQLite:        -{borrados_db} registros")
    print(f"  Google Sheets: -{borrados_sheet} filas")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    print("=" * 60)


if __name__ == "__main__":
    main()
