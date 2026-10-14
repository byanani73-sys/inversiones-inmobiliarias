"""
Agrega la columna 'excluida' al Google Sheet si no existe,
y la rellena con FALSE en todos los registros existentes.

No modifica SQLite — la columna solo vive en el Sheet y
el frontend la escribe via guardarCambio(id, 'excluida', 'TRUE'/'FALSE').

Uso:
  python pipeline/agregar_columna_excluida.py
"""

import time

import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
CREDENTIALS_PATH = "credentials.json"
SHEET_ID         = "13fWYaAwwe9qyVqfb08Qhu_zusak_uVvTQv3-1rhfwDA"
WORKSHEET_NAME   = "Hoja 1"
COL_NAME         = "excluida"


def col_letter(idx: int) -> str:
    """Índice 0-based → letra de columna (A, B, ..., Z, AA, ...)."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def connect_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


def main():
    print("Conectando al Sheet...")
    ws = connect_sheets()
    all_values = ws.get_all_values()

    if not all_values:
        print("El Sheet está vacío.")
        return

    headers = list(all_values[0])
    n_rows  = len(all_values)
    print(f"  {n_rows - 1} filas de datos, {len(headers)} columnas actuales")

    # -----------------------------------------------------------------------
    # 1. Agregar header si no existe
    # -----------------------------------------------------------------------
    if COL_NAME in headers:
        col_idx = headers.index(COL_NAME)
        print(f"  → Columna '{COL_NAME}' ya existe en {col_letter(col_idx)} — no se modifica el header")
    else:
        col_idx = len(headers)          # 0-based
        ws.update_cell(1, col_idx + 1, COL_NAME)
        headers.append(COL_NAME)
        print(f"  → Header '{COL_NAME}' agregado en columna {col_letter(col_idx)}")
        time.sleep(0.8)

    # -----------------------------------------------------------------------
    # 2. Rellenar con FALSE donde esté vacío
    # -----------------------------------------------------------------------
    letter = col_letter(col_idx)
    data_rows = n_rows - 1

    # Leer la columna actual para no sobreescribir valores ya seteados
    col_values = ws.col_values(col_idx + 1)  # base-1, incluye header
    existing_data = col_values[1:]           # saltar header

    nuevos = []
    ya_tienen = 0
    for val in existing_data:
        if val.strip():
            nuevos.append([val])            # ya tiene valor, lo conserva
            ya_tienen += 1
        else:
            nuevos.append(["FALSE"])

    print(f"  Filas con valor existente: {ya_tienen}")
    print(f"  Filas a escribir FALSE:    {data_rows - ya_tienen}")

    if data_rows - ya_tienen > 0:
        print(f"\nEscribiendo en {letter}2:{letter}{n_rows}...")
        ws.update(
            values=nuevos,
            range_name=f"{letter}2:{letter}{n_rows}",
        )
        print("  ✓ Listo")
    else:
        print("  Todas las filas ya tenían valor — nada que escribir")

    print(f"\n{'=' * 50}")
    print("COLUMNA 'excluida' LISTA")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    print("=" * 50)


if __name__ == "__main__":
    main()
