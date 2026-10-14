"""
Exporta registros de data/zonaprop.db a Google Sheets.

Uso:
  python pipeline/migrar_a_sheets.py

Requiere:
  - credentials.json en la raiz del proyecto (service account de Google)
  - El sheet debe estar compartido con el email del service account
"""

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
BATCH_SIZE = 500

# Headers exactos que debe tener la primera fila
HEADERS = [
    "id_zonaprop",
    "barrio_simple",
    "orden_barrio",
    "direccion",
    "precio_actual",
    "m2_totales",
    "m2_cubiertos",
    "m2_descubiertos",
    "m2_tasables",
    "precio_por_m2_tasable",
    "antiguedad",
    "url",
    "estado",
    "nota",
    "fecha_primera_vez",
    "es_nuevo",
    "bajo_precio",
    "precio_anterior",
]

# Columnas que se leen de SQLite (en el mismo orden que HEADERS[:15])
SQL_COLUMNS = [
    "id_zonaprop",
    "barrio_simple",
    "orden_barrio",
    "direccion",
    "precio_actual",
    "m2_totales",
    "m2_cubiertos",
    "m2_descubiertos",
    "m2_tasables",
    "precio_por_m2_tasable",
    "antiguedad",
    "url",
]

QUERY = f"""
SELECT {', '.join(SQL_COLUMNS)}, fecha_primera_vez
FROM propiedades
WHERE barrio_simple IS NOT NULL
  AND precio_por_m2_tasable IS NOT NULL
ORDER BY orden_barrio ASC, precio_por_m2_tasable ASC
"""

# Valores por defecto para columnas extras
DEFAULTS = {
    "estado": "Sin llamar",
    "nota": "",
    "es_nuevo": False,
    "bajo_precio": False,
    "precio_anterior": "",
}


# ---------------------------------------------------------------------------
# Funciones
# ---------------------------------------------------------------------------
def fetch_data():
    """Lee los registros de SQLite y los devuelve como lista de listas."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(QUERY).fetchall()
    conn.close()

    result = []
    for row in rows:
        # row tiene las SQL_COLUMNS + fecha_primera_vez (13 valores)
        values = list(row)

        # Reemplazar None por "" para Google Sheets
        values = [v if v is not None else "" for v in values]

        # Insertar columnas extras en las posiciones correctas:
        # Despues de url (index 11) van: estado (12), nota (13),
        # fecha_primera_vez ya esta al final (index 12 del row original)
        # Reorganizar: SQL_COLUMNS(0-11) + estado + nota + fecha_primera_vez + es_nuevo + bajo_precio + precio_anterior
        sql_fields = values[:12]              # id..url
        fecha = values[12]                     # fecha_primera_vez

        final_row = sql_fields + [
            DEFAULTS["estado"],                # estado
            DEFAULTS["nota"],                  # nota
            fecha,                             # fecha_primera_vez
            DEFAULTS["es_nuevo"],              # es_nuevo
            DEFAULTS["bajo_precio"],           # bajo_precio
            DEFAULTS["precio_anterior"],       # precio_anterior
        ]
        result.append(final_row)

    return result


def connect_sheets():
    """Conecta a Google Sheets con gspread y devuelve el worksheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    return worksheet


def ensure_headers(ws):
    """Verifica que la fila 1 tenga los headers correctos. Si no, los escribe."""
    existing = ws.row_values(1)
    if existing == HEADERS:
        print("  Headers OK")
        return False
    else:
        print(f"  Headers actuales: {existing[:5]}... ({len(existing)} cols)")
        print(f"  Escribiendo headers correctos ({len(HEADERS)} cols)")
        ws.update(values=[HEADERS], range_name="A1")
        return True


def clear_data(ws):
    """Limpia todo excepto la fila de headers."""
    # Obtener cantidad de filas actuales
    all_values = ws.get_all_values()
    total_rows = len(all_values)
    if total_rows > 1:
        # Limpiar desde fila 2 hasta el final
        last_col = chr(ord("A") + len(HEADERS) - 1)  # max 26 cols -> suficiente
        range_str = f"A2:{last_col}{total_rows}"
        ws.batch_clear([range_str])
        print(f"  Limpiadas {total_rows - 1} filas de datos anteriores")
    else:
        print("  Hoja vacia (solo headers)")


def ensure_rows(ws, needed):
    """Expande la hoja si no tiene suficientes filas."""
    current = ws.row_count
    if current < needed:
        ws.add_rows(needed - current)
        print(f"  Expandida de {current} a {needed} filas")
    else:
        print(f"  Filas suficientes ({current} >= {needed})")


def write_batches(ws, rows):
    """Escribe filas en lotes de BATCH_SIZE."""
    total = len(rows)
    written = 0

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        start_row = i + 2  # fila 1 = headers, datos desde fila 2
        last_col = chr(ord("A") + len(HEADERS) - 1)
        end_row = start_row + len(batch) - 1
        range_str = f"A{start_row}:{last_col}{end_row}"

        ws.update(values=batch, range_name=range_str)
        written += len(batch)
        print(f"  Lote {i // BATCH_SIZE + 1}: filas {start_row}-{end_row} ({written}/{total})")

        # Pausa entre lotes para no exceder rate limits
        if i + BATCH_SIZE < total:
            time.sleep(1)

    return written


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()

    print("Leyendo datos de SQLite...")
    rows = fetch_data()
    print(f"  {len(rows)} registros a exportar")

    if not rows:
        print("No hay registros para exportar.")
        return

    print("\nConectando a Google Sheets...")
    ws = connect_sheets()
    print("  Conectado")

    print("\nVerificando headers...")
    ensure_headers(ws)

    print("\nLimpiando datos anteriores...")
    clear_data(ws)

    print("\nVerificando capacidad de la hoja...")
    ensure_rows(ws, len(rows) + 1)  # +1 por la fila de headers

    print(f"\nEscribiendo {len(rows)} registros en lotes de {BATCH_SIZE}...")
    written = write_batches(ws, rows)

    elapsed = time.time() - t0
    print(f"\n{'=' * 50}")
    print("EXPORTACION COMPLETA")
    print(f"  Registros exportados: {written}")
    print(f"  Tiempo total:         {elapsed:.1f} segundos")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
