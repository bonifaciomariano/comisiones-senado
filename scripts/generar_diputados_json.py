#!/usr/bin/env python3
"""
Genera data/diputados_comisiones.json cruzando:
  - data/diputados.csv          (padrón de diputados, fuente de bloques)
  - data/2025-2027 Permanentes al 27-04-2026.pdf  (integrantes por comisión)

Uso:
  python3 scripts/generar_diputados_json.py
"""

import json
import re
import sys
import os
from pathlib import Path

try:
    import pdfplumber
    import pandas as pd
    from unidecode import unidecode
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pdfplumber', 'pandas', 'unidecode', '-q'])
    import pdfplumber
    import pandas as pd
    from unidecode import unidecode

ROOT = Path(__file__).parent.parent
CSV_PATH  = ROOT / 'data' / 'diputados.csv'
PDF_PATH  = ROOT / 'data' / '2025-2027 Permanentes al 27-04-2026.pdf'
OUT_PATH  = ROOT / 'data' / 'diputados_comisiones.json'

# ── Cargo normalisation ────────────────────────────────────────────────────────
CARGO_KEYWORDS = [
    # (normalised_value, regex_pattern)  — longest first so we match "Vice Presidente 1" before "Presidente"
    ('vicepresidente_1', r'Vice\s*[Pp]residente\s*1[°º]?|Vicepresidente\s*1[°º]?'),
    ('vicepresidente_2', r'Vice\s*[Pp]residente\s*2[°º]?|Vicepresidente\s*2[°º]?'),
    ('presidente',       r'Presiden[ta][ae]'),
    ('secretario',       r'Secretari[ao]'),
    ('vocal',            r'Vocales?'),
]

# ── Known PDF bloque strings → CSV bloque (unidecoded upper) ──────────────────
# Sorted longest-first so multi-word matches win over single-word.
PDF_BLOQUE_MAP = {
    'PTS-FIT-UNIDAD':    'PTS-FRENTE DE IZQUIERDA Y DE TRABAJADORES UNIDAD',
    'PO-FIT-UNIDAD':     'PARTIDO OBRERO EN EL FTE DE IZQUIERDA Y DE TRABAJADORES-UNIDAD',
    'Adelante Bs. As.':  'ADELANTE BUENOS AIRES',
    'Provincias Unidas': 'PROVINCIAS UNIDAS',
    'Innovación Fed.':   'INNOVACION FEDERAL',
    'Innovación Fed':    'INNOVACION FEDERAL',
    'Encuentro Fed.':    'ENCUENTRO FEDERAL',
    'Encuentro Fed':     'ENCUENTRO FEDERAL',
    'Elijo Catamarca':   'ELIJO CATAMARCA',
    'Prod. Y Trabajo':   'PRODUCCION Y TRABAJO',
    'La Neuquinidad':    'LA NEUQUINIDAD',
    'Por Santa Cruz':    'POR SANTA CRUZ',
    'Coal. Cívica':      'COALICION CIVICA',
    'Independencia':     'INDEPENDENCIA',
    'MID':               'MID - MOVIMIENTO DE INTEGRACION Y DESARROLLO',
    'UCR':               'UCR - UNION CIVICA RADICAL',
    'PRO':               'PRO',
    'UXP':               'UNION POR LA PATRIA',
    'LLA':               'LA LIBERTAD AVANZA',
}
_PDF_BLOQUES_SORTED = sorted(PDF_BLOQUE_MAP.keys(), key=len, reverse=True)

# Noise patterns that appear after the bloque on a member line
_NOISE_RE = re.compile(
    r'\s+\d+[°º]\s+Competencia.*$'
    r'|\s+(Expedientes|Dictaminados)\s*$'
    r'|\s+\d+$',
    re.DOTALL | re.IGNORECASE,
)


def norm_key(s: str) -> str:
    """Unidecode + upper + strip punctuation/extra spaces for dict key."""
    s = unidecode(s).upper().strip()
    s = re.sub(r'["""\'`]', '', s)   # drop quotes/aliases
    s = re.sub(r'\s+', ' ', s)
    return s


