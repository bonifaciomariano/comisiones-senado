#!/usr/bin/env python3
"""
Exporta estadísticas de comisiones legislativas (Senado, Diputados, Bicamerales)
a data/estadisticas_comisiones.json y data/estadisticas_comisiones.csv
"""

import json
import csv
import re
import os
from collections import OrderedDict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(BASE, "index.html")
DIP_PATH  = os.path.join(BASE, "data", "diputados_comisiones.json")
BIC_PATH  = os.path.join(BASE, "data", "bicamerales.json")
JSON_OUT  = os.path.join(BASE, "data", "estadisticas_comisiones.json")
CSV_OUT   = os.path.join(BASE, "data", "estadisticas_comisiones.csv")


# ──────────────────────────────────────────────────────────────────────────────
# 1. Parsear index.html — SENATOR_BLOQUES, ALL_COMISIONES, RAW_CHANGES, AUTORIDADES
# ──────────────────────────────────────────────────────────────────────────────

def load_html():
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read()


def parse_js_object_simple(text, var_name):
    """Extrae un objeto JS {key:value, ...} como dict Python (solo strings)."""
    pattern = rf"const {re.escape(var_name)}\s*=\s*\{{(.*?)\}};"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return {}
    body = m.group(1)
    result = {}
    for kv in re.finditer(r'"([^"]+)"\s*:\s*"([^"]*)"', body):
        result[kv.group(1)] = kv.group(2)
    return result


