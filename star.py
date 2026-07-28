#!/usr/bin/env python3
"""Da estrella en GitHub a los repos que @tom_doerr comparte en X.

Lee el timeline con Playwright usando cookies de sesion, intercepta las
respuestas del endpoint GraphQL UserTweets (trae las URLs ya expandidas)
y hace star via la API REST de GitHub.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

X_USER = os.environ.get("X_USER", "tom_doerr")
STATE = Path(__file__).parent / "state.json"
# Candidatos identificados pero aun no marcados. --dry-run los acumula aqui,
# --star-pending los consume. Asi el scraping y el marcado son pasos separados.
PENDING = Path(__file__).parent / "pending.json"
# .get() y no [] : permite importar el modulo para tests sin el entorno completo.
GH_TOKEN = os.environ.get("GH_TOKEN", "")
AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN", "")
CT0 = os.environ.get("X_CT0", "")

# ---------------------------------------------------------------- filtros ---
# Los tres estan APAGADOS por defecto. Se activan por variable de entorno y se
# pueden combinar (se aplican en cascada: si uno rechaza, no se marca).
#
#   MAX_AGE_MONTHS=6           -> solo posts de los ultimos 6 meses
#   KEYWORDS=python,cli,agent  -> solo si el texto del post menciona alguna
#   MIN_STARS=100              -> solo repos con >= 100 estrellas
# `or 0` y no default "0": una variable definida pero vacia (lo que manda
# Actions cuando la Variable no existe) reventaria con int("").
MAX_AGE_MONTHS = int(os.environ.get("MAX_AGE_MONTHS") or 0)
KEYWORDS = [k.strip().lower() for k in (os.environ.get("KEYWORDS") or "").split(",") if k.strip()]
MIN_STARS = int(os.environ.get("MIN_STARS") or 0)

# github.com/owner/repo — descarta gist, orgs sueltas y rutas internas (/blog, /features...)
REPO_RE = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/?(?:[?#]|$)")
NOT_A_USER = {"orgs", "features", "topics", "collections", "sponsors", "marketplace", "apps", "settings"}

# Cuantos scrolls sin tweets nuevos antes de asumir que llegamos al final.
# Ojo: cuando X throttlea tarda 10-30s en servir el siguiente lote. Un limite
# bajo hace que el script concluya "fin del timeline" cuando en realidad solo
# estaba esperando — un falso negativo silencioso.
STALL_LIMIT = 12
WAIT_MS = 3000          # espera base entre scrolls
WAIT_MAX_MS = 15000     # tope del backoff cuando no llega contenido nuevo

# Tope de posts a recorrer. X corta el timeline de perfil alrededor de los 800,
# asi que pasar de ahi solo gasta tiempo. Cuenta posts VISTOS (con o sin
# enlace), que es lo que realmente limita X — no los que traen GitHub.
MAX_POSTS = int(os.environ.get("MAX_POSTS") or 800)

# Epoch del snowflake de Twitter (2010-11-04), en ms.
SNOWFLAKE_EPOCH_MS = 1288834974657

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_id": None, "starred": []}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def tweet_date(tweet_id):
    """Fecha UTC del post, derivada del propio ID.

    Los IDs de X son snowflakes: los 41 bits altos son el timestamp en ms
    desde 2010-11-04. Asi el filtro por antiguedad no cuesta ni una llamada
    de red ni un campo extra del JSON.
    """
    ms = (int(tweet_id) >> 22) + SNOWFLAKE_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def walk_tweets(obj):
    """Saca (id_str, [urls], texto) de cada tweet en un JSON de X.

    Busca recursivamente en vez de navegar la ruta exacta: X cambia el
    esquema de timeline_v2 cada pocos meses, esto lo aguanta.
    """
    if isinstance(obj, dict):
        if "id_str" in obj and isinstance(obj.get("entities"), dict):
            # Emite TODOS los posts, tengan enlace o no: el contador de posts
            # vistos es lo que se compara contra el tope de X.
            urls = [u.get("expanded_url", "") for u in obj["entities"].get("urls", [])]
            text = obj.get("full_text") or obj.get("text") or ""
            yield obj["id_str"], urls, text
        for v in obj.values():
            yield from walk_tweets(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_tweets(v)


def scrape(last_id):
    """Devuelve {tweet_id: {"urls": [...], "text": "..."}} hasta last_id.

    Si last_id es None hace backfill completo (X corta el scroll ~800 posts).
    """
    tweets = {}          # solo los que traen enlace
    seen_ids = set()     # todos, para medir contra el tope de X
    hit_checkpoint = False

    def on_response(resp):
        nonlocal hit_checkpoint
        if "UserTweets" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        for tid, urls, text in walk_tweets(data):
            # int() y no comparacion de strings: los IDs son numeros y
            # "999" > "1000" lexicograficamente.
            if last_id and int(tid) <= int(last_id):
                hit_checkpoint = True
                continue
            seen_ids.add(tid)
            if not urls:
                continue
            entry = tweets.setdefault(tid, {"urls": [], "text": ""})
            entry["urls"].extend(urls)
            entry["text"] = entry["text"] or text

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 2000})
        ctx.add_cookies([
            {"name": "auth_token", "value": AUTH_TOKEN, "domain": ".x.com", "path": "/"},
            {"name": "ct0", "value": CT0, "domain": ".x.com", "path": "/"},
        ])
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(f"https://x.com/{X_USER}", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        prev, stalls = 0, 0
        while stalls < STALL_LIMIT and not hit_checkpoint and len(seen_ids) < MAX_POSTS:
            page.mouse.wheel(0, 4000)
            # Backoff: cada estancamiento consecutivo espera mas, por si X
            # esta throttleando en vez de haberse quedado sin contenido.
            page.wait_for_timeout(min(WAIT_MS * (1 + stalls), WAIT_MAX_MS))
            if len(seen_ids) == prev:
                stalls += 1
            else:
                stalls = 0
                prev = len(seen_ids)
                print(f"  ... {len(seen_ids)} posts vistos, "
                      f"{len(tweets)} con enlace", file=sys.stderr)

        agotado = stalls >= STALL_LIMIT
        motivo = ("checkpoint" if hit_checkpoint else
                  f"tope {MAX_POSTS}" if len(seen_ids) >= MAX_POSTS else
                  f"sin contenido nuevo tras {STALL_LIMIT} intentos "
                  f"(fin del timeline O throttling de X — no distinguible)")
        print(f"  scroll detenido: {motivo}", file=sys.stderr)
        if agotado and len(seen_ids) < 200:
            print("  AVISO: pocos posts para ser el fin real del timeline. "
                  "Probable rate limit; reintenta en 15-30 min.", file=sys.stderr)
        browser.close()
    return tweets


def extract_repos(tweets):
    """{owner/repo: tweet_id} de los tweets, mas reciente primero."""
    repos = {}
    for tid in sorted(tweets, key=int, reverse=True):
        for url in tweets[tid]["urls"]:
            m = REPO_RE.match(url or "")
            if not m:
                continue
            owner, repo = m.group(1), m.group(2).removesuffix(".git")
            if owner in NOT_A_USER:
                continue
            repos.setdefault(f"{owner}/{repo}", tid)
    return repos


def repo_stars(full_name):
    """Estrellas del repo, o -1 si no se pudo consultar (no bloquea)."""
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}",
                         headers=GH_HEADERS, timeout=30)
        return r.json().get("stargazers_count", -1) if r.status_code == 200 else -1
    except requests.RequestException:
        return -1


def should_star(full_name, tweet_id, tweets):
    """(bool, motivo). Filtros baratos primero: fecha y keywords no cuestan red,
    el umbral de estrellas si (1 GET por repo), asi que va al final."""
    if MAX_AGE_MONTHS:
        limite = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_MONTHS * 30)
        if tweet_date(tweet_id) < limite:
            return False, f"post de {tweet_date(tweet_id):%Y-%m}"

    if KEYWORDS:
        texto = tweets.get(tweet_id, {}).get("text", "").lower()
        if not any(k in texto for k in KEYWORDS):
            return False, "sin keyword"

    if MIN_STARS:
        n = repo_stars(full_name)
        if 0 <= n < MIN_STARS:          # -1 = no se pudo saber -> pasa
            return False, f"{n} estrellas"

    return True, ""


def star(full_name):
    r = requests.put(
        f"https://api.github.com/user/starred/{full_name}",
        headers={**GH_HEADERS, "Content-Length": "0"},  # GitHub lo exige en este PUT
        timeout=30,
    )
    return r.status_code, r


def save_pending(pend):
    PENDING.write_text(json.dumps(pend, indent=2, sort_keys=True) + "\n")


def merge_pending(previos, candidatos):
    """Fusiona candidatos sobre lo ya guardado, in-place. -> (nuevos, completados)

    Aditivo: nunca borra ni pisa un valor existente, pero si rellena los
    campos que quedaron vacios (p.ej. tweet_id=None de una corrida vieja).
    """
    nuevos = completados = 0
    for k, v in candidatos.items():
        if k not in previos:
            previos[k] = v
            nuevos += 1
        else:
            huecos = {kk: vv for kk, vv in v.items() if not previos[k].get(kk)}
            if huecos:
                previos[k].update(huecos)
                completados += 1
    return nuevos, completados


def star_pending():
    """Marca todo lo acumulado en pending.json. No toca X ni necesita cookies."""
    if not PENDING.exists():
        sys.exit("No hay pending.json todavia. Corre primero con --dry-run.")

    pend = json.loads(PENDING.read_text())
    state = load_state()
    already = set(state["starred"])
    cola = [k for k in sorted(pend) if k not in already]
    print(f"{len(cola)} por marcar (de {len(pend)} en pending.json)", file=sys.stderr)

    ok_n = fail_n = 0
    for i, full_name in enumerate(cola, 1):
        code, resp = star(full_name)
        if code == 204:
            state["starred"].append(full_name)
            pend.pop(full_name, None)
            ok_n += 1
            print(f"  * {full_name}")
        else:
            fail_n += 1
            print(f"  ! {full_name}: HTTP {code} {resp.text[:120]}", file=sys.stderr)
        time.sleep(0.5)
        # Checkpoint cada 25: una corrida de ~800 dura ~12 min, no queremos
        # perder el avance si se corta a mitad.
        if i % 25 == 0:
            save_state(state)
            save_pending(pend)
            print(f"  ({i}/{len(cola)})", file=sys.stderr)

    save_state(state)
    save_pending(pend)
    print(f"Listo. {ok_n} marcados, {fail_n} fallaron, "
          f"{len(pend)} siguen pendientes.", file=sys.stderr)


def main():
    # --star-pending: marca lo ya identificado, sin volver a leer X.
    if "--star-pending" in sys.argv:
        if not GH_TOKEN:
            sys.exit("Falta GH_TOKEN")
        return star_pending()

    # --dry-run: scrapea y lista, sin marcar ni tocar state.json. No necesita
    # el PAT de GitHub (salvo que uses MIN_STARS, que si consulta la API).
    dry = "--dry-run" in sys.argv
    requeridas = ["X_AUTH_TOKEN", "X_CT0"] + ([] if dry and not MIN_STARS else ["GH_TOKEN"])
    missing = [k for k in requeridas if not os.environ.get(k)]
    if missing:
        sys.exit(f"Faltan variables de entorno: {', '.join(missing)}")

    state = load_state()
    mode = "backfill completo" if not state["last_id"] else f"desde {state['last_id']}"
    activos = [f"{n}={v}" for n, v in
               [("MAX_AGE_MONTHS", MAX_AGE_MONTHS), ("KEYWORDS", ",".join(KEYWORDS)),
                ("MIN_STARS", MIN_STARS)] if v]
    print(f"Leyendo @{X_USER} ({mode}) | filtros: {' '.join(activos) or 'ninguno'}",
          file=sys.stderr)

    tweets = scrape(state["last_id"])
    if not tweets:
        print("Sin posts nuevos.", file=sys.stderr)
        return

    repos = extract_repos(tweets)
    already = set(state["starred"])
    print(f"{len(tweets)} posts -> {len(repos)} repos", file=sys.stderr)

    marcados = descartados = 0
    candidatos = {}
    for full_name, tid in repos.items():
        if full_name in already:
            continue
        ok, motivo = should_star(full_name, tid, tweets)
        if not ok:
            descartados += 1
            print(f"  - {full_name} ({motivo})", file=sys.stderr)
            continue
        if dry:
            marcados += 1
            candidatos[full_name] = {
                "tweet_id": tid,
                "date": f"{tweet_date(tid):%Y-%m-%d}",
                "post_url": f"https://x.com/{X_USER}/status/{tid}",
                # Contexto: de que va el repo, sin abrir 798 enlaces.
                "text": " ".join(tweets[tid]["text"].split())[:200],
            }
            print(f"  ? {full_name}  [{tweet_date(tid):%Y-%m-%d}]")
            continue
        code, resp = star(full_name)
        if code == 204:
            state["starred"].append(full_name)
            marcados += 1
            print(f"  * {full_name}")
        else:
            print(f"  ! {full_name}: HTTP {code} {resp.text[:120]}", file=sys.stderr)
        time.sleep(0.5)  # cortesia con la API

    if dry:
        # Merge aditivo: correr de nuevo amplia la lista en vez de
        # reemplazarla, y ademas rellena campos que quedaron vacios en
        # corridas anteriores (p.ej. tweet_id) sin pisar lo ya guardado.
        previos = json.loads(PENDING.read_text()) if PENDING.exists() else {}
        nuevos, completados = merge_pending(previos, candidatos)
        save_pending(previos)

        fechas = [tweet_date(t) for t in tweets]
        print(f"\n[dry-run] {marcados} candidatos: {nuevos} nuevos, "
              f"{completados} completados con datos que faltaban.\n"
              f"Rango de posts: {min(fechas):%Y-%m-%d} .. {max(fechas):%Y-%m-%d}\n"
              f"Guardados en pending.json: {len(previos)} en total.\n"
              f"Ninguna estrella fue puesta.", file=sys.stderr)
        return

    state["last_id"] = max(tweets, key=int) if not state["last_id"] else str(
        max(int(max(tweets, key=int)), int(state["last_id"]))
    )
    save_state(state)
    print(f"Listo. +{marcados} marcados, {descartados} descartados, "
          f"{len(state['starred'])} en total.", file=sys.stderr)


if __name__ == "__main__":
    main()
