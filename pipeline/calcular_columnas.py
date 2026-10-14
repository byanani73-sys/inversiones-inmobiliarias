"""
Agrega columnas calculadas a la tabla propiedades y las actualiza:

  m2_descubiertos    = m2_totales - m2_cubiertos
  m2_tasables        = m2_cubiertos + proporcion de descubiertos
  precio_por_m2_total   = precio_actual / m2_totales
  precio_por_m2_tasable = precio_actual / m2_tasables

Registros con m2_cubiertos > m2_totales se marcan como dato corrupto
(columnas calculadas = NULL).

Crea una VIEW propiedades_ordenadas filtrada y ordenada por barrio + precio/m2.

Uso:
  python pipeline/calcular_columnas.py
"""

import sqlite3

DB_PATH = "data/zonaprop.db"

COLUMNAS = [
    ("m2_descubiertos", "INTEGER"),
    ("m2_tasables", "INTEGER"),
    ("precio_por_m2_total", "REAL"),
    ("precio_por_m2_tasable", "REAL"),
]

VIEW_SQL = """
CREATE VIEW IF NOT EXISTS propiedades_ordenadas AS
SELECT * FROM propiedades
WHERE precio_por_m2_tasable IS NOT NULL
ORDER BY barrio ASC, precio_por_m2_tasable ASC
"""


def agregar_columnas(conn):
    """Agrega las columnas si no existen."""
    existentes = {
        row[1] for row in conn.execute("PRAGMA table_info(propiedades)").fetchall()
    }
    for nombre, tipo in COLUMNAS:
        if nombre not in existentes:
            conn.execute(f"ALTER TABLE propiedades ADD COLUMN {nombre} {tipo}")
            print(f"  Columna agregada: {nombre} ({tipo})")
        else:
            print(f"  Columna ya existe: {nombre}")
    conn.commit()


def calcular(conn):
    """Calcula y actualiza los valores para todos los registros."""
    # Registros con datos suficientes para intentar el calculo
    rows = conn.execute(
        """
        SELECT id_zonaprop, m2_totales, m2_cubiertos, precio_actual
        FROM propiedades
        WHERE m2_cubiertos IS NOT NULL
          AND m2_totales IS NOT NULL
          AND precio_actual IS NOT NULL
        """
    ).fetchall()

    total_db = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]
    sin_datos = total_db - len(rows)

    actualizados = 0
    corruptos = 0
    corruptos_detalle = []

    for id_zp, m2_tot, m2_cub, precio in rows:
        # Validacion: m2_cubiertos no puede superar m2_totales
        if m2_cub > m2_tot:
            conn.execute(
                """
                UPDATE propiedades SET
                    m2_descubiertos    = NULL,
                    m2_tasables        = NULL,
                    precio_por_m2_total   = NULL,
                    precio_por_m2_tasable = NULL
                WHERE id_zonaprop = ?
                """,
                (id_zp,),
            )
            corruptos += 1
            corruptos_detalle.append(
                f"    {id_zp}: m2_cub={m2_cub} > m2_tot={m2_tot}"
            )
            continue

        m2_desc = m2_tot - m2_cub

        if m2_desc <= 25:
            m2_tas = m2_cub + round(m2_desc * 0.5)
        else:
            m2_tas = m2_cub + round(m2_desc * 0.33)

        precio_m2_total = round(precio / m2_tot, 2) if m2_tot > 0 else None
        precio_m2_tasable = round(precio / m2_tas, 2) if m2_tas > 0 else None

        conn.execute(
            """
            UPDATE propiedades SET
                m2_descubiertos    = ?,
                m2_tasables        = ?,
                precio_por_m2_total   = ?,
                precio_por_m2_tasable = ?
            WHERE id_zonaprop = ?
            """,
            (m2_desc, m2_tas, precio_m2_total, precio_m2_tasable, id_zp),
        )
        actualizados += 1

    conn.commit()
    return actualizados, sin_datos, corruptos, corruptos_detalle


def crear_vista(conn):
    """Crea (o recrea) la VIEW propiedades_ordenadas."""
    conn.execute("DROP VIEW IF EXISTS propiedades_ordenadas")
    conn.execute(VIEW_SQL)
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM propiedades_ordenadas"
    ).fetchone()[0]
    return count


def resumen(conn, actualizados, sin_datos, corruptos, corruptos_detalle, vista_count):
    """Muestra el resumen final."""
    total = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]

    print(f"\n{'=' * 60}")
    print("RESUMEN")
    print(f"{'=' * 60}")
    print(f"  Total registros en DB:       {total}")
    print(f"  Actualizados correctamente:  {actualizados}")
    print(f"  Salteados por NULL:          {sin_datos}")
    print(f"  Salteados por corruptos:     {corruptos}")
    print(f"  Vista propiedades_ordenadas: {vista_count} registros")

    if corruptos_detalle:
        print(f"\n  Detalle de datos corruptos (m2_cub > m2_tot):")
        for line in corruptos_detalle:
            print(line)

    # Muestra de los primeros registros de la vista
    rows = conn.execute(
        """
        SELECT id_zonaprop, moneda, precio_actual,
               m2_totales, m2_cubiertos, m2_descubiertos,
               m2_tasables, precio_por_m2_total, precio_por_m2_tasable,
               barrio
        FROM propiedades_ordenadas
        LIMIT 10
        """
    ).fetchall()

    if rows:
        print(f"\n  Top 10 de la vista (ordenado por barrio + $/m2 tasable):")
        print(
            f"  {'ID':>10} {'Mon':>4} {'Precio':>10} "
            f"{'Tot':>5} {'Cub':>5} {'Desc':>5} {'Tas':>5} "
            f"{'$/m2 tot':>10} {'$/m2 tas':>10}  {'Barrio'}"
        )
        print("  " + "-" * 95)
        for r in rows:
            id_zp, mon, precio, tot, cub, desc, tas, pm2t, pm2s, barrio = r
            barrio_s = (barrio or "?")[:25]
            print(
                f"  {id_zp:>10} {(mon or '?'):>4} {precio:>10,} "
                f"{tot:>5} {cub:>5} {desc:>5} {tas:>5} "
                f"{pm2t:>10,.2f} {pm2s:>10,.2f}  {barrio_s}"
            )
    print(f"{'=' * 60}")


def main():
    conn = sqlite3.connect(DB_PATH)

    print("Agregando columnas...")
    agregar_columnas(conn)

    print("\nCalculando valores...")
    actualizados, sin_datos, corruptos, corruptos_detalle = calcular(conn)

    print("\nCreando vista propiedades_ordenadas...")
    vista_count = crear_vista(conn)
    print(f"  {vista_count} registros en la vista")

    resumen(conn, actualizados, sin_datos, corruptos, corruptos_detalle, vista_count)
    conn.close()


if __name__ == "__main__":
    main()
