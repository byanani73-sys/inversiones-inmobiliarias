"""
Extrae tipo_operacion y tipo_propiedad del slug de la URL de cada propiedad
y los guarda en SQLite. No requiere Playwright ni acceso a internet.

Formato de URL:
  https://www.zonaprop.com.ar/propiedades/clasificado/XXXXXXXX-...
  Slug de 8 caracteres:
    pos 0-1 : operación  (ve=Venta, al=Alquiler, te=Alquiler Temporario)
    pos 2-3 : "cl"       (siempre fijo, ignorar)
    pos 4-7 : tipo       (appa=Departamento, phpa=PH, capa=Casa, ...)

Uso:
  python pipeline/extraer_tipo_url.py
  python pipeline/extraer_tipo_url.py --dry-run
"""

import argparse
import re
import sqlite3

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
DB_PATH = "data/zonaprop.db"

TIPO_OPERACION = {
    "ve": "Venta",
    "al": "Alquiler",
    "te": "Alquiler Temporario",
}

# Clave: posiciones 4-7 del slug (4 caracteres)
TIPO_PROPIEDAD = {
    "appa": "Departamento",
    "phpa": "PH",
    "copa": "Consultorio",
    "capa": "Casa",
    "ocpa": "Oficina",
    "fcpa": "Fondo de Comercio",
    "gapa": "Cochera",
    "trpa": "Terreno",
    "edpa": "Edificio",
    "lcpa": "Local Comercial",
    "bgpa": "Galpón",
    "bnpa": "Bóveda",
    "depa": "Depósito",
    "htpa": "Hotel",
}


# ---------------------------------------------------------------------------
# Parseo del slug
# ---------------------------------------------------------------------------
def parsear_slug(url: str) -> tuple[str | None, str | None]:
    """
    Extrae (tipo_operacion, tipo_propiedad) del slug de la URL.

    Ejemplo:
      'https://.../clasificado/veclappa-departamento-3-ambientes...'
      slug     = 'veclappa'
      ops_code = 've'  → 'Venta'
      tip_code = 'appa' → 'Departamento'

    Devuelve (None, None) si la URL no tiene el formato esperado.
    Si el código de tipo no está en la tabla devuelve tipo = 'Otro'.
    """
    if not url:
        return None, None

    m = re.search(r"/clasificado/([a-z]{8})-", url)
    if not m:
        return None, None

    slug     = m.group(1)
    ops_code = slug[0:2]   # posiciones 1-2: operación
    tip_code = slug[4:8]   # posiciones 5-8: tipo de propiedad

    ops  = TIPO_OPERACION.get(ops_code)
    tipo = TIPO_PROPIEDAD.get(tip_code, "Otro")

    return ops, tipo


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    """Agrega columnas nuevas si no existen."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}
    for col in ("tipo_operacion", "tipo_propiedad"):
        if col not in cols:
            conn.execute(f"ALTER TABLE propiedades ADD COLUMN {col} TEXT")
            print(f"  → Columna {col} agregada")
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(dry_run: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)

    print("Verificando schema...")
    init_db(conn)

    rows = conn.execute(
        "SELECT id_zonaprop, url FROM propiedades WHERE url IS NOT NULL"
    ).fetchall()

    total = len(rows)
    print(f"Registros a procesar: {total}\n")

    updates         = []
    sin_match       = 0
    stats_operacion = {}
    stats_tipo      = {}

    for id_zp, url in rows:
        ops, tipo = parsear_slug(url)

        if ops is None:   # URL sin slug reconocible
            sin_match += 1
            continue

        stats_operacion[ops]  = stats_operacion.get(ops, 0) + 1
        stats_tipo[tipo]      = stats_tipo.get(tipo, 0) + 1
        updates.append((ops, tipo, id_zp))

    if not dry_run:
        conn.executemany(
            "UPDATE propiedades SET tipo_operacion = ?, tipo_propiedad = ? WHERE id_zonaprop = ?",
            updates,
        )
        conn.commit()
        print(f"Escritos {len(updates)} registros en la DB.")
    else:
        print(f"[dry-run] Se escribirían {len(updates)} registros.")

    conn.close()

    print()
    print("=" * 45)
    print("TIPO DE OPERACIÓN")
    print("=" * 45)
    for k, v in sorted(stats_operacion.items(), key=lambda x: -x[1]):
        bar = "█" * (v * 30 // max(stats_operacion.values()))
        print(f"  {k:<25} {v:>5}  {bar}")

    print()
    print("=" * 45)
    print("TIPO DE PROPIEDAD")
    print("=" * 45)
    for k, v in sorted(stats_tipo.items(), key=lambda x: -x[1]):
        bar = "█" * (v * 30 // max(stats_tipo.values()))
        print(f"  {k:<25} {v:>5}  {bar}")

    if sin_match:
        print()
        print(f"  Sin match (slug no reconocible): {sin_match}")

    print()
    print(f"Total procesados: {len(updates) + sin_match} / {total}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extrae tipo_operacion y tipo_propiedad del slug de la URL"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Solo muestra la distribución sin escribir en la DB",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(dry_run=args.dry_run)
