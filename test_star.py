#!/usr/bin/env python3
"""Self-check: python test_star.py (sin frameworks, sin entorno)."""
from datetime import datetime, timezone

import star


def test_walk_tweets_encuentra_anidados():
    # Forma real (recortada) de una respuesta UserTweets.
    payload = {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [
        {"entries": [{"content": {"itemContent": {"tweet_results": {"result": {"legacy": {
            "id_str": "2081916116302024731",
            "full_text": "A project-based guide to learn OpenCV in 3 hours using Python",
            "entities": {"urls": [
                {"expanded_url": "https://github.com/murtazahassan/Learn-OpenCV-in-3-hours"}
            ]},
        }}}}}}]}
    ]}}}}}}
    got = list(star.walk_tweets(payload))
    assert len(got) == 1, got
    tid, urls, text = got[0]
    assert tid == "2081916116302024731", tid
    assert urls == ["https://github.com/murtazahassan/Learn-OpenCV-in-3-hours"], urls
    assert "OpenCV" in text, text


def test_walk_tweets_emite_tambien_sin_enlaces():
    # Se emiten con urls=[] para poder contarlos contra el tope de X;
    # el filtrado ocurre en el consumidor, no aqui.
    got = list(star.walk_tweets({"id_str": "1", "entities": {"urls": []}}))
    assert got == [("1", [], "")], got


def _tw(urls, text=""):
    return {"urls": urls, "text": text}


def test_extract_repos_filtra_bien():
    tweets = {
        "300": _tw(["https://github.com/krypton-byte/neonize"]),
        "200": _tw(["https://github.com/features/copilot"]),       # ruta interna
        "100": _tw(["https://github.com/murtazahassan/Learn-OpenCV-in-3-hours"]),
        "90":  _tw(["https://example.com/nope"]),                  # no es github
        "80":  _tw(["https://github.com/torvalds/linux.git"]),     # sufijo .git
        "70":  _tw(["https://github.com/owner/repo/issues/42"]),   # subruta
    }
    got = star.extract_repos(tweets)
    assert "krypton-byte/neonize" in got, got
    assert "murtazahassan/Learn-OpenCV-in-3-hours" in got, got
    assert "torvalds/linux" in got, got            # .git recortado
    assert "features/copilot" not in got, got      # NOT_A_USER
    assert not any("example.com" in k for k in got), got
    assert "owner/repo" not in got, got            # subruta descartada


def test_ids_se_ordenan_como_numeros():
    # "999" > "1000" como string; el orden debe ser numerico.
    tweets = {"999": _tw(["https://github.com/a/viejo"]),
              "1000": _tw(["https://github.com/b/nuevo"])}
    assert list(star.extract_repos(tweets)) == ["b/nuevo", "a/viejo"]


def test_tweet_date_decodifica_snowflake():
    # El post real del ejemplo: 2026-07-28T01:34:16Z
    d = star.tweet_date("2081916116302024731")
    assert d.year == 2026 and d.month == 7 and d.day == 28, d
    assert d.tzinfo == timezone.utc, d


def test_filtro_edad():
    viejo = "1000000000000000000"   # 2018
    tweets = {viejo: _tw([], "algo")}
    star.MAX_AGE_MONTHS = 6
    try:
        ok, motivo = star.should_star("a/b", viejo, tweets)
        assert not ok and "post de" in motivo, (ok, motivo)
    finally:
        star.MAX_AGE_MONTHS = 0


def test_filtro_keywords():
    tweets = {"1": _tw([], "A high-performance Python library for WhatsApp")}
    star.KEYWORDS = ["python", "cli"]
    try:
        assert star.should_star("a/b", "1", tweets)[0]          # menciona python
        star.KEYWORDS = ["rust"]
        ok, motivo = star.should_star("a/b", "1", tweets)
        assert not ok and motivo == "sin keyword", (ok, motivo)
    finally:
        star.KEYWORDS = []


def test_merge_rellena_huecos_sin_pisar():
    previos = {
        "a/uno": {"tweet_id": None, "date": "2026-07-04"},          # hueco
        "a/dos": {"tweet_id": "999", "date": "2026-07-05"},         # completo
    }
    candidatos = {
        "a/uno": {"tweet_id": "111", "date": "2026-07-04", "text": "hola"},
        "a/dos": {"tweet_id": "222", "date": "2026-07-99", "text": "otro"},
        "a/tres": {"tweet_id": "333", "date": "2026-07-06", "text": "nuevo"},
    }
    nuevos, completados = star.merge_pending(previos, candidatos)

    # a/dos tambien cuenta como completado: le faltaba el campo `text`
    # entero (formato viejo). Un campo ausente es un hueco igual que un None.
    assert nuevos == 1 and completados == 2, (nuevos, completados)
    assert previos["a/uno"]["tweet_id"] == "111"      # hueco rellenado
    assert previos["a/uno"]["text"] == "hola"         # campo nuevo agregado
    assert previos["a/dos"]["tweet_id"] == "999"      # NO se piso
    assert previos["a/dos"]["date"] == "2026-07-05"   # NO se piso
    assert previos["a/dos"]["text"] == "otro"         # pero si se agrego
    assert "a/tres" in previos


def test_merge_es_idempotente():
    previos = {"a/uno": {"tweet_id": "111", "date": "2026-07-04"}}
    cand = {"a/uno": {"tweet_id": "111", "date": "2026-07-04"}}
    assert star.merge_pending(previos, cand) == (0, 0)


def test_sin_filtros_todo_pasa():
    assert star.should_star("a/b", "2081916116302024731", {}) == (True, "")


if __name__ == "__main__":
    for name, fn in sorted(vars().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\ntodo verde")
