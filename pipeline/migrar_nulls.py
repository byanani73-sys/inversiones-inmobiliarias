"""
migrar_nulls.py — Migración one-shot de valores NULL en SQLite y Google Sheet.

Acciones:
  1. SQLite: actualiza tipo_operacion y tipo_propiedad donde sean NULL (parsea slug).
  2. SQLite: pone activa=1 donde activa sea NULL.
  3. Sheet:  actualiza tipo_operacion y tipo_propiedad donde estén vacíos (parsea slug).
  4. Sheet:  pone activa=1 donde la celda esté vacía.

Uso:
  python pipeline/migrar_nulls.py
  python pipeline/migrar_nulls.py --dry-run
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scraper_diario import (
    DB_PATH, connect_sheets, cargar_sheet_state, col_letter,
)
from pipeline.extraer_tipo_url import parsear_slug


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def migrar_sqlite(dry_run: bool) -> None:
    print("\n=== SQLite ===")
    conn = sqlite3.connect(DB_PATH)

    # 1. tipo_operacion / tipo_propiedad NULL
    filas = conn.execute(
        "SELECT id_zonaprop, url FROM propiedades "
        "WHERE (tipo_propiedad IS NULL OR tipo_operacion IS NULL) AND url IS NOT NULL"
    ).fetchall()

    updates_tipo = []
    for id_zp, url in filas:
        ops, tipo = parsear_slug(url)
        updates_tipo.append((ops, tipo, id_zp))

    if updates_tipo:
        if not dry_run:
            conn.executemany(
                "UPDATE propiedades SET tipo_operacion = ?, tipo_propiedad = ? WHERE id_zonaprop = ?",
                updates_tipo,
            )
            conn.commit()
        print(f"  tipo_operacion/tipo_propiedad: {len(updates_tipo)} fila(s) {'actualizadas' if not dry_run else 'a actualizar (dry-run)'}")
    else:
        print("  tipo_operacion/tipo_propiedad: sin NULLs — nada que hacer")

    # 2. activa NULL → 1
    n_activa = conn.execute(
        "SELECT COUNT(*) FROM propiedades WHERE activa IS NULL"
    ).fetchone()[0]

    if n_activa:
        if not dry_run:
            conn.execute("UPDATE propiedades SET activa = 1 WHERE activa IS NULL")
            conn.commit()
        print(f"  activa: {n_activa} fila(s) {'puestas a 1' if not dry_run else 'a poner en 1 (dry-run)'}")
    else:
        print("  activa: sin NULLs — nada que hacer")

    conn.close()


# ---------------------------------------------------------------------------
# Google Sheet
# ---------------------------------------------------------------------------
def migrar_sheets(dry_run: bool) -> None:
    print("\n=== Google Sheet ===")
    ws = connect_sheets()
    all_values = ws.get_all_values()

    if not all_values:
        print("  Sheet vacío — nada que hacer")
        return

    headers = all_values[0]
    h_idx   = {h: i for i, h in enumerate(headers)}

    url_col   = h_idx.get("url")
    tipo_op_i = h_idx.get("tipo_operacion")
    tipo_pr_i = h_idx.get("tipo_propiedad")
    activa_i  = h_idx.get("activa")

    if url_col is None:
        print("  [!] Columna 'url' no encontrada en el Sheet")
        return

    updates_tipo   = []   # {range, values}
    updates_activa = []

    for r_num, row in enumerate(all_values, 1):
        if r_num == 1:
            continue  # cabecera

        url = row[url_col] if len(row) > url_col else ""

        # tipo_operacion / tipo_propiedad vacíos
        if tipo_op_i is not None and tipo_pr_i is not None:
            cur_op   = row[tipo_op_i] if len(row) > tipo_op_i else ""
            cur_tipo = row[tipo_pr_i] if len(row) > tipo_pr_i else ""
            if not cur_op or not cur_tipo:
                ops, tipo = parsear_slug(url)
                if not cur_op and ops:
                    updates_tipo.append({
                        "range":  f"{col_letter(tipo_op_i)}{r_num}",
                        "values": [[ops]],
                    })
                if not cur_tipo and tipo:
                    updates_tipo.append({
                        "range":  f"{col_letter(tipo_pr_i)}{r_num}",
                        "values": [[tipo]],
                    })

        # activa vacío → 1
        if activa_i is not None:
            cur_activa = row[activa_i] if len(row) > activa_i else ""
            if not cur_activa:
                updates_activa.append({
                    "range":  f"{col_letter(activa_i)}{r_num}",
                    "values": [[1]],
                })

    # Escribir
    BATCH = 500  # gspread batch_update acepta hasta 1000 rangos

    if updates_tipo:
        print(f"  tipo_operacion/tipo_propiedad: {len(updates_tipo)} celda(s) a actualizar")
        if not dry_run:
            for i in range(0, len(updates_tipo), BATCH):
                ws.batch_update(updates_tipo[i:i+BATCH])
                time.sleep(1)
            print(f"  ✓ tipo actualizado")
    else:
        print("  tipo_operacion/tipo_propiedad: sin vacíos — nada que hacer")

    if updates_activa:
        print(f"  activa: {len(updates_activa)} celda(s) a poner en 1")
        if not dry_run:
            for i in range(0, len(updates_activa), BATCH):
                ws.batch_update(updates_activa[i:i+BATCH])
                time.sleep(1)
            print(f"  ✓ activa actualizado")
    else:
        print("  activa: sin vacíos — nada que hacer")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(dry_run: bool) -> None:
    if dry_run:
        print("=== DRY-RUN: no se escribirá nada ===")

    migrar_sqlite(dry_run)
    migrar_sheets(dry_run)

    print("\nListo.")


def parse_args():
    parser = argparse.ArgumentParser(description="Migración one-shot de NULLs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra qué se haría, sin escribir")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(dry_run=args.dry_run)
