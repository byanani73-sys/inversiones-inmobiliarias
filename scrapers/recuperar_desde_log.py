"""
recuperar_desde_log.py — Recupera teléfonos y nombres capturados en el log
de backfill_telefonos.py que nunca se guardaron en SQLite por interrupción
del proceso.

Lee el log, parsea las líneas con 📞, hace match en SQLite por barrio+pm2
(±5%) y actualiza los registros con match único. Finaliza con sync al Sheet.

Uso:
  python scrapers/recuperar_desde_log.py
  python scrapers/recuperar_desde_log.py --log logs/backfill_log.txt
  python scrapers/recuperar_desde_log.py --dry-run
  python scrapers/recuperar_desde_log.py --sin-sheets
"""

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "zonaprop.db"
DEFAULT_LOG = ROOT / "logs" / "backfill_log.txt"

# ---------------------------------------------------------------------------
# Prefijos argentinos válidos
# ---------------------------------------------------------------------------
AR_VALID_PREFIXES = ("11", "15", "54", "91", "01", "549")

# Regex para parsear líneas del log con 📞
# Ejemplo: "  [   1/1618] Monserrat            | USD 1,250/m² | 📞 91158482096 | Macarena Villambrosa"
LOG_LINE_RE = re.compile(
    r'\[\s*\d+/\d+\]\s+'            # [N/TOTAL] con espacios
    r'(.+?)\s*\|\s*'                # barrio (con padding)
    r'USD\s+([\d,]+)/m²\s*\|\s*'  # precio USD X,XXX/m²
    r'\U0001f4de\s*(\S+)\s*\|\s*'   # 📞 número de teléfono
    r'(.+)'                          # nombre
)


# ---------------------------------------------------------------------------
# Validación de teléfono
# ---------------------------------------------------------------------------
def validar_telefono(raw: str) -> str | None:
    """
    Limpia y valida un número de teléfono argentino.
    Devuelve el número limpio (solo dígitos) o None si es inválido.
    """
    cleaned = re.sub(r'\D', '', raw)
    if len(cleaned) < 10 or len(cleaned) > 13:
        return None
    if not any(cleaned.startswith(p) for p in AR_VALID_PREFIXES):
        return None
    return cleaned


# ---------------------------------------------------------------------------
# Parseo del log
# ---------------------------------------------------------------------------
def parsear_log(log_path: Path) -> list[dict]:
    """
    Lee el archivo de log y extrae todas las entradas con teléfono válido.
    Devuelve lista de {barrio, pm2, telefono, nombre}.
    """
    entradas = []
    invalidas = 0
    texto = log_path.read_text(encoding="utf-8", errors="replace")

    for line in texto.splitlines():
        m = LOG_LINE_RE.search(line)
        if not m:
            continue

        barrio_raw, pm2_raw, tel_raw, nombre_raw = m.groups()

        barrio  = barrio_raw.strip()
        pm2_str = pm2_raw.replace(",", "")
        nombre  = nombre_raw.strip()
        if nombre == "-":
            nombre = None

        try:
            pm2 = int(pm2_str)
        except ValueError:
            continue

        tel = validar_telefono(tel_raw.strip())
        if tel is None:
            invalidas += 1
            continue

        entradas.append({
            "barrio":   barrio,
            "pm2":      pm2,
            "telefono": tel,
            "nombre":   nombre,
        })

    if invalidas:
        print(f"  ({invalidas} números descartados por prefijo/longitud inválida)")

    return entradas


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------
def buscar_en_db(conn: sqlite3.Connection, barrio: str, pm2: int) -> list[str]:
    """
    Busca id_zonaprop en propiedades con match de barrio y pm2.

    Estrategia en dos pasos:
    1. Busca candidatos dentro de ±5%.
    2. Si hay más de uno, intenta reducir con ±1% (el log muestra pm2 redondeado,
       la DB guarda el float exacto — ±1% descarta vecinos que solo entraron por
       la tolerancia amplia).
    3. Devuelve la lista final: 0 = no encontrado, 1 = match único, >1 = ambiguo.
    """
    def _query(lo, hi):
        rows = conn.execute(
            """SELECT id_zonaprop, precio_por_m2_tasable FROM propiedades
               WHERE barrio_simple = ?
                 AND precio_por_m2_tasable BETWEEN ? AND ?
                 AND telefono_publicante IS NULL
                 AND activa = 1""",
            (barrio, lo, hi),
        ).fetchall()
        return rows

    amplio = _query(pm2 * 0.95, pm2 * 1.05)
    if len(amplio) <= 1:
        return [r[0] for r in amplio]

    # Más de un candidato → intentar reducir con ±1%
    tight = _query(pm2 * 0.99, pm2 * 1.01)
    if len(tight) == 1:
        return [tight[0][0]]

    # Siguen siendo varios → ambiguo
    return [r[0] for r in amplio]


