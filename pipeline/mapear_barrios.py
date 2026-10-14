"""
Mapea el campo barrio (texto libre de ZonaProp) a barrio_simple + orden_barrio.

Agrega dos columnas a la tabla propiedades:
  barrio_simple  TEXT    - nombre normalizado del barrio
  orden_barrio   INTEGER - orden de prioridad (1 = Palermo, 32 = Otros)

Actualiza la VIEW propiedades_ordenadas para ordenar por orden_barrio.

Uso:
  python pipeline/mapear_barrios.py
"""

import sqlite3
from collections import Counter

DB_PATH = "data/zonaprop.db"

# ---------------------------------------------------------------------------
# Mapeo: barrio_simple -> lista de valores posibles en el campo barrio
# ---------------------------------------------------------------------------
MAPEO_BARRIOS = {
    "Palermo":              ["Palermo", "Palermo Chico", "Palermo Hollywood", "Palermo Soho", "Palermo Viejo", "Palermo Nuevo", "Bot\u00e1nico", "Barrio Parque", "Las Ca\u00f1itas", "Barrio Parque, Palermo"],
    "Belgrano":             ["Belgrano", "Belgrano C", "Belgrano R", "Belgrano Chico", "Barrio Chino", "Barrio Parque General Belgrano", "Belgrano, Capital Federal"],
    "Recoleta":             ["Recoleta", "Barrio Norte", "Recoleta, Capital Federal"],
    "Caballito":            ["Caballito", "Caballito Norte", "Caballito Sur", "Cid Campeador", "Parque Rivadavia", "Primera Junta", "Caballito, Capital Federal"],
    "Villa Urquiza":        ["Villa Urquiza", "Villa Ortuzar", "Villa Urquiza, Capital Federal"],
    "N\u00fa\u00f1ez":      ["N\u00fa\u00f1ez", "Lomas de N\u00fa\u00f1ez", "N\u00fa\u00f1ez, Capital Federal"],
    "Colegiales":           ["Colegiales", "Concepci\u00f3n", "Colegiales, Capital Federal"],
    "Villa Crespo":         ["Villa Crespo", "Parque Centenario", "Villa Crespo, Capital Federal"],
    "Almagro":              ["Almagro", "Almagro Norte", "Almagro Sur", "Almagro, Capital Federal", "Almagro Norte, Almagro", "Almagro Sur, Almagro"],
    "Saavedra":             ["Saavedra", "Saavedra, Capital Federal"],
    "Villa del Parque":     ["Villa del Parque", "Villa Santa Rita", "Villa del Parque, Capital Federal"],
    "Chacarita":            ["Chacarita", "Chacarita, Capital Federal"],
    "Flores":               ["Flores", "Flores Norte", "Flores Sur", "Flores, Capital Federal"],
    "San Telmo":            ["San Telmo", "San Telmo, Capital Federal"],
    "Boedo":                ["Boedo", "Boedo, Capital Federal"],
    "Villa Devoto":         ["Villa Devoto", "Villa Devoto, Capital Federal"],
    "Coghlan":              ["Coghlan", "Coghlan, Capital Federal"],
    "Retiro":               ["Retiro", "Catalinas", "Distrito Quartier", "Puerto Retiro", "Retiro, Capital Federal"],
    "Puerto Madero":        ["Puerto Madero", "Puerto Madero, Capital Federal"],
    "Centro / Microcentro": ["Centro / Microcentro", "Centro / Microcentro, Capital Federal"],
    "Tribunales":           ["Tribunales", "Tribunales, Capital Federal"],
    "Monserrat":            ["Monserrat", "San Nicol\u00e1s", "Monserrat, Capital Federal"],
    "La Paternal":          ["La Paternal", "La Paternal, Capital Federal"],
    "Parque Chas":          ["Parque Chas", "Parque Chas, Capital Federal"],
    "Floresta":             ["Floresta", "Floresta Norte", "Floresta Sur", "Floresta, Capital Federal"],
    "San Crist\u00f3bal":   ["San Crist\u00f3bal", "Constituci\u00f3n", "San Cristobal, Capital Federal"],
    "Once":                 ["Once", "Once, Capital Federal"],
    "Congreso":             ["Congreso", "Congreso, Capital Federal"],
    "Balvanera":            ["Balvanera", "Balvanera, Capital Federal"],
    "Parque Patricios":     ["Parque Patricios", "Parque Patricios, Capital Federal"],
    "Barracas / La Boca":   ["Barracas", "La Boca", "Barracas, Capital Federal", "La Boca, Capital Federal"],
    "Mataderos":            ["Mataderos", "Na\u00f3n", "Mataderos, Capital Federal"],
    "Agron\u00f3mia":       ["Agronom\u00eda", "Agronom\u00eda, Capital Federal"],
    "Otros":                ["Liniers", "Liniers, Capital Federal", "Monte Castro", "Monte Castro, Capital Federal", "Parque Avellaneda", "Parque Avellaneda, Capital Federal", "Parque Chacabuco", "Parque Chacabuco, Capital Federal", "Pompeya", "Pompeya, Capital Federal", "Versalles", "Versalles, Capital Federal", "Villa Luro", "Villa Luro, Capital Federal", "Villa Lugano", "Villa Lugano, Capital Federal", "Villa Real", "Villa Real, Capital Federal", "Villa Riachuelo", "Villa Riachuelo, Capital Federal", "Villa Soldati", "Villa Soldati, Capital Federal", "Velez Sarsfield", "Velez Sarsfield, Capital Federal", "Villa Pueyrred\u00f3n", "Villa Pueyrred\u00f3n, Capital Federal", "Villa General Mitre", "Villa General Mitre, Capital Federal", "Otro", "Otros"],
}

