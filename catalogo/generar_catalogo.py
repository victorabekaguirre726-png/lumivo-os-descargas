#!/usr/bin/env python3
"""
Lumivo OS — generador del catálogo de películas y series.

Corre una vez al día (GitHub Actions) y escribe catalogo.json, que Lumivo OS
descarga. Así las TVs no consultan las APIs directamente (menos llamadas, más rápido).

FUENTES (verificadas el 2026-11-09):
- Películas: Wikidata (datos CC0, uso comercial permitido, sin atribución obligatoria).
  Popularidad: visitas a Wikipedia en español (API de Wikimedia, CC0).
  Pósters: solo los carteles con licencia libre de Wikimedia Commons (propiedad P3383).
  No hay una fuente gratuita con pósters comerciales: para agregarla después,
  implementa otro proveedor que llene "poster"/"fondo" (ver PROVEEDORES_POSTER).
- Series: TVmaze (datos CC BY-SA 4.0: uso comercial permitido con atribución y enlace).
  Imágenes de TVmaze: se permite enlazarlas, pero no las licencia (son de los estudios);
  Lumivo OS las puede apagar desde contenido.json → "catalogo": {"imagenes": false}.

Solo usa la biblioteca estándar de Python 3.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "LumivoOS-catalogo/1.0 (https://grouplumivo.com/launcher/; tvlumivo@gmail.com) python-urllib"
HOY = dt.date.today()
SALIDA = sys.argv[1] if len(sys.argv) > 1 else "catalogo.json"
PAGINAS_TVMAZE = int(os.environ.get("PAGINAS_TVMAZE", "0"))  # 0 = todas


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pedir(url, cabeceras=None, intentos=4):
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity", **(cabeceras or {})})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            espera = int(e.headers.get("Retry-After", "0") or 0) or (2 + i * 3)
            log(f"  HTTP {e.code} en {url[:90]}… reintento en {espera}s")
            time.sleep(espera)
        except Exception as e:  # red inestable
            log(f"  error {e} en {url[:90]}… reintento")
            time.sleep(2 + i * 3)
    return None


# ----------------------------------------------------------------------------
# Wikidata (películas)
# ----------------------------------------------------------------------------
SPARQL = "https://query.wikidata.org/sparql"


def sparql(q):
    url = SPARQL + "?" + urllib.parse.urlencode({"query": q, "format": "json"})
    txt = pedir(url, {"Accept": "application/sparql-results+json"})
    if not txt:
        return []
    return json.loads(txt)["results"]["bindings"]


def qid(uri):
    return uri.rsplit("/", 1)[-1]


def candidatos_por_fecha(desde, hasta, min_links):
    # Consulta ligera (la precisión de la fecha se revisa después, en los detalles)
    q = f"""
    SELECT DISTINCT ?f ?links WHERE {{
      ?f wdt:P577 ?fecha.
      FILTER(?fecha >= "{desde}T00:00:00Z"^^xsd:dateTime && ?fecha <= "{hasta}T00:00:00Z"^^xsd:dateTime)
      ?f wdt:P31 wd:Q11424; wikibase:sitelinks ?links.
      FILTER(?links >= {min_links})
    }} LIMIT 800"""
    return {qid(b["f"]["value"]): int(b["links"]["value"]) for b in sparql(q)}


def candidatos_mejor_calificadas():
    # Calificación de la crítica registrada en Wikidata (Metacritic = Q150248)
    q = """
    SELECT ?f ?links ?score WHERE {
      ?f wdt:P31 wd:Q11424; wikibase:sitelinks ?links; p:P444 ?st.
      ?st ps:P444 ?score; pq:P447 wd:Q150248.
      FILTER(?links >= 60)
    }"""
    res = {}
    for b in sparql(q):
        m = re.match(r"(\d+)\s*/\s*100", b["score"]["value"])
        if m:
            res[qid(b["f"]["value"])] = (int(m.group(1)), int(b["links"]["value"]))
    return res


def limpiar_genero(g):
    g = re.sub(r"^(película|cine|filme|género)( de| del)? ", "", g.strip(), flags=re.I)
    return g[:1].upper() + g[1:] if g else g


def detalles_peliculas(ids):
    pelis = {}
    for i in range(0, len(ids), 60):
        lote = " ".join("wd:" + x for x in ids[i:i + 60])
        q = f"""
        SELECT ?f ?es ?en ?desc ?dur ?imdb ?poster ?eswiki ?links
          (GROUP_CONCAT(DISTINCT ?gen; separator="|") AS ?generos)
          (GROUP_CONCAT(DISTINCT ?dir; separator="|") AS ?directores)
          (GROUP_CONCAT(DISTINCT CONCAT(STR(?fecha), "@", STR(?prec), "@", COALESCE(STR(?lugar), "")); separator="|") AS ?fechas)
          (GROUP_CONCAT(DISTINCT CONCAT(STR(?sc), "@", STR(?por)); separator="|") AS ?scores)
        WHERE {{
          VALUES ?f {{ {lote} }}
          ?f wikibase:sitelinks ?links.
          OPTIONAL {{ ?f rdfs:label ?es FILTER(LANG(?es) = "es") }}
          OPTIONAL {{ ?f rdfs:label ?en FILTER(LANG(?en) = "en") }}
          OPTIONAL {{ ?f schema:description ?desc FILTER(LANG(?desc) = "es") }}
          OPTIONAL {{ ?f wdt:P2047 ?dur }}
          OPTIONAL {{ ?f wdt:P345 ?imdb }}
          OPTIONAL {{ ?f wdt:P3383 ?poster }}
          OPTIONAL {{ ?f wdt:P136 ?g. ?g rdfs:label ?gen FILTER(LANG(?gen) = "es") }}
          OPTIONAL {{ ?f wdt:P57 ?d. ?d rdfs:label ?dir FILTER(LANG(?dir) IN ("es", "mul", "en")) }}
          OPTIONAL {{ ?f p:P577 ?pst. ?pst psv:P577 [wikibase:timeValue ?fecha; wikibase:timePrecision ?prec]. OPTIONAL {{ ?pst pq:P291 ?lugar }} }}
          OPTIONAL {{ ?f p:P444 ?rs. ?rs ps:P444 ?sc; pq:P447 ?por. FILTER(?por IN (wd:Q150248, wd:Q105584)) }}
          OPTIONAL {{ ?art schema:about ?f; schema:isPartOf <https://es.wikipedia.org/>; schema:name ?eswiki }}
        }}
        GROUP BY ?f ?es ?en ?desc ?dur ?imdb ?poster ?eswiki ?links"""
        for b in sparql(q):
            v = lambda k: b.get(k, {}).get("value")
            fid = qid(v("f"))
            titulo = v("es") or v("en")
            if not titulo or re.match(r"^Q\d+$", titulo):
                continue
            # Quita aclaraciones tipo "(película de 2026)"
            titulo = re.sub(r"\s*\((película|filme|film)[^)]*\)$", "", titulo, flags=re.I).strip()
            # Fechas: la de México si existe; si no, la primera (estreno original), solo con día exacto
            fechas, fecha_mx = [], None
            for parte in (v("fechas") or "").split("|"):
                trozos = parte.split("@")
                if len(trozos) < 3 or int(trozos[1] or 0) < 11:
                    continue
                f = trozos[0][:10]
                fechas.append(f)
                if trozos[2].endswith("/Q96"):
                    fecha_mx = min(fecha_mx, f) if fecha_mx else f
            if not fechas:
                continue
            calif = []
            for parte in (v("scores") or "").split("|"):
                if "@" not in parte:
                    continue
                sc, por = parte.split("@", 1)
                fuente = "Metacritic" if por.endswith("Q150248") else "Rotten Tomatoes"
                # Rotten Tomatoes: solo el porcentaje de la crítica (no el promedio /10)
                if fuente == "Rotten Tomatoes" and not sc.strip().endswith("%"):
                    continue
                if not any(c["fuente"] == fuente for c in calif):
                    calif.append({"fuente": fuente, "valor": sc})
            calif.sort(key=lambda c: c["fuente"] != "Metacritic")
            previo = pelis.get(fid, {})
            dur = v("dur")
            pelis[fid] = {
                "id": fid,
                "tipo": "pelicula",
                "titulo": titulo,
                "original": v("en") if v("en") and v("en") != titulo else None,
                "descripcion": v("desc"),
                "estreno": min(fechas),
                "estrenoMexico": fecha_mx,
                "anio": int(min(fechas)[:4]),
                "duracion": previo.get("duracion") or (int(float(dur)) if dur else None),
                "generos": [limpiar_genero(x) for x in (v("generos") or "").split("|") if x][:3],
                "director": list(dict.fromkeys(x for x in (v("directores") or "").split("|") if x))[:2],
                "calificaciones": calif,
                "imdb": v("imdb"),
                "wikipedia": v("eswiki"),
                "links": int(v("links") or 0),
                # Solo carteles con licencia libre en Wikimedia Commons
                "poster": commons(v("poster")) if v("poster") else None,
                "fuente": "Wikidata",
                "url": "https://www.wikidata.org/wiki/" + fid,
            }
        time.sleep(1)
    return pelis


def commons(url_archivo):
    nombre = urllib.parse.unquote(url_archivo.rsplit("/", 1)[-1])
    return "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(nombre) + "?width=500"


def visitas_wikipedia(titulo):
    fin = HOY - dt.timedelta(days=1)
    ini = fin - dt.timedelta(days=29)
    art = urllib.parse.quote(titulo.replace(" ", "_"), safe="")
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/es.wikipedia/all-access/user/"
           f"{art}/daily/{ini:%Y%m%d}/{fin:%Y%m%d}")
    txt = pedir(url, intentos=2)
    if not txt:
        return 0
    return sum(x.get("views", 0) for x in json.loads(txt).get("items", []))


def fecha_ref(p):
    return p.get("estrenoMexico") or p["estreno"]


def generar_peliculas():
    log("Películas: buscando candidatas en Wikidata…")
    hace_un_anio = HOY - dt.timedelta(days=400)
    en_medio_anio = HOY + dt.timedelta(days=210)
    cand = candidatos_por_fecha(hace_un_anio.isoformat(), HOY.isoformat(), 12)
    # Las que aún no se estrenan tienen menos artículos en Wikipedia: umbral más bajo
    cand.update(candidatos_por_fecha((HOY + dt.timedelta(days=1)).isoformat(), en_medio_anio.isoformat(), 5))
    log(f"  {len(cand)} candidatas por fecha")
    top = candidatos_mejor_calificadas()
    log(f"  {len(top)} con calificación de Metacritic")
    mejores = sorted(top.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:60]
    ids = list(dict.fromkeys(list(cand.keys()) + [k for k, _ in mejores]))
    pelis = detalles_peliculas(ids)
    log(f"  {len(pelis)} con datos completos")

    # Popularidad: visitas a Wikipedia en español (últimos 30 días)
    recientes = [p for p in pelis.values() if hace_un_anio.isoformat() <= fecha_ref(p) <= en_medio_anio.isoformat()]
    for p in recientes:
        p["visitas"] = visitas_wikipedia(p["wikipedia"]) if p.get("wikipedia") else 0
        time.sleep(0.15)

    hoy = HOY.isoformat()
    pop = sorted([p for p in recientes if fecha_ref(p) <= hoy and p.get("visitas")], key=lambda p: -p["visitas"])[:24]
    hace_90 = (HOY - dt.timedelta(days=90)).isoformat()
    estrenos = sorted([p for p in recientes if hace_90 <= fecha_ref(p) <= hoy], key=lambda p: (-(p.get("visitas") or 0)))[:24]
    estrenos.sort(key=lambda p: fecha_ref(p), reverse=True)
    proximas = sorted([p for p in recientes if fecha_ref(p) > hoy], key=lambda p: (-(p.get("visitas") or 0), -p["links"]))[:24]
    proximas.sort(key=lambda p: fecha_ref(p))
    mejor = []
    for fid, (sc, _) in mejores:
        if fid in pelis:
            pelis[fid]["metacritic"] = sc
            mejor.append(pelis[fid])
    mejor = mejor[:24]
    return pelis, {"populares": pop, "estrenos": estrenos, "proximas": proximas, "mejores": mejor}


# ----------------------------------------------------------------------------
# TVmaze (series)
# ----------------------------------------------------------------------------
TVMAZE = "https://api.tvmaze.com"
GENEROS_ES = {
    "Action": "Acción", "Adult": "Adultos", "Adventure": "Aventura", "Anime": "Anime", "Children": "Infantil",
    "Comedy": "Comedia", "Crime": "Crimen", "DIY": "Hazlo tú mismo", "Drama": "Drama", "Espionage": "Espionaje",
    "Family": "Familiar", "Fantasy": "Fantasía", "Food": "Cocina", "History": "Historia", "Horror": "Terror",
    "Legal": "Legal", "Medical": "Médica", "Music": "Música", "Mystery": "Misterio", "Nature": "Naturaleza",
    "Romance": "Romance", "Science-Fiction": "Ciencia ficción", "Sports": "Deportes", "Supernatural": "Sobrenatural",
    "Thriller": "Suspenso", "Travel": "Viajes", "War": "Bélica", "Western": "Western",
}
ESTADOS_ES = {"Running": "En emisión", "Ended": "Finalizada", "To Be Determined": "Por confirmar", "In Development": "En desarrollo"}


def texto_plano(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def serie(s):
    img = s.get("image") or {}
    canal = (s.get("network") or {}).get("name") or (s.get("webChannel") or {}).get("name")
    return {
        "id": "tv" + str(s["id"]),
        "tvmaze": s["id"],
        "tipo": "serie",
        "titulo": s.get("name"),
        "anio": int(s["premiered"][:4]) if s.get("premiered") else None,
        "estreno": s.get("premiered"),
        "fin": s.get("ended"),
        "estado": ESTADOS_ES.get(s.get("status"), s.get("status")),
        "generos": [GENEROS_ES.get(g, g) for g in s.get("genres") or []][:3],
        "calificacion": (s.get("rating") or {}).get("average"),
        "duracion": s.get("averageRuntime") or s.get("runtime"),
        "idioma": s.get("language"),
        "canal": canal,
        "sinopsis": texto_plano(s.get("summary"))[:600],
        "sinopsisIdioma": "en",
        "poster": img.get("medium"),
        "posterGrande": img.get("original"),
        "url": s.get("url"),
        "peso": s.get("weight") or 0,
        "fuente": "TVmaze",
    }


def generar_series():
    log("Series: leyendo el índice de TVmaze…")
    todas, pagina = [], 0
    while True:
        txt = pedir(f"{TVMAZE}/shows?page={pagina}")
        if not txt:
            break
        lote = json.loads(txt)
        if not lote:
            break
        todas.extend(lote)
        pagina += 1
        if PAGINAS_TVMAZE and pagina >= PAGINAS_TVMAZE:
            break
        time.sleep(0.55)  # TVmaze: al menos 20 llamadas cada 10 s por IP
    log(f"  {len(todas)} series en {pagina} páginas")

    con_img = [s for s in todas if s.get("image") and s.get("name")]
    peso = lambda s: (s.get("weight") or 0, (s.get("rating") or {}).get("average") or 0)

    populares = sorted([s for s in con_img if s.get("status") in ("Running", "Ended") and (s.get("weight") or 0) >= 90],
                       key=lambda s: (-(s.get("weight") or 0), -((s.get("rating") or {}).get("average") or 0)))[:24]
    emision = sorted([s for s in con_img if s.get("status") == "Running"], key=lambda s: tuple(-x for x in peso(s)))[:24]
    recomendadas = sorted([s for s in con_img if ((s.get("rating") or {}).get("average") or 0) >= 8.3 and (s.get("weight") or 0) >= 80],
                          key=lambda s: (-((s.get("rating") or {}).get("average") or 0), -(s.get("weight") or 0)))[:24]
    anime = sorted([s for s in con_img if "Anime" in (s.get("genres") or [])], key=lambda s: tuple(-x for x in peso(s)))[:24]

    series = {}
    listas = {}
    for nombre, lista in (("populares", populares), ("emision", emision), ("recomendadas", recomendadas), ("anime", anime)):
        listas[nombre] = []
        for s in lista:
            o = serie(s)
            series[o["id"]] = o
            listas[nombre].append(o["id"])

    # Imagen panorámica (fondo) para el banner: series en emisión más vistas
    for sid in listas["emision"][:8]:
        txt = pedir(f"{TVMAZE}/shows/{series[sid]['tvmaze']}/images")
        if txt:
            fondos = [i for i in json.loads(txt) if i.get("type") == "background"]
            fondos.sort(key=lambda i: (not i.get("main"), -(((i.get("resolutions") or {}).get("original") or {}).get("width") or 0)))
            if fondos:
                series[sid]["fondo"] = fondos[0]["resolutions"]["original"]["url"]
        time.sleep(0.55)
    return series, listas


# ----------------------------------------------------------------------------
def main():
    pelis, lp = generar_peliculas()
    series, ls = generar_series()

    usados = {}
    for lista in lp.values():
        for p in lista:
            usados[p["id"]] = p
    for p in usados.values():
        p.pop("links", None)

    # Banner: estrenos de películas (los más vistos) intercalados con series en emisión con imagen panorámica
    banner = []
    estrenos_b = [p["id"] for p in sorted(lp["estrenos"], key=lambda p: -(p.get("visitas") or 0))[:4]]
    series_b = [sid for sid in ls["emision"] if series[sid].get("fondo")][:4]
    for i in range(max(len(estrenos_b), len(series_b))):
        if i < len(estrenos_b):
            banner.append(estrenos_b[i])
        if i < len(series_b):
            banner.append(series_b[i])

    catalogo = {
        "version": 1,
        "generado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fuentes": {
            "peliculas": "Datos de películas: Wikidata (CC0). Popularidad: visitas a Wikipedia en español (CC0). Carteles: solo con licencia libre de Wikimedia Commons.",
            "series": "Datos de series: TVmaze (tvmaze.com), licencia CC BY-SA 4.0.",
        },
        "banner": banner,
        "secciones": [
            {"id": "pel-populares", "titulo": "Películas populares", "items": [p["id"] for p in lp["populares"]]},
            {"id": "pel-estrenos", "titulo": "Estrenos recientes", "items": [p["id"] for p in lp["estrenos"]]},
            {"id": "pel-proximas", "titulo": "Próximamente en cines", "items": [p["id"] for p in lp["proximas"]]},
            {"id": "pel-mejores", "titulo": "Películas mejor calificadas", "items": [p["id"] for p in lp["mejores"]]},
            {"id": "ser-populares", "titulo": "Series populares", "items": ls["populares"]},
            {"id": "ser-recomendadas", "titulo": "Series recomendadas", "items": ls["recomendadas"]},
            {"id": "ser-emision", "titulo": "Series en emisión", "items": ls["emision"]},
            {"id": "anime", "titulo": "Anime destacado", "items": ls["anime"]},
        ],
        "titulos": {**usados, **series},
    }
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(catalogo, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Listo: {SALIDA} ({os.path.getsize(SALIDA) // 1024} KB), {len(catalogo['titulos'])} títulos")
    for s in catalogo["secciones"]:
        log(f"  {s['titulo']}: {len(s['items'])}")


if __name__ == "__main__":
    main()
