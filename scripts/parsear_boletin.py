#!/usr/bin/env python3
"""
Parsea el Boletín de Reuniones de Comisiones del Senado (PDF) y actualiza data/agenda.json.

Uso:
    python scripts/parsear_boletin.py [ruta_al_pdf]

Si no se pasa ruta, detecta automáticamente el boletín con número más alto en data/boletines/.
"""

import json
import re
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"
BOLETINES_DIR = DATA_DIR / "boletines"
AGENDA_PATH = DATA_DIR / "agenda.json"

FOOTER_RE = re.compile(
    r"GP\.GCIP|La impresión de este documento|^Fecha: \d{2}/\d{2}/\d{4}$",
    re.IGNORECASE,
)

DIAS_SEMANA = re.compile(
    r"\b(LUNES|MARTES|MI[EÉ]RCOLES|JUEVES|VIERNES|S[AÁ]BADO|DOMINGO)\b",
    re.IGNORECASE,
)

# Tipos de ítem de temario reconocidos
TIPOS_TEMARIO = re.compile(
    r"^(PROYECTO DE LEY|PROYECTO DE RESOLUCI[OÓ]N|PROYECTO DE DECLARACI[OÓ]N"
    r"|PROYECTO DE COMUNICACI[OÓ]N|CANDIDATOS|AUDIENCIA P[UÚ]BLICA|DICTAMEN"
    r"|MENSAJE DEL P\.?E\.?|INFORMES?)[\s:]*$",
    re.IGNORECASE | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Detección automática del boletín más reciente
# ---------------------------------------------------------------------------

def _normalizar_nombre(nombre: str) -> str:
    return (
        nombre.upper()
        .replace("Í", "I").replace("É", "E").replace("Ó", "O")
        .replace("Ú", "U").replace("Á", "A").replace("Ñ", "N")
    )


def encontrar_ultimo_boletin() -> tuple[Path | None, int]:
    patron = re.compile(r"BOLETIN_DE_REUNIONES_DE_COMISIONES_(\d+)-(\d+)\.PDF$")
    mejor: Path | None = None
    mejor_n = -1
    for archivo in BOLETINES_DIR.glob("*.pdf"):
        nombre_norm = _normalizar_nombre(archivo.name)
        m = patron.match(nombre_norm)
        if m and int(m.group(1)) > mejor_n:
            mejor_n = int(m.group(1))
            mejor = archivo
    return mejor, mejor_n


def extraer_numero_boletin(nombre_archivo: str) -> tuple[int | None, str | None]:
    nombre_norm = _normalizar_nombre(Path(nombre_archivo).name)
    m = re.search(r"BOLETIN_DE_REUNIONES_DE_COMISIONES_(\d+)-(\d+)", nombre_norm)
    if m:
        return int(m.group(1)), m.group(2)
    return None, None


# ---------------------------------------------------------------------------
# Extracción del PDF
# ---------------------------------------------------------------------------

def extraer_pdf(pdf_path: Path) -> tuple[list[list[list]], str]:
    """Devuelve (filas_de_tablas, texto_completo) de todas las páginas."""
    all_rows: list[list[list]] = []
    textos: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for tabla in page.extract_tables():
                all_rows.extend(tabla)
            texto = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            textos.append(texto)

    return all_rows, "\n".join(textos)


# ---------------------------------------------------------------------------
# Parseo de tablas → metadata de reuniones
# ---------------------------------------------------------------------------

def _es_fila_encabezado(row: list) -> bool:
    return (
        isinstance(row[0], str) and "FECHA" in row[0].upper()
        and isinstance(row[1], str) and "HORA" in row[1].upper()
    )


def _limpiar(texto: str | None) -> str:
    if not texto:
        return ""
    return re.sub(r"\s+", " ", texto).strip()


def parsear_celda_fecha(celda: str) -> tuple[str, str]:
    """'MIÉRCOLES\n20/05' → ('MIÉRCOLES', '20/05')"""
    lineas = [l.strip() for l in celda.strip().split("\n") if l.strip()]
    dia = ""
    fecha = ""
    for l in lineas:
        if DIAS_SEMANA.match(l):
            dia = l.upper()
        elif re.match(r"^\d{1,2}/\d{2}$", l):
            fecha = l
    return dia, fecha


def parsear_celda_comisiones(celda: str) -> tuple[list[str], str]:
    """
    'SALUD\nLEGISLACIÓN GENERAL\n(CUARTO INTERMEDIO)'
    → (['SALUD', 'LEGISLACIÓN GENERAL'], 'CUARTO INTERMEDIO')
    """
    lineas = [l.strip() for l in celda.strip().split("\n") if l.strip()]
    comisiones = []
    modalidad = ""
    for l in lineas:
        m = re.match(r"^\((.+)\)$", l)
        if m:
            modalidad = m.group(1).strip()
        else:
            comisiones.append(l)
    return comisiones, modalidad


def parsear_celda_salon(celda: str) -> tuple[str, str]:
    """
    'ILLIA\n1° PISO DEL PALACIO DEL HSN'
    → ('ILLIA', '1° PISO DEL PALACIO DEL HSN')
    """
    lineas = [l.strip() for l in celda.strip().split("\n") if l.strip()]
    salon = lineas[0] if lineas else ""
    salon_resto = " ".join(lineas[1:]) if len(lineas) > 1 else ""
    return salon, salon_resto


def parsear_celda_contenido(celda: str) -> tuple[list[dict], list[str]]:
    """
    Parsea la celda de temario y devuelve (items_temario, expositores_parciales).
    Los expositores completos se cargan desde el texto completo del PDF.
    """
    if not celda or not celda.strip():
        return [], []

    texto = celda.strip()

    # Separar expositores del resto
    expositores_parciales: list[str] = []
    m_exp = re.search(r"EXPOSITORES?:\s*\n(.*)", texto, re.DOTALL | re.IGNORECASE)
    if m_exp:
        texto_exp = m_exp.group(1)
        expositores_parciales = _parsear_expositores_texto(texto_exp)
        texto = texto[: m_exp.start()].strip()

    # Parsear ítems del temario
    items = _parsear_items_temario(texto)

    return items, expositores_parciales


def _parsear_items_temario(texto: str) -> list[dict]:
    """Extrae ítems del tipo 'PROYECTO DE LEY:\nEXPTE. ...'"""
    items: list[dict] = []
    if not texto.strip():
        return items

    # Dividir por líneas que son encabezados de tipo
    bloques = re.split(r"\n(?=PROYECTO DE |CANDIDATOS|AUDIENCIA|DICTAMEN|INFORME|MENSAJE)", texto, flags=re.IGNORECASE)
    for bloque in bloques:
        bloque = bloque.strip()
        if not bloque:
            continue
        # Separar tipo del contenido
        m = re.match(
            r"^(PROYECTO DE LEY|PROYECTO DE RESOLUCI[OÓ]N|PROYECTO DE DECLARACI[OÓ]N"
            r"|PROYECTO DE COMUNICACI[OÓ]N|CANDIDATOS|AUDIENCIA P[UÚ]BLICA|DICTAMEN"
            r"|MENSAJE DEL P\.?E\.?|INFORMES?)[:\s]*(.*)$",
            bloque,
            re.DOTALL | re.IGNORECASE,
        )
        if m:
            tipo = m.group(1).upper().strip()
            contenido = _limpiar(m.group(2))
            items.append({"tipo": tipo, "contenido": contenido})
        else:
            # Contenido sin tipo reconocido → agregar al último ítem o como genérico
            if items:
                items[-1]["contenido"] = _limpiar(items[-1]["contenido"] + " " + bloque)
            else:
                items.append({"tipo": "OTROS", "contenido": _limpiar(bloque)})
    return items


_PALABRAS_NO_NOMBRE = {
    "PREVENCIÓN", "PREVENCION", "ESPECIALISTA", "COORDINACIÓN", "COORDINACION",
    "DIRECCIÓN", "DIRECCION", "COMISIÓN", "COMISION", "PRESIDENTA", "PRESIDENTE",
    "SECRETARIA", "SECRETARIO", "REPRESENTANTE", "FUNDADORA", "FUNDADOR",
    "INVESTIGADORA", "INVESTIGADOR", "TRABAJADORA", "TRABAJADOR", "JEFA", "JEFE",
    "PROFESORA", "PROFESOR", "DOCENTE", "SISTEMA", "UNIVERSIDAD", "ÁREA", "AREA",
    "HOSPITAL", "JUZGADO", "COMITÉ", "COMITE", "ASOCIACIÓN", "ASOCIACION",
    "PROVINCIA", "FACULTAD", "DECANA", "DECANO", "MÉDICA", "MÉDICO", "MEDICA",
    "MEDICO", "ABOGADA", "ABOGADO", "PSICÓLOGA", "PSICOLOGA", "LICENCIADA",
    "LICENCIADO", "TOXICOLOGÍA", "TOXICOLOGIA",
}


def _es_inicio_expositor(linea: str) -> bool:
    """True si la línea comienza con lo que parece un nombre de persona seguido de coma."""
    m = re.match(r"^([^,]+),", linea)
    if not m:
        return False
    palabras = m.group(1).strip().split()
    if len(palabras) < 2 or len(palabras) > 7:
        return False
    # Normalizar la primera palabra (quitar tildes) y comparar con palabras no-nombre
    primera = palabras[0].upper()
    primera_norm = (
        primera.replace("É", "E").replace("Á", "A").replace("Ó", "O")
        .replace("Í", "I").replace("Ú", "U").replace("Ñ", "N")
    )
    return primera_norm not in _PALABRAS_NO_NOMBRE


def _parsear_expositores_texto(texto: str) -> list[str]:
    """
    Parsea texto con lista de expositores en mayúsculas.
    Un nuevo expositor comienza cuando la entrada anterior terminó en '.' y
    la línea actual parece un nombre de persona.
    """
    expositores: list[str] = []
    buffer: list[str] = []

    for linea in texto.split("\n"):
        linea = linea.strip()
        if not linea:
            continue
        if FOOTER_RE.search(linea):
            continue
        prev_terminado = bool(buffer) and buffer[-1].rstrip().endswith(".")
        if prev_terminado and _es_inicio_expositor(linea):
            expositores.append(_limpiar(" ".join(buffer)))
            buffer = [linea]
        else:
            buffer.append(linea)

    if buffer:
        expositores.append(_limpiar(" ".join(buffer)))

    return [e for e in expositores if e]


def parsear_reuniones_de_tablas(all_rows: list[list]) -> list[dict]:
    """
    Recorre todas las filas de todas las tablas y extrae las reuniones de senadores.
    Detiene el procesamiento al encontrar 'REUNIONES DE ASESORES'.
    """
    reuniones: list[dict] = []
    en_senadores = False
    reunion_actual: dict | None = None
    esperando_datos = False

    for row in all_rows:
        col0_raw = row[0] or ""
        col0 = _limpiar(col0_raw)
        col0_upper = col0.upper()

        # Fin de sección
        if "ASESORES" in col0_upper and "REUNIONES" in col0_upper:
            break

        # Inicio de sección (puede estar embebida en la primera fila del header)
        if "SENADORES" in col0_upper and "REUNIONES" in col0_upper:
            en_senadores = True
            continue

        # Detección de fila encabezado FECHA | HORA | COMISIÓN | SALÓN
        if _es_fila_encabezado(row):
            en_senadores = True
            esperando_datos = True
            # Guardar reunión anterior si existía
            if reunion_actual:
                reuniones.append(reunion_actual)
            reunion_actual = None
            continue

        if not en_senadores:
            continue

        # Fila con datos de la reunión (inmediatamente después del encabezado)
        if esperando_datos and row[1] is not None and re.match(r"^\d{1,2}:\d{2}$", _limpiar(row[1])):
            dia, fecha = parsear_celda_fecha(row[0] or "")
            hora = _limpiar(row[1])
            comisiones, modalidad = parsear_celda_comisiones(row[2] or "")
            salon, salon_completo = parsear_celda_salon(row[3] or "")

            reunion_actual = {
                "dia": dia,
                "fecha": fecha,
                "hora": hora,
                "modalidad": modalidad,
                "comisiones": comisiones,
                "salon": salon,
                "salon_completo": (_limpiar(salon + " " + salon_completo)).strip(),
                "temario": [],
                "expositores": [],
            }
            esperando_datos = False
            continue

        # Fila TEMARIO (fila de sección, sin datos útiles)
        if col0_upper == "TEMARIO":
            continue

        # Fila vacía
        if not col0:
            continue

        # Fila de contenido: temario + expositores (primera parte)
        if reunion_actual is not None and row[1] is None:
            items, exp_parcial = parsear_celda_contenido(col0_raw)
            reunion_actual["temario"].extend(items)
            reunion_actual["expositores"].extend(exp_parcial)

    # Añadir última reunión
    if reunion_actual:
        reuniones.append(reunion_actual)

    return reuniones


# ---------------------------------------------------------------------------
# Enriquecimiento de expositores con texto completo (cross-page)
# ---------------------------------------------------------------------------

def enriquecer_expositores(reuniones: list[dict], texto_completo: str) -> None:
    """
    Para cada reunión que tenga "EXPOSITORES:" en el texto completo, reemplaza
    la lista parcial de expositores por la completa (incluyendo páginas siguientes).
    """
    # Aislar sección de senadores en el texto completo
    m = re.search(
        r"REUNIONES DE SENADORES.*?\n(.*?)(?=REUNIONES DE ASESORES|\Z)",
        texto_completo,
        re.DOTALL | re.IGNORECASE,
    )
    seccion = m.group(1) if m else texto_completo

    # Cada ocurrencia de "EXPOSITORES:" en la sección
    bloques_exp = list(re.finditer(r"EXPOSITORES?:\s*\n", seccion, re.IGNORECASE))
    if not bloques_exp:
        return

    # Construir índice hora → reunión para matching
    hora_a_reunion = {r["hora"]: r for r in reuniones}

    for i, match in enumerate(bloques_exp):
        # Determinar fin del bloque de expositores
        siguiente_inicio = (
            bloques_exp[i + 1].start() if i + 1 < len(bloques_exp) else len(seccion)
        )
        patron_fin = re.search(
            r"FECHA\s+HORA\s+COMISI[OÓ]N|REUNIONES DE ASESORES",
            seccion[match.end():siguiente_inicio],
            re.IGNORECASE,
        )
        fin = match.end() + (patron_fin.start() if patron_fin else siguiente_inicio - match.end())
        texto_exp = seccion[match.end():fin]

        expositores = _parsear_expositores_texto(texto_exp)

        # Asociar a la reunión cuya HORA aparece más cerca (antes) de este EXPOSITORES:
        texto_previo = seccion[: match.start()]
        horas_previas = re.findall(r"\b(\d{1,2}:\d{2})\b", texto_previo)
        reunion_target = None
        for hora in reversed(horas_previas):
            if hora in hora_a_reunion:
                reunion_target = hora_a_reunion[hora]
                break

        if reunion_target is not None:
            reunion_target["expositores"] = expositores


# ---------------------------------------------------------------------------
# Extracción de metadata del boletín desde el texto
# ---------------------------------------------------------------------------

_MESES = [
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
]
_PATRON_MES = "|".join(_MESES)


def extraer_fecha_info(texto: str) -> str:
    """
    Extrae la fecha del encabezado del boletín ('DD DE MES DE AAAA - HH:MM H').
    pdfplumber fragmenta el encabezado por diferencias de y-posición en el PDF,
    así que se buscan los componentes por separado en los primeros 400 caracteres.
    """
    cabecera = texto[:400]

    # Mes
    m_mes = re.search(rf"\b({_PATRON_MES})\b", cabecera, re.IGNORECASE)
    if not m_mes:
        return ""

    # Día y hora: en el PDF aparecen en la misma línea como "DD AAAA - HH:MM"
    # pdfplumber extrae esa línea como "I 19 2026 - 20:00" (la 'I' grande de INFORMACIÓN)
    m_dia_hora = re.search(r"\b(\d{1,2})\s+20\d{2}\s*[-–]\s*(\d{2}:\d{2})", cabecera)
    if not m_dia_hora:
        return ""

    # Año
    m_anio = re.search(r"\b(20\d{2})\b", cabecera)
    if not m_anio:
        return ""

    return (
        f"{m_dia_hora.group(1)} DE {m_mes.group(1).upper()} DE "
        f"{m_anio.group(1)} - {m_dia_hora.group(2)} H"
    )


# ---------------------------------------------------------------------------
# Punto de entrada principal
# ---------------------------------------------------------------------------

def parsear_boletin(pdf_path: Path) -> dict:
    numero, anio = extraer_numero_boletin(pdf_path.name)
    all_rows, texto_completo = extraer_pdf(pdf_path)

    fecha_info = extraer_fecha_info(texto_completo)
    reuniones = parsear_reuniones_de_tablas(all_rows)
    enriquecer_expositores(reuniones, texto_completo)

    return {
        "boletin": {
            "numero": numero,
            "anio": int(anio) if anio else None,
            "fecha_informacion": fecha_info,
        },
        "reuniones": reuniones,
    }


def main() -> None:
    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
    else:
        BOLETINES_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path, _ = encontrar_ultimo_boletin()
        if pdf_path is None:
            print("Error: no se encontró ningún boletín en data/boletines/", file=sys.stderr)
            sys.exit(1)
        print(f"Boletín detectado: {pdf_path.name}")

    if not pdf_path.exists():
        print(f"Error: archivo no encontrado: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Parseando: {pdf_path.name}")
    resultado = parsear_boletin(pdf_path)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(AGENDA_PATH, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    n = len(resultado["reuniones"])
    print(f"✓ {n} reunión(es) encontrada(s)")
    print(f"✓ Guardado en: {AGENDA_PATH}")

    for r in resultado["reuniones"]:
        print(f"\n  {r['dia']} {r['fecha']} {r['hora']} h.")
        print(f"  Comisiones : {', '.join(r['comisiones'])}")
        print(f"  Salón      : {r['salon']}")
        if r["modalidad"]:
            print(f"  Modalidad  : {r['modalidad']}")
        for t in r["temario"]:
            print(f"  {t['tipo']}: {t['contenido'][:80]}{'...' if len(t['contenido']) > 80 else ''}")
        if r["expositores"]:
            print(f"  Expositores: {len(r['expositores'])}")


if __name__ == "__main__":
    main()