# ── Build CSV lookup ───────────────────────────────────────────────────────────
def build_csv_lookup(csv_path: Path) -> dict:
    df = pd.read_csv(csv_path)
    lookup: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        apellido_raw = str(row['Apellido']).strip()
        nombre_raw   = str(row['Nombre']).strip()
        bloque_raw   = str(row['Bloque']).strip()

        # Remove alias in quotes from nombre (e.g. Ernesto Pipi"")
        nombre_clean = re.sub(r'\s*["""\'`][^"""\'`]*["""\'`]', '', nombre_raw).strip()

        nombre_completo = f"{apellido_raw}, {nombre_clean}"
        key = norm_key(apellido_raw)

        entry = {
            'nombre_completo': nombre_completo,
            'bloque': bloque_raw,
            'nombre_norm': norm_key(nombre_clean),
            'nombre_ini':  norm_key(nombre_clean)[:1],   # first initial
        }
        lookup.setdefault(key, []).append(entry)

    return lookup


def lookup_csv(csv_lookup: dict, apellido_pdf: str, nombre_pdf_hint: str) -> tuple[str, str]:
    """
    Returns (nombre_completo, bloque) from CSV, or ('', '') if not found.
    Tries compound apellido first, then first word only as fallback.
    """
    key_full = norm_key(apellido_pdf)
    candidates = csv_lookup.get(key_full, [])

    # Fallback: try first word of compound apellido
    if not candidates and ' ' in key_full:
        key_first = key_full.split()[0]
        candidates = csv_lookup.get(key_first, [])

    if not candidates:
        return '', ''

    if len(candidates) == 1:
        e = candidates[0]
        return e['nombre_completo'], e['bloque']

    # Multiple candidates → disambiguate by nombre initial or first name
    hint_norm = norm_key(nombre_pdf_hint)
    hint_ini  = hint_norm[:1] if hint_norm else ''

    # Exact first-name match
    for e in candidates:
        if e['nombre_norm'] and hint_norm and e['nombre_norm'].startswith(hint_norm[:3]):
            return e['nombre_completo'], e['bloque']

    # Initial match
    for e in candidates:
        if hint_ini and e['nombre_ini'] == hint_ini:
            return e['nombre_completo'], e['bloque']

    # Give up, return first
    return candidates[0]['nombre_completo'], candidates[0]['bloque']


# ── PDF member-line parsing ────────────────────────────────────────────────────
def strip_noise(text: str) -> str:
    return _NOISE_RE.sub('', text).strip()


def extract_bloque_pdf(rest: str) -> tuple[str, str]:
    """From 'Nombre Bloque trailing', extract (nombre_str, bloque_str)."""
    rest = strip_noise(rest)
    for key in _PDF_BLOQUES_SORTED:
        if rest.endswith(key):
            return rest[:-len(key)].strip(), key
    return rest, ''


def detect_cargo(text: str) -> tuple[str, str]:
    """
    Check if text starts with a cargo keyword.
    Returns (cargo_norm, remainder) or ('', text) if none found.
    """
    for cargo_norm, pattern in CARGO_KEYWORDS:
        m = re.match(r'^(?:' + pattern + r')\s+', text, re.UNICODE)
        if m:
            return cargo_norm, text[m.end():]
    return '', text


