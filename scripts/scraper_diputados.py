#!/usr/bin/env python3
"""
Scraper de comisiones permanentes de la Cámara de Diputados de la Nación Argentina.
Fuente: https://www.hcdn.gob.ar/comisiones/permanentes/
Genera: data/diputados_comisiones.json
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timezone

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'requests', 'beautifulsoup4', '-q'])
    import requests
    from bs4 import BeautifulSoup

BASE_URL = 'https://www.hcdn.gob.ar'
LIST_URL = f'{BASE_URL}/comisiones/permanentes/'

CARGO_MAP = {
    'presidente': 'presidente',
    'presidenta': 'presidente',
    'vicepresidente 1°': 'vicepresidente1',
    'vicepresidenta 1°': 'vicepresidente1',
    'vicepresidente 1': 'vicepresidente1',
    'vicepresidenta 1': 'vicepresidente1',
    'vicepresidente 2°': 'vicepresidente2',
    'vicepresidenta 2°': 'vicepresidente2',
    'vicepresidente 2': 'vicepresidente2',
    'vicepresidenta 2': 'vicepresidente2',
    'secretario': 'secretario',
    'secretaria': 'secretario',
    'vocal': 'vocal',
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ComisionesBot/1.0; +https://github.com/bonifaciomariano/comisiones-senado)'
}


def normalize_cargo(raw: str) -> str:
    if not raw:
        return 'vocal'
    lower = raw.lower().strip()
    for key, val in CARGO_MAP.items():
        if key in lower:
            return val
    return 'vocal'


def scrape_comision(abreviatura: str) -> dict | None:
    url = f'{BASE_URL}/comisiones/permanentes/{abreviatura}/integrantes.html'
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f'  ERROR fetching {url}: {e}', file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Nombre de la comisión
    nombre = ''
    h1 = soup.find('h1')
    if h1:
        nombre = h1.get_text(strip=True)
    if not nombre:
        title = soup.find('title')
        if title:
            nombre = title.get_text(strip=True).split('|')[0].strip()

    integrantes = []

    # Try primary table structure used by HCDN
    tables = soup.find_all('table')
    for table in tables:
        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2:
                continue
            nombre_cel = cells[0].get_text(strip=True)
            bloque_cel = cells[1].get_text(strip=True) if len(cells) > 1 else ''
            cargo_cel = cells[2].get_text(strip=True) if len(cells) > 2 else ''

            if not nombre_cel or nombre_cel.lower() in ('diputado/a', 'nombre', 'legislador', 'integrante'):
                continue

            integrantes.append({
                'nombre': nombre_cel,
                'bloque': bloque_cel,
                'cargo': normalize_cargo(cargo_cel),
            })

    # Fallback: look for divs/lists used in some pages
    if not integrantes:
        member_items = soup.select('.integrante, .diputado, .member, li.legislador')
        for item in member_items:
            nombre_el = item.find(class_=['nombre', 'name', 'diputado-nombre'])
            bloque_el = item.find(class_=['bloque', 'block'])
            cargo_el = item.find(class_=['cargo', 'role', 'autoridad'])
            nombre_text = nombre_el.get_text(strip=True) if nombre_el else item.get_text(strip=True)
            bloque_text = bloque_el.get_text(strip=True) if bloque_el else ''
            cargo_text = cargo_el.get_text(strip=True) if cargo_el else ''
            if nombre_text:
                integrantes.append({
                    'nombre': nombre_text,
                    'bloque': bloque_text,
                    'cargo': normalize_cargo(cargo_text),
                })

    return {
        'nombre': nombre,
        'abreviatura': abreviatura,
        'integrantes': integrantes,
    }


def scrape_comisiones_list() -> list[str]:
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f'ERROR fetching comisiones list: {e}', file=sys.stderr)
        sys.exit(1)

    soup = BeautifulSoup(resp.text, 'html.parser')
    abreviaturas = []

    for a in soup.find_all('a', href=True):
        href = a['href']
        if '/comisiones/permanentes/' in href and href.endswith('/'):
            parts = href.rstrip('/').split('/')
            if parts:
                abr = parts[-1]
                if abr and abr != 'permanentes' and abr not in abreviaturas:
                    abreviaturas.append(abr)

    return abreviaturas


def main():
    print('Obteniendo listado de comisiones permanentes...', file=sys.stderr)
    abreviaturas = scrape_comisiones_list()
    print(f'Encontradas {len(abreviaturas)} comisiones: {abreviaturas}', file=sys.stderr)

    comisiones = []
    errors = 0

    for abr in abreviaturas:
        print(f'  Scrapeando {abr}...', file=sys.stderr)
        result = scrape_comision(abr)
        if result is None:
            errors += 1
            continue
        comisiones.append(result)
        print(f'    -> {result["nombre"]} ({len(result["integrantes"])} integrantes)', file=sys.stderr)

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    output = {
        'actualizado': timestamp,
        'comisiones': comisiones,
    }

    out_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'diputados_comisiones.json')
    out_path = os.path.normpath(out_path)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f'\nGenerado {out_path}', file=sys.stderr)
    print(f'Comisiones: {len(comisiones)}, errores: {errors}', file=sys.stderr)

    if errors > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
