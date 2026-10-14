"""
Elimina de SQLite y Google Sheets todos los registros donde tipo_operacion != 'Venta'.

Uso:
  python pipeline/limpiar_no_ventas.py              # solo diagnóstico, no borra nada
  python pipeline/limpiar_no_ventas.py --confirmar  # borrado real
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
def fetch_no_ventas(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id_zonaprop, tipo_operacion, tipo_propiedad, barrio, precio_actual, moneda
        FROM propiedades
        WHERE tipo_operacion != 'Venta'
           OR tipo_operacion IS NULL
        ORDER BY tipo_operacion, tipo_propiedad
        """
    ).fetchall()
    cols = ["id_zonaprop", "tipo_operacion", "tipo_propiedad", "barrio", "precio_actual", "moneda"]
    return [dict(zip(cols, r)) for r in rows]


def borrar_de_sqlite(conn: sqlite3.Connection, ids: list[str]) -> int:
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
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


def borrar_de_sheets(ws, ids_a_borrar: set[str]) -> int:
    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        return 0

    rows_to_delete = []
    for i, row in enumerate(all_values):
        if i == 0:
            continue
        if row and row[0] in ids_a_borrar:
            rows_to_delete.append(i + 1)  # gspread base-1

    if not rows_to_delete:
        return 0

    rows_to_delete.sort(reverse=True)
    for row_num in rows_to_delete:
        ws.delete_rows(row_num)
        time.sleep(0.3)

    return len(rows_to_delete)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Elimina registros que no son ventas")
    parser.add_argument("--confirmar", action="store_true",
                        help="Ejecuta el borrado real. Sin este flag solo muestra diagnóstico.")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    no_ventas = fetch_no_ventas(conn)
    ids = [r["id_zonaprop"] for r in no_ventas]

    total_db = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]
    solo_ventas = total_db - len(no_ventas)

    print(f"\nRegistros totales en SQLite:  {total_db}")
    print(f"Ventas (quedan):              {solo_ventas}")
    print(f"No-ventas (a eliminar):       {len(no_ventas)}")

    # Desglose por tipo_operacion
    print()
    print("=" * 48)
    print("DESGLOSE POR TIPO DE OPERACIÓN")
    print("=" * 48)
    conteo_ops = {}
    for r in no_ventas:
        k = r["tipo_operacion"] or "NULL"
        conteo_ops[k] = conteo_ops.get(k, 0) + 1
    for k, v in sorted(conteo_ops.items(), key=lambda x: -x[1]):
        print(f"  {k:<28} {v:>5}")

    # Desglose por tipo_propiedad dentro de los no-ventas
    print()
    print("=" * 48)
    print("DESGLOSE POR TIPO DE PROPIEDAD (no-ventas)")
    print("=" * 48)
    conteo_tipo = {}
    for r in no_ventas:
        k = r["tipo_propiedad"] or "NULL"
        conteo_tipo[k] = conteo_tipo.get(k, 0) + 1
    for k, v in sorted(conteo_tipo.items(), key=lambda x: -x[1]):
        print(f"  {k:<28} {v:>5}")

    if not args.confirmar:
        print()
        print("=" * 48)
        print("MODO SIMULACIÓN — no se borró nada.")
        print("Para borrar, corré con --confirmar")
        print("=" * 48)
        conn.close()
        return

    # --- Borrado real ---
    print()
    print("=" * 48)
    print("EJECUTANDO BORRADO REAL...")
    print("=" * 48)

    print(f"\n[1/2] Borrando {len(ids)} registros de SQLite...")
    borrados_db = borrar_de_sqlite(conn, ids)
    conn.close()
    print(f"      ✓ {borrados_db} registros eliminados de SQLite")

    print(f"\n[2/2] Borrando filas del Google Sheet...")
    print("      Conectando...")
    ws = connect_sheets()
    borrados_sheet = borrar_de_sheets(ws, set(ids))
    print(f"      ✓ {borrados_sheet} filas eliminadas del Sheet")

    print(f"\n{'=' * 48}")
    print("LIMPIEZA COMPLETA")
    print(f"  SQLite:        -{borrados_db} registros")
    print(f"  Google Sheets: -{borrados_sheet} filas")
    print(f"  Quedan en DB:  {solo_ventas} registros (solo ventas)")
    print("=" * 48)


if __name__ == "__main__":
    main()