def parse_member_line(line: str, in_vocales: bool) -> dict | None:
    """
    Parses a single PDF text line into a member dict, or returns None.
    Expected input (already stripped): e.g.
        '1 Presidente MAYORAZ, Nicolás F. LLA'
        '8 ALÍ, Ernesto "Pipi" UXP 5º Competencia'
        'Secretario VACANTE 3º Competencia'
    """
    line = line.strip()
    if not line or ',' not in line:
        return None

    # Skip VACANTE
    if 'VACANTE' in line.upper():
        return None

    # Strip leading number
    text = re.sub(r'^\d+\s+', '', line)

    # Detect and strip cargo keyword
    cargo, text = detect_cargo(text)

    # If cargo == 'vocal', flip the flag but the member itself is still a vocal
    if cargo == 'vocal':
        cargo = 'vocal'
    elif cargo == '':
        cargo = 'vocal' if in_vocales else 'vocal'

    # Now text should be: APELLIDO[S], Nombre Bloque [trailing]
    if ',' not in text:
        return None

    comma = text.index(',')
    apellido = text[:comma].strip()
    rest     = text[comma + 1:].strip()

    # Skip lines that don't start with uppercase (noise/headers)
    if not apellido or not apellido[0].isupper():
        return None

    # Extract nombre hint and bloque from rest
    nombre_hint, bloque_pdf = extract_bloque_pdf(rest)
    # nombre_hint might be like "Nicolás F." or "Ernesto "Pipi""
    nombre_hint_clean = re.sub(r'\s*["""\'`][^"""\'`]*["""\'`]\s*', ' ', nombre_hint).strip()

    return {
        'apellido_pdf': apellido,
        'nombre_hint':  nombre_hint_clean,
        'bloque_pdf':   bloque_pdf,
        'cargo':        cargo,
    }


# ── Parse a single commission page ────────────────────────────────────────────
_HEADER_NOISE = re.compile(
    r'^(Reunión de Comisión|Comisiones|Permanentes|Período|Secretaría|Subdirección|'
    r'H\. Cámara|Cargo\s+Diputado|DIRECCION)',
    re.IGNORECASE,
)

def _clean_com_name(raw: str) -> str:
    """Strip 'de ' prefix before uppercase letters; fix known OCR artifacts."""
    raw = re.sub(r'^de\s+(?=[A-ZÁÉÍÓÚÑÜ])', '', raw)   # "de Asuntos..." → "Asuntos..."
    raw = re.sub(r'^del\s+', '', raw)                   # "del Mercosur" → "Mercosur"
    raw = raw.replace('ONG S', 'ONGs')                  # OCR artifact
    return raw.strip()