def parse_all_comisiones(text):
    """Parsea ALL_COMISIONES = [{nombre:'...', cupo:N}, ...]"""
    m = re.search(r"const ALL_COMISIONES\s*=\s*\[(.*?)\];", text, re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    result = []
    for entry in re.finditer(r"\{nombre:'([^']+)',\s*cupo:(\d+)\}", body):
        result.append({"nombre": entry.group(1), "cupo": int(entry.group(2))})
    return result


def parse_raw_changes(text):
    """Parsea RAW_CHANGES = [{dpp:..., comision:..., tipo:..., senador:..., reemplaza:...}, ...]"""
    m = re.search(r"const RAW_CHANGES\s*=\s*\[(.*?)\];", text, re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    result = []
    for entry in re.finditer(r"\{([^}]+)\}", body):
        raw = entry.group(1)
        def get_field(name):
            fm = re.search(rf"{name}:'([^']*)'", raw)
            return fm.group(1) if fm else None
        result.append({
            "dpp":       get_field("dpp"),
            "comision":  get_field("comision"),
            "tipo":      get_field("tipo"),
            "senador":   get_field("senador"),
            "reemplaza": get_field("reemplaza"),
        })
    return result


def parse_autoridades(text, var_name="AUTORIDADES"):
    """Parsea AUTORIDADES = {'nombre': {pres:'...', vice:'...', secr:'...'}, ...}"""
    pattern = rf"const {re.escape(var_name)}\s*=\s*\{{(.*?)\}};\s*// ── {re.escape(var_name)}"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        # fallback: intentar sin comentario final
        pattern2 = rf"const {re.escape(var_name)}\s*=\s*\{{(.*?)\}};"
        m = re.search(pattern2, text, re.DOTALL)
    if not m:
        return {}
    body = m.group(1)
    result = {}
    for entry in re.finditer(r"'([^']+)'\s*:\s*\{pres:'([^']*)',\s*vice:'([^']*)',\s*secr:'([^']*)'\}", body):
        result[entry.group(1)] = {
            "pres": entry.group(2),
            "vice": entry.group(3),
            "secr": entry.group(4),
        }
    return result


def normalize_name(s):
    if not s:
        return s
    s = re.sub(r"^AVILA,", "ÁVILA,", s)
    s = re.sub(r"^ESPINOLA,", "ESPÍNOLA,", s)
    return s


def build_state(raw_changes):
    """Replica buildState() del JS para reconstruir el estado actual de cada comisión."""
    state = {}  # comision -> list of senator names (ordered)

    pending_rewrites = {}  # comision -> {dpp, members[]}

    for c in raw_changes:
        if c["senador"]:
            c["senador"] = normalize_name(c["senador"])
        if c["reemplaza"]:
            c["reemplaza"] = normalize_name(c["reemplaza"])

        comision = c["comision"]
        tipo = c["tipo"]

        if comision not in state:
            state[comision] = []

        if tipo == "rewrite":
            if comision not in pending_rewrites:
                pending_rewrites[comision] = {"dpp": c["dpp"], "members": []}
            if pending_rewrites[comision]["dpp"] != c["dpp"]:
                state[comision] = list(pending_rewrites[comision]["members"])
                pending_rewrites[comision] = {"dpp": c["dpp"], "members": []}
            if c["senador"] not in pending_rewrites[comision]["members"]:
                pending_rewrites[comision]["members"].append(c["senador"])
        else:
            if comision in pending_rewrites:
                state[comision] = list(pending_rewrites[comision]["members"])
                del pending_rewrites[comision]

            com = state[comision]
            if tipo == "add":
                if c["senador"] not in com:
                    com.append(c["senador"])
            elif tipo == "replace":
                rep = c["reemplaza"]
                if rep in com:
                    com.remove(rep)
                if c["senador"] not in com:
                    com.append(c["senador"])
            elif tipo == "remove":
                if c["senador"] in com:
                    com.remove(c["senador"])

    # Aplicar rewrites pendientes al final
    for comision, rw in pending_rewrites.items():
        state[comision] = list(rw["members"])

    return state


def get_role_senado(autoridades, comision, nombre):
    a = autoridades.get(comision, {})
    if a.get("pres") == nombre:
        return "Presidente"
    if a.get("vice") == nombre:
        return "Vicepresidente"
    if a.get("secr") == nombre:
        return "Secretario/a"
    return "Vocal"


def get_role_bic(autoridades_bic, comision, nombre):
    a = autoridades_bic.get(comision, {})
    if a.get("pres") == nombre:
        return "Presidente"
    if a.get("vice") == nombre:
        return "Vicepresidente"
    if a.get("secr") == nombre:
        return "Secretario/a"
    return "Vocal"


# ──────────────────────────────────────────────────────────────────────────────
# 2. Construir datos de cada cámara
# ──────────────────────────────────────────────────────────────────────────────

def build_senado(html):
    senator_bloques = parse_js_object_simple(html, "SENATOR_BLOQUES")
    all_comisiones  = parse_all_comisiones(html)
    raw_changes     = parse_raw_changes(html)
    autoridades     = parse_autoridades(html, "AUTORIDADES")
    state           = build_state(raw_changes)

    cupo_map = {c["nombre"]: c["cupo"] for c in all_comisiones}

    comisiones = []
    for com_def in all_comisiones:
        nombre = com_def["nombre"]
        cupo   = com_def["cupo"]
        members = state.get(nombre, [])

        integrantes = []
        for sen in members:
            bloque = senator_bloques.get(sen, "DESCONOCIDO")
            cargo  = get_role_senado(autoridades, nombre, sen)
            integrantes.append({"nombre": sen, "bloque": bloque, "cargo": cargo})

        aut = autoridades.get(nombre, {})
        tiene_pres = bool(aut.get("pres"))

        comisiones.append({
            "camara":             "senado",
            "nombre":             nombre,
            "total_reglamentario": cupo,
            "total_actuales":     len(members),
            "vacantes":           cupo - len(members),
            "tiene_presidente":   tiene_pres,
            "integrantes":        integrantes,
        })

    return comisiones


def build_diputados():
    with open(DIP_PATH, encoding="utf-8") as f:
        data = json.load(f)

    comisiones = []
    for com in data["comisiones"]:
        nombre      = com["nombre"]
        integrantes_raw = com.get("integrantes", [])
        total       = len(integrantes_raw)

        integrantes = []
        tiene_pres  = False
        for ing in integrantes_raw:
            cargo = ing.get("cargo", "vocal") or "vocal"
            if "president" in cargo.lower():
                tiene_pres = True
                cargo_norm = "Presidente"
            elif "vicepresidente_1" in cargo.lower():
                cargo_norm = "Vicepresidente 1°"
            elif "vicepresidente_2" in cargo.lower():
                cargo_norm = "Vicepresidente 2°"
            elif "vicepresidente" in cargo.lower():
                cargo_norm = "Vicepresidente"
            elif "secret" in cargo.lower():
                cargo_norm = "Secretario/a"
            else:
                cargo_norm = "Vocal"
            integrantes.append({
                "nombre": ing["nombre"],
                "bloque": ing.get("bloque", ""),
                "cargo":  cargo_norm,
            })

        comisiones.append({
            "camara":             "diputados",
            "nombre":             nombre,
            "total_reglamentario": total,
            "total_actuales":     total,
            "vacantes":           0,
            "tiene_presidente":   tiene_pres,
            "integrantes":        integrantes,
        })

    return comisiones


def build_bicamerales(html):
    if not os.path.exists(BIC_PATH):
        return []

    autoridades_bic = parse_autoridades(html, "AUTORIDADES_BIC")

    with open(BIC_PATH, encoding="utf-8") as f:
        data = json.load(f)

    comisiones = []
    for com in data["comisiones"]:
        nombre = com["nombre"]
        integrantes_raw = [i for i in com.get("integrantes", []) if not i.get("vacante", False)]
        total = len(integrantes_raw)

        aut = autoridades_bic.get(nombre, {})
        tiene_pres = bool(aut.get("pres"))

        integrantes = []
        for ing in integrantes_raw:
            cargo = get_role_bic(autoridades_bic, nombre, ing["nombre"])
            integrantes.append({
                "nombre": ing["nombre"],
                "bloque": ing.get("bloque", ""),
                "cargo":  cargo,
                "camara_origen": ing.get("camara", ""),
            })

        comisiones.append({
            "camara":             "bicamerales",
            "nombre":             nombre,
            "total_reglamentario": total,
            "total_actuales":     total,
            "vacantes":           0,
            "tiene_presidente":   tiene_pres,
            "integrantes":        integrantes,
        })

    return comisiones


# ──────────────────────────────────────────────────────────────────────────────
# 3. Resumen por cámara
# ──────────────────────────────────────────────────────────────────────────────

def build_resumen(comisiones, camara):
    total_comisiones    = len(comisiones)
    con_presidente      = sum(1 for c in comisiones if c["tiene_presidente"])
    sin_presidente      = total_comisiones - con_presidente
    total_reglamentario = sum(c["total_reglamentario"] for c in comisiones)
    total_actuales      = sum(c["total_actuales"] for c in comisiones)
    total_vacantes      = sum(c["vacantes"] for c in comisiones)

    bloque_count = {}
    for com in comisiones:
        for ing in com["integrantes"]:
            b = ing.get("bloque") or "SIN BLOQUE"
            bloque_count[b] = bloque_count.get(b, 0) + 1

    composicion_bloques = {}
    for bloque, count in sorted(bloque_count.items(), key=lambda x: -x[1]):
        pct = round(count / total_actuales * 100, 2) if total_actuales else 0
        composicion_bloques[bloque] = {"bancas_ocupadas": count, "porcentaje": pct}

    return {
        "camara":               camara,
        "total_comisiones":     total_comisiones,
        "con_presidente":       con_presidente,
        "sin_presidente":       sin_presidente,
        "total_reglamentario":  total_reglamentario,
        "total_actuales":       total_actuales,
        "total_vacantes":       total_vacantes,
        "composicion_bloques":  composicion_bloques,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4. Imprimir resumen en pantalla
# ──────────────────────────────────────────────────────────────────────────────

def print_resumen(resumen):
    print(f"\n{'='*60}")
    print(f"  CÁMARA: {resumen['camara'].upper()}")
    print(f"{'='*60}")
    print(f"  Comisiones totales     : {resumen['total_comisiones']}")
    print(f"  Con presidente         : {resumen['con_presidente']}")
    print(f"  Sin presidente         : {resumen['sin_presidente']}")
    print(f"  Bancas reglamentarias  : {resumen['total_reglamentario']}")
    print(f"  Integrantes actuales   : {resumen['total_actuales']}")
    print(f"  Vacantes totales       : {resumen['total_vacantes']}")
    print(f"\n  Composición por bloque:")
    for bloque, datos in resumen["composicion_bloques"].items():
        print(f"    {bloque:<55} {datos['bancas_ocupadas']:>4}  ({datos['porcentaje']:>5.1f}%)")


# ──────────────────────────────────────────────────────────────────────────────
# 5. Exportar JSON y CSV
# ──────────────────────────────────────────────────────────────────────────────

def export_json(all_comisiones, resumenes):
    output = {
        "resumenes": resumenes,
        "comisiones": all_comisiones,
    }
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✓ JSON guardado en {JSON_OUT}")


def export_csv(all_comisiones):
    rows = []
    for com in all_comisiones:
        camara   = com["camara"]
        nombre   = com["nombre"]
        for ing in com["integrantes"]:
            rows.append({
                "camara":   camara,
                "comision": nombre,
                "nombre":   ing["nombre"],
                "bloque":   ing.get("bloque", ""),
                "cargo":    ing.get("cargo", ""),
            })

    with open(CSV_OUT, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["camara", "comision", "nombre", "bloque", "cargo"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"✓ CSV guardado en {CSV_OUT}")


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    html = load_html()

    print("Procesando Senado...")
    senado_coms = build_senado(html)

    print("Procesando Diputados...")
    dip_coms = build_diputados()

    print("Procesando Bicamerales...")
    bic_coms = build_bicamerales(html)

    all_comisiones = senado_coms + dip_coms + bic_coms

    resumenes = [
        build_resumen(senado_coms, "senado"),
        build_resumen(dip_coms,    "diputados"),
        build_resumen(bic_coms,    "bicamerales"),
    ]

    for r in resumenes:
        print_resumen(r)

    export_json(all_comisiones, resumenes)
    export_csv(all_comisiones)


if __name__ == "__main__":
    main()