ORDEN_BARRIOS = [
    # Prioridad definida por el usuario
    "Palermo", "Recoleta", "Belgrano", "Villa Urquiza", "Saavedra",
    "N\u00fa\u00f1ez", "Coghlan", "Colegiales", "Villa del Parque", "Caballito",
    "Villa Crespo",
    # Resto en orden original
    "Almagro", "Chacarita", "Flores", "San Telmo", "Boedo",
    "Villa Devoto", "Retiro", "Puerto Madero", "Centro / Microcentro",
    "Tribunales", "Monserrat", "La Paternal", "Parque Chas", "Floresta",
    "San Crist\u00f3bal", "Once", "Congreso", "Balvanera", "Parque Patricios",
    "Barracas / La Boca", "Mataderos", "Agron\u00f3mia", "Otros",
]

# Indice de orden (1-based)
ORDEN_INDEX = {b: i + 1 for i, b in enumerate(ORDEN_BARRIOS)}

# Lookup invertido: valor original -> barrio_simple
LOOKUP = {}
for barrio_simple, variantes in MAPEO_BARRIOS.items():
    for v in variantes:
        LOOKUP[v] = barrio_simple

VIEW_SQL = """
CREATE VIEW IF NOT EXISTS propiedades_ordenadas AS
SELECT * FROM propiedades
WHERE precio_por_m2_tasable IS NOT NULL
ORDER BY orden_barrio ASC, precio_por_m2_tasable ASC
"""


def resolver_barrio(barrio_raw):
    """Busca barrio_raw en el mapeo. Prueba el texto completo y luego
    la parte antes de la primera coma (ej: 'Palermo, Capital Federal')."""
    if not barrio_raw:
        return "Otros", ORDEN_INDEX["Otros"]

    # Match exacto
    if barrio_raw in LOOKUP:
        simple = LOOKUP[barrio_raw]
        return simple, ORDEN_INDEX[simple]

    # Match por la parte antes de la coma
    parte = barrio_raw.split(",")[0].strip()
    if parte in LOOKUP:
        simple = LOOKUP[parte]
        return simple, ORDEN_INDEX[simple]

    return "Otros", ORDEN_INDEX["Otros"]


