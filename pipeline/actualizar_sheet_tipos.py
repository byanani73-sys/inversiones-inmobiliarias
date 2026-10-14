"""
Agrega (si no existen) las columnas tipo_operacion y tipo_propiedad al Google Sheet
y las puebla con los valores ya calculados en SQLite.

Uso:
  python pipeline/actualizar_sheet_tipos.py
"""

import sqlite3
import time

import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
DB_PATH         = "data/zonaprop.db"
CREDENTIALS_PATH = "credentials.json"
SHEET_ID        = "13fWYaAwwe9qyVqfb08Qhu_zusak_uVvTQv3-1rhfwDA"
WORKSHEET_NAME  = "Hoja 1"


def connect_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


def col_letter(idx: int) -> str:
    """Índice 0-based → letra de columna (A, B, ..., Z, AA, ...)."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def main():
    # -----------------------------------------------------------------------
    # 1. Leer tipos desde SQLite
    # -----------------------------------------------------------------------
    print("Leyendo tipos desde SQLite...")
    conn = sqlite3.connect(DB_PATH)
    tipos = {
        row[0]: (row[1] or "", row[2] or "")
        for row in conn.execute(
            "SELECT id_zonaprop, tipo_operacion, tipo_propiedad FROM propiedades"
        ).fetchall()
    }
    conn.close()
    print(f"  {len(tipos)} registros con tipos en SQLite")

    # -----------------------------------------------------------------------
    # 2. Conectar al Sheet y leer contenido actual
    # -----------------------------------------------------------------------
    print("\nConectando al Sheet...")
    ws = connect_sheets()
    all_values = ws.get_all_values()

    if not all_values:
        print("El Sheet está vacío.")
        return

    headers = list(all_values[0])
    n_rows  = len(all_values)
    print(f"  {n_rows - 1} filas de datos, {len(headers)} columnas actuales")
    print(f"  Últimas columnas: {headers[-4:]}")

    # -----------------------------------------------------------------------
    # 3. Agregar headers si no existen
    # -----------------------------------------------------------------------
    for col_name in ("tipo_operacion", "tipo_propiedad"):
        if col_name not in headers:
            next_col = len(headers) + 1                    # base-1 para gspread
            ws.update_cell(1, next_col, col_name)
            headers.append(col_name)
            print(f"  → Header '{col_name}' agregado en columna {col_letter(next_col - 1)}")
            time.sleep(0.5)
        else:
            print(f"  → Header '{col_name}' ya existía en col {col_letter(headers.index(col_name))}")

    idx_op   = headers.index("tipo_operacion")
    idx_prop = headers.index("tipo_propiedad")
    idx_id   = headers.index("id_zonaprop")

    # -----------------------------------------------------------------------
    # 4. Construir las dos columnas de valores (una entrada por fila de datos)
    # -----------------------------------------------------------------------
    col_op_values   = []
    col_prop_values = []
    sin_match       = 0

    for row in all_values[1:]:                             # saltar header
        id_zp = row[idx_id] if len(row) > idx_id else ""
        if id_zp and id_zp in tipos:
            op, prop = tipos[id_zp]
            col_op_values.append([op])
            col_prop_values.append([prop])
        else:
            col_op_values.append([""])
            col_prop_values.append([""])
            sin_match += 1

    if sin_match:
        print(f"  ⚠ {sin_match} filas del Sheet sin match en SQLite (se dejarán vacías)")

    # -----------------------------------------------------------------------
    # 5. Escribir las dos columnas en batch (una llamada API por columna)
    # -----------------------------------------------------------------------
    data_rows = n_rows - 1
    letter_op   = col_letter(idx_op)
    letter_prop = col_letter(idx_prop)

    print(f"\nEscribiendo tipo_operacion en {letter_op}2:{letter_op}{n_rows}...")
    ws.update(
        values=col_op_values,
        range_name=f"{letter_op}2:{letter_op}{n_rows}",
    )
    time.sleep(1)

    print(f"Escribiendo tipo_propiedad en {letter_prop}2:{letter_prop}{n_rows}...")
    ws.update(
        values=col_prop_values,
        range_name=f"{letter_prop}2:{letter_prop}{n_rows}",
    )

    # -----------------------------------------------------------------------
    # 6. Resumen
    # -----------------------------------------------------------------------
    actualizados = data_rows - sin_match
    print(f"\n{'=' * 50}")
    print("ACTUALIZACIÓN COMPLETA")
    print(f"  Filas actualizadas: {actualizados}")
    print(f"  Sin match:          {sin_match}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    print("=" * 50)


if __name__ == "__main__":
    main()