def actualizar_en_db(
    conn: sqlite3.Connection,
    id_zp: str,
    telefono: str,
    nombre: str | None,
) -> None:
    conn.execute(
        """UPDATE propiedades
           SET telefono_publicante = ?, nombre_publicante = ?
           WHERE id_zonaprop = ?""",
        (telefono, nombre, id_zp),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(log_path: Path, dry_run: bool, sin_sheets: bool) -> None:
    from scrapers.backfill_telefonos import sincronizar_telefonos_sheets
    from scrapers.scraper_diario import connect_sheets, cargar_sheet_state

    print(f"\n{'=' * 65}")
    print(f"RECUPERAR DESDE LOG — {log_path.name}")
    if dry_run:
        print("  [DRY-RUN: no se escribirá nada]")
    print(f"{'=' * 65}\n")

    # -----------------------------------------------------------------------
    # 1. Parsear log
    # -----------------------------------------------------------------------
    print("[1/4] Parseando log...")
    if not log_path.exists():
        print(f"  [!] Archivo no encontrado: {log_path}")
        return
    entradas = parsear_log(log_path)
    print(f"  {len(entradas)} entradas con teléfono válido encontradas en el log")

    if not entradas:
        print("Nada que procesar.")
        return

    # -----------------------------------------------------------------------
    # 2. Conectar Sheet
    # -----------------------------------------------------------------------
    sh = None
    if not sin_sheets:
        print("\n[2/4] Conectando al Sheet...")
        try:
            ws = connect_sheets()
            sh = cargar_sheet_state(ws)
            print(f"  {len(sh['id_to_row'])} IDs cargados del Sheet")
        except Exception as e:
            print(f"  [!] Error Sheet: {e} — continuando sin sync")
    else:
        print("\n[2/4] Sin-sheets: se omite conexión")

    # -----------------------------------------------------------------------
    # 3. Matching en SQLite
    # -----------------------------------------------------------------------
    print(f"\n[3/4] Buscando matches en SQLite...\n")
    conn = sqlite3.connect(DB_PATH)

    matcheados:     list[dict] = []
    ambiguos:       list[dict] = []
    no_encontrados: list[dict] = []

    for e in entradas:
        ids = buscar_en_db(conn, e["barrio"], e["pm2"])

        if len(ids) == 1:
            matcheados.append({
                "id":       ids[0],
                "nombre":   e["nombre"],
                "telefono": e["telefono"],
            })
            tag = "✓ match"
        elif len(ids) == 0:
            no_encontrados.append(e)
            tag = "✗ no encontrado"
        else:
            ambiguos.append(e)
            tag = f"⚠ ambiguo ({len(ids)} candidatos)"

        nom = e["nombre"] or "-"
        print(
            f"  {e['barrio']:<25} pm2={e['pm2']:>6} | "
            f"📞 {e['telefono']:<14} {nom[:25]:<25} → {tag}"
        )

    # Guardar en SQLite
    print()
    if not dry_run and matcheados:
        for r in matcheados:
            actualizar_en_db(conn, r["id"], r["telefono"], r["nombre"])
        conn.commit()
        print(f"  ✓ SQLite: {len(matcheados)} propiedades actualizadas")
    elif dry_run:
        print(f"  [dry-run] SQLite: {len(matcheados)} propiedades a actualizar")
    else:
        print("  SQLite: ningún match único — sin cambios")

    conn.close()

    # -----------------------------------------------------------------------
    # 4. Sync Sheet
    # -----------------------------------------------------------------------
    print(f"\n[4/4] Sincronizando Sheet...")
    if sin_sheets:
        print("  Sheet: omitido (--sin-sheets)")
    elif not sh:
        print("  Sheet: sin conexión, omitido")
    elif not matcheados:
        print("  Sheet: sin datos para sincronizar")
    else:
        sincronizar_telefonos_sheets(sh, matcheados, dry_run=dry_run)

    # -----------------------------------------------------------------------
    # Resumen
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 65}")
    print("RESUMEN")
    print(f"{'=' * 65}")
    print(f"  Entradas en log (tel válido):  {len(entradas)}")
    print(f"  Matcheados (match único):       {len(matcheados)}")
    print(f"  Ambiguos (>1 candidato):        {len(ambiguos)}")
    print(f"  No encontrados (0 candidatos):  {len(no_encontrados)}")

    if ambiguos:
        print()
        print("  Ambiguos:")
        for e in ambiguos:
            print(f"    {e['barrio']:<25} pm2={e['pm2']:>6} | {e['telefono']} | {e['nombre'] or '-'}")

    if no_encontrados:
        print()
        print("  No encontrados:")
        for e in no_encontrados:
            print(f"    {e['barrio']:<25} pm2={e['pm2']:>6} | {e['telefono']} | {e['nombre'] or '-'}")

    print(f"{'=' * 65}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Recupera teléfonos del log de backfill que no se guardaron en SQLite"
    )
    parser.add_argument(
        "--log", type=Path, default=DEFAULT_LOG,
        help=f"Ruta al archivo de log (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="No escribir nada (ni SQLite ni Sheet)",
    )
    parser.add_argument(
        "--sin-sheets", action="store_true",
        help="No sincronizar con Google Sheets",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(
        log_path=args.log,
        dry_run=args.dry_run,
        sin_sheets=args.sin_sheets,
    )