def agregar_columnas(conn):
    existentes = {
        row[1] for row in conn.execute("PRAGMA table_info(propiedades)").fetchall()
    }
    for nombre, tipo in [("barrio_simple", "TEXT"), ("orden_barrio", "INTEGER")]:
        if nombre not in existentes:
            conn.execute(f"ALTER TABLE propiedades ADD COLUMN {nombre} {tipo}")
            print(f"  Columna agregada: {nombre} ({tipo})")
        else:
            print(f"  Columna ya existe: {nombre}")
    conn.commit()


def mapear(conn):
    rows = conn.execute(
        "SELECT id_zonaprop, barrio FROM propiedades"
    ).fetchall()

    mapeados = 0
    otros = 0
    sin_mapear = Counter()

    for id_zp, barrio_raw in rows:
        simple, orden = resolver_barrio(barrio_raw)

        conn.execute(
            "UPDATE propiedades SET barrio_simple = ?, orden_barrio = ? WHERE id_zonaprop = ?",
            (simple, orden, id_zp),
        )

        if simple == "Otros":
            otros += 1
            # Solo registrar como "sin mapear" si no esta en el LOOKUP
            # (es decir, cayo en Otros por descarte, no por mapeo explicito)
            if barrio_raw and barrio_raw not in LOOKUP:
                parte = barrio_raw.split(",")[0].strip()
                if parte not in LOOKUP:
                    sin_mapear[barrio_raw] += 1
        else:
            mapeados += 1

    conn.commit()
    return mapeados, otros, sin_mapear


def crear_vista(conn):
    conn.execute("DROP VIEW IF EXISTS propiedades_ordenadas")
    conn.execute(VIEW_SQL)
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM propiedades_ordenadas"
    ).fetchone()[0]
    return count


def main():
    conn = sqlite3.connect(DB_PATH)

    print("Agregando columnas...")
    agregar_columnas(conn)

    print("\nMapeando barrios...")
    mapeados, otros, sin_mapear = mapear(conn)

    print("\nActualizando vista propiedades_ordenadas...")
    vista_count = crear_vista(conn)

    # Resumen
    total = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]
    print(f"\n{'=' * 60}")
    print("RESUMEN")
    print(f"{'=' * 60}")
    print(f"  Total registros:     {total}")
    print(f"  Mapeados a barrio:   {mapeados}")
    print(f"  Asignados a Otros:   {otros}")
    print(f"  Vista ordenada:      {vista_count} registros")

    # Barrios sin mapear con mas de 3 ocurrencias
    frecuentes = {b: c for b, c in sin_mapear.items() if c > 3}
    if frecuentes:
        print(f"\n  ATENCION: barrios sin mapear con mas de 3 ocurrencias:")
        for barrio, count in sorted(frecuentes.items(), key=lambda x: -x[1]):
            safe_barrio = barrio.encode("ascii", "replace").decode()
            print(f"    {count:>4}x  {safe_barrio}")
    else:
        print(f"\n  Sin barrios frecuentes sin mapear (todos con <=3 ocurrencias)")

    # Distribucion por barrio_simple
    dist = conn.execute(
        """
        SELECT barrio_simple, orden_barrio, COUNT(*) as n
        FROM propiedades
        GROUP BY barrio_simple, orden_barrio
        ORDER BY orden_barrio
        """
    ).fetchall()
    print(f"\n  Distribucion por barrio_simple:")
    print(f"  {'#':>3} {'Barrio':<25} {'Cant':>5}")
    print(f"  {'-' * 35}")
    for barrio_s, orden, n in dist:
        safe_barrio = (barrio_s or "?").encode("ascii", "replace").decode()
        print(f"  {orden:>3} {safe_barrio:<25} {n:>5}")

    print(f"{'=' * 60}")
    conn.close()


if __name__ == "__main__":
    main()
