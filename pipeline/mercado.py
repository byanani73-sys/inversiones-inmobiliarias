"""
mercado.py — Referencias de mercado por barrio

Fuentes:
  - Mudafy (alquiler, actualización diaria):
    https://mudafy.com.ar/monitor-inmobiliario/caba?operacion=alquiler
  - deinmobiliarios (venta cierre, semestral):
    https://www.deinmobiliarios.com/blog/precios-de-las-propiedades-en-caba-...

Para barrios sin datos directos se usa un comparable + factor de ajuste.
Los precios son USD/m².

Actualizar manualmente cuando salga un nuevo informe de deinmobiliarios.
"""

# ---------------------------------------------------------------------------
# Precios de ALQUILER por barrio — Mudafy (USD/m²/mes)
# Última actualización: octubre 2026
# ---------------------------------------------------------------------------
ALQUILER_USD_M2 = {
    "Puerto Madero":        23,
    "Núñez":                20,
    "Palermo":              19,
    "Colegiales":           18,
    "Belgrano":             16,
    "Recoleta":             15,
    "Retiro":               15,
    "Villa Crespo":         14,
    "Villa Urquiza":        14,
    "Flores":               13,
    "Caballito":            13,
    "Almagro":              12,
    "Monserrat":            12,
    "Centro / Microcentro": 11,
    "Balvanera":            10,
}

# ---------------------------------------------------------------------------
# Precios de VENTA (cierre real) por barrio y tipo — deinmobiliarios (USD/m²)
# Última actualización: H1 2026
# Tipos: "monoambiente", "2_amb", "3_amb", "4_amb_mas", "ph"
# ---------------------------------------------------------------------------
VENTA_CIERRE_USD_M2 = {
    "Recoleta": {
        "monoambiente": 2300,
        "2_amb":        2200,
        "3_amb":        2100,
        "4_amb_mas":    1900,
        "ph":           None,
    },
    "Palermo": {
        "monoambiente": 2600,
        "2_amb":        2400,
        "3_amb":        2200,
        "4_amb_mas":    2100,
        "ph":           1500,
    },
    "Belgrano": {
        "monoambiente": 2400,
        "2_amb":        2200,
        "3_amb":        2100,
        "4_amb_mas":    1900,
        "ph":           1600,
    },
    "Almagro": {
        "monoambiente": 1900,
        "2_amb":        1800,
        "3_amb":        1700,
        "4_amb_mas":    1400,
        "ph":           1100,
    },
    "Balvanera": {
        "monoambiente": 1500,
        "2_amb":        1500,
        "3_amb":        1300,
        "4_amb_mas":    1100,
        "ph":           800,
    },
}

# ---------------------------------------------------------------------------
# Comparables para barrios sin datos directos
# (barrio_comparable, factor)
# factor > 1 → más caro que el comparable
# factor < 1 → más barato que el comparable
# Se aplica tanto a alquiler como a venta salvo que se especifique otro.
# ---------------------------------------------------------------------------
COMPARABLES = {
    # Zona norte
    "Saavedra":             ("Núñez",         0.95),
    "Coghlan":              ("Saavedra",       0.95),
    "Villa Devoto":         ("Villa Urquiza",  0.90),
    "Villa del Parque":     ("Villa Urquiza",  0.90),

    # Zona centro-oeste
    "Chacarita":            ("Villa Crespo",   0.90),
    "Parque Chas":          ("Coghlan",        0.90),
    "La Paternal":          ("Villa Crespo",   0.85),
    "Agronomía":            ("La Paternal",    0.90),

    # Zona centro
    "Boedo":                ("Almagro",        0.90),
    "San Cristóbal":        ("Balvanera",      0.95),
    "Once":                 ("Balvanera",      0.95),
    "Congreso":             ("Monserrat",      1.00),
    "Floresta":             ("Caballito",      0.85),
    "Tribunales":           ("Recoleta",       0.90),

    # Zona sur
    "San Telmo":            ("Monserrat",      1.10),
    "Parque Patricios":     ("Boedo",          0.85),
    "Barracas / La Boca":   ("Parque Patricios", 0.90),
    "Mataderos":            ("Flores",         0.80),

    # Casos especiales
    "Retiro":               ("Retiro",         1.00),   # Retiro ya tiene datos en alquiler
}

# ---------------------------------------------------------------------------
# Costos de compra (% sobre precio de compra)
# ---------------------------------------------------------------------------
COSTOS_COMPRA = {
    "sellos":       0.036,   # Impuesto de sellos CABA
    "escribania":   0.020,   # Honorarios notariales
    # Comisión: se calcula según origen de la propiedad (ver calcular_costos_compra)
}

# Comisión según origen
COMISION_COMPRA = {
    "dueno_directo":  0.00,   # Sin comisión
    "captada_padre":  0.00,   # El padre la captó, no hay comisión
    "inmobiliaria":   0.02,   # 2% (se comparte entre inmobiliarias)
}