def parse_commission_page(text: str) -> dict | None:
    """Extract commission name, abbreviation, and raw member entries from a page."""
    lines = text.split('\n')

    # Find commission name (may span two lines)
    nombre_com = ''
    abrev_com  = ''
    name_line_idx = -1

    for i, line in enumerate(lines):
        m = re.match(r'^\d+\.\s+Comisi[oó]n\s+(.+)$', line.strip())
        if m:
            nombre_com = m.group(1).strip()
            name_line_idx = i
            # Check if next line is a continuation (not a header/noise line)
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and not _HEADER_NOISE.match(nxt) and not re.match(r'^\d+\.', nxt):
                    nombre_com = (nombre_com + ' ' + nxt).strip()
            nombre_com = _clean_com_name(nombre_com)
            break

    if not nombre_com:
        return None

    # Find abbreviation: first short alphanumeric token after "Comisiones" (anywhere in line)
    # Handles both "Comisiones\nABREV" and "Comisiones Reunión...\nABREV"
    for i, line in enumerate(lines):
        lstrip = line.strip()
        if lstrip.startswith('Comisiones'):
            for j in range(i + 1, min(i + 5, len(lines))):
                candidate = lines[j].strip()
                if re.match(r'^[A-Za-z0-9]{2,10}$', candidate) and candidate not in (
                    'de', 'del', 'la', 'las', 'los', 'en', 'al', 'Reunión',
                ):
                    abrev_com = candidate
                    break
            if abrev_com:
                break

    # Parse member lines
    members_raw = []
    in_member_section = False
    in_vocales = False

    for line in lines:
        lstrip = line.strip()

        if re.search(r'Cargo\s+Diputado', lstrip):
            in_member_section = True
            continue

        if 'Comunica su Constitución' in lstrip or 'DIRECCION COMISIONES' in lstrip:
            in_member_section = False
            continue

        if not in_member_section:
            continue

        # Skip pure noise lines
        if re.match(r'^(\d+[°º]\s+Competencia|Expedientes|Dictaminados|Reunión\s+Conjunta)$', lstrip, re.IGNORECASE):
            continue

        if not lstrip or lstrip in ('Bloque', 'Asistencia', 'Reunión', 'Conjunta'):
            continue

        parsed = parse_member_line(lstrip, in_vocales)
        if parsed is None:
            continue

        # Once we see first vocal (cargo='vocal' after "Vocales" keyword), set flag
        # The "Vocales" keyword is consumed by detect_cargo, setting cargo='vocal'
        # Check if the original line contained the "Vocales" keyword:
        if re.match(r'^\d+\s+Vocales?\s+', lstrip) or re.match(r'^Vocales?\s+', lstrip):
            in_vocales = True

        members_raw.append(parsed)

    return {
        'nombre':      nombre_com,
        'abreviatura': abrev_com,
        'members_raw': members_raw,
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print('Construyendo lookup desde CSV...', file=sys.stderr)
    csv_lookup = build_csv_lookup(CSV_PATH)
    print(f'  {sum(len(v) for v in csv_lookup.values())} diputados indexados, '
          f'{len(csv_lookup)} apellidos únicos.', file=sys.stderr)

    print('\nLeyendo PDF...', file=sys.stderr)
    comisiones_out = []
    no_match = []

    with pdfplumber.open(PDF_PATH) as pdf:
        total = len(pdf.pages)
        print(f'  Total páginas: {total} (saltando primeras 2)', file=sys.stderr)

        for page_idx, page in enumerate(pdf.pages[2:], start=3):
            text = page.extract_text() or ''
            parsed = parse_commission_page(text)
            if not parsed:
                print(f'  [!] Página {page_idx}: no se pudo extraer comisión', file=sys.stderr)
                continue

            integrantes = []
            for raw in parsed['members_raw']:
                nombre_csv, bloque_csv = lookup_csv(
                    csv_lookup, raw['apellido_pdf'], raw['nombre_hint']
                )

                if not nombre_csv:
                    # Fallback: use PDF data
                    bloque_fallback = PDF_BLOQUE_MAP.get(raw['bloque_pdf'], raw['bloque_pdf'])
                    nombre_fallback = f"{raw['apellido_pdf']}, {raw['nombre_hint']}".strip(', ')
                    no_match.append({
                        'comision': parsed['nombre'],
                        'apellido_pdf': raw['apellido_pdf'],
                        'nombre_hint':  raw['nombre_hint'],
                        'bloque_pdf':   raw['bloque_pdf'],
                    })
                    integrantes.append({
                        'nombre': nombre_fallback,
                        'bloque': bloque_fallback,
                        'cargo':  raw['cargo'],
                    })
                else:
                    integrantes.append({
                        'nombre': nombre_csv,
                        'bloque': bloque_csv,
                        'cargo':  raw['cargo'],
                    })

            comisiones_out.append({
                'nombre':      parsed['nombre'],
                'abreviatura': parsed['abreviatura'],
                'integrantes': integrantes,
            })
            print(f'  [{page_idx:2d}] {parsed["nombre"]} ({parsed["abreviatura"]}) '
                  f'→ {len(integrantes)} integrantes', file=sys.stderr)

    output = {
        'actualizado': '2026-04-27',
        'fuente':      'Planilla Dirección Comisiones HCDN — 27/04/2026',
        'comisiones':  comisiones_out,
    }

    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f'\n✓ Generado {OUT_PATH}', file=sys.stderr)
    print(f'  Comisiones: {len(comisiones_out)}', file=sys.stderr)

    total_int = sum(len(c['integrantes']) for c in comisiones_out)
    print(f'  Total integrantes: {total_int}', file=sys.stderr)
    print(f'  Sin cruce exitoso: {len(no_match)}', file=sys.stderr)

    if no_match:
        print('\n── Integrantes SIN CRUCE ─────────────────────────────────────────', file=sys.stderr)
        for e in no_match:
            print(f"  [{e['comision']}] {e['apellido_pdf']}, {e['nombre_hint']} "
                  f"(bloque PDF: {e['bloque_pdf']})", file=sys.stderr)


if __name__ == '__main__':
    main()