# Costos de venta (% sobre precio de venta estimado)
COSTOS_VENTA = {
    "comision_inmobiliaria": 0.03,   # 3% al vender (estándar)
    "escribania_vendedor":   0.010,  # ~1%
    # Impuesto a la transferencia: aplica solo en ciertos casos,
    # se calcula aparte si corresponde.
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_alquiler_m2(barrio: str) -> float | None:
    """
    Devuelve el precio de alquiler en USD/m²/mes para el barrio.
    Si no tiene datos directos, usa el comparable con su factor.
    Devuelve None si no hay manera de estimarlo.
    """
    if barrio in ALQUILER_USD_M2:
        return ALQUILER_USD_M2[barrio]

    if barrio in COMPARABLES:
        comp, factor = COMPARABLES[barrio]
        base = get_alquiler_m2(comp)   # recursivo por si el comparable también usa otro
        if base is not None:
            return round(base * factor, 1)

    return None


def get_venta_m2(barrio: str, tipo_propiedad: str) -> float | None:
    """
    Devuelve el precio de cierre estimado en USD/m² para el barrio y tipo.
    Si no tiene datos directos, usa el comparable con su factor.
    tipo_propiedad: 'Departamento', 'PH', etc. (como viene de ZonaProp)
    """
    tipo_key = _normalizar_tipo(tipo_propiedad)
    if tipo_key is None:
        return None

    if barrio in VENTA_CIERRE_USD_M2:
        return VENTA_CIERRE_USD_M2[barrio].get(tipo_key)

    if barrio in COMPARABLES:
        comp, factor = COMPARABLES[barrio]
        base = get_venta_m2(comp, tipo_propiedad)
        if base is not None:
            return round(base * factor)

    return None


def _normalizar_tipo(tipo_zonaprop: str) -> str | None:
    """Mapea tipo de propiedad de ZonaProp a clave interna."""
    if not tipo_zonaprop:
        return None
    t = tipo_zonaprop.lower()
    if "ph" in t:
        return "ph"
    if "departamento" in t:
        return None   # necesitamos saber ambientes — ver calcular_tipo_por_ambientes
    return None


def tipo_por_ambientes(ambientes: int | None, tipo_zonaprop: str) -> str | None:
    """Determina la clave de tipo a partir de ambientes."""
    if tipo_zonaprop and "ph" in tipo_zonaprop.lower():
        return "ph"
    if ambientes is None:
        return None
    if ambientes == 1:
        return "monoambiente"
    if ambientes == 2:
        return "2_amb"
    if ambientes == 3:
        return "3_amb"
    return "4_amb_mas"


def calcular_costos_compra(precio: int, origen: str = "dueno_directo") -> dict:
    """
    Calcula los costos de compra en USD.
    origen: 'dueno_directo' | 'captada_padre' | 'inmobiliaria'
    """
    sellos      = round(precio * COSTOS_COMPRA["sellos"])
    escribania  = round(precio * COSTOS_COMPRA["escribania"])
    comision    = round(precio * COMISION_COMPRA.get(origen, 0.02))
    total       = sellos + escribania + comision
    return {
        "sellos":            sellos,
        "escribania_compra": escribania,
        "comision_compra":   comision,
        "total_costos_compra": total,
        "precio_total_compra": precio + total,
    }


def calcular_roi_alquiler(precio_compra: int, m2_tasables: int,
                           barrio: str, origen: str = "dueno_directo") -> dict | None:
    """
    Calcula el ROI de la estrategia alquiler (buy & hold).
    Retorna dict con métricas o None si no hay datos de mercado.
    """
    alquiler_m2 = get_alquiler_m2(barrio)
    if alquiler_m2 is None or not m2_tasables:
        return None

    costos = calcular_costos_compra(precio_compra, origen)
    precio_total = costos["precio_total_compra"]

    alquiler_mensual = round(alquiler_m2 * m2_tasables)
    alquiler_anual   = alquiler_mensual * 12
    rendimiento_bruto = round(alquiler_anual / precio_total * 100, 2)

    return {
        "alquiler_mensual_estimado": alquiler_mensual,
        "alquiler_anual_estimado":   alquiler_anual,
        "rendimiento_bruto_%":       rendimiento_bruto,
        **costos,
    }


def calcular_roi_flip(precio_compra: int, m2_tasables: int,
                      barrio: str, ambientes: int | None,
                      tipo_propiedad: str,
                      origen: str = "dueno_directo") -> dict | None:
    """
    Calcula el ROI de la estrategia flip (compra y venta).
    Retorna dict con métricas o None si no hay datos de mercado.
    """
    tipo_key = tipo_por_ambientes(ambientes, tipo_propiedad)
    precio_venta_m2 = get_venta_m2(barrio, tipo_propiedad) if tipo_key is None else None

    if tipo_key:
        # Buscar precio usando tipo_key directo
        if barrio in VENTA_CIERRE_USD_M2:
            precio_venta_m2 = VENTA_CIERRE_USD_M2[barrio].get(tipo_key)
        elif barrio in COMPARABLES:
            comp, factor = COMPARABLES[barrio]
            base_m2 = (VENTA_CIERRE_USD_M2.get(comp) or {}).get(tipo_key)
            if base_m2:
                precio_venta_m2 = round(base_m2 * factor)

    if precio_venta_m2 is None or not m2_tasables:
        return None

    costos_compra    = calcular_costos_compra(precio_compra, origen)
    precio_total_in  = costos_compra["precio_total_compra"]

    precio_venta_est = precio_venta_m2 * m2_tasables
    costo_comision_v = round(precio_venta_est * COSTOS_VENTA["comision_inmobiliaria"])
    costo_escrib_v   = round(precio_venta_est * COSTOS_VENTA["escribania_vendedor"])
    total_costos_v   = costo_comision_v + costo_escrib_v

    ganancia_bruta   = precio_venta_est - precio_total_in - total_costos_v
    roi              = round(ganancia_bruta / precio_total_in * 100, 2)

    return {
        "precio_venta_estimado":     precio_venta_est,
        "precio_venta_m2_referencia": precio_venta_m2,
        "costos_venta":              total_costos_v,
        "ganancia_estimada":         ganancia_bruta,
        "roi_flip_%":                roi,
        **costos_compra,
    }
