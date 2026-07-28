# star-tom-doerr

Da estrella automáticamente a los repos de GitHub que [@tom_doerr](https://x.com/tom_doerr)
comparte en X. Corre cada 6 h en GitHub Actions.

## Cómo funciona

1. Playwright abre `x.com/tom_doerr` con tus cookies de sesión.
2. Intercepta las respuestas del endpoint GraphQL interno `UserTweets` — ese JSON
   trae `entities.urls[].expanded_url`, la URL **ya resuelta** (nada de seguir `t.co`).
3. Filtra las que sean `github.com/owner/repo`.
4. `PUT /user/starred/{owner}/{repo}` con tu token personal.
5. Guarda el último tweet visto en `state.json` y lo commitea, para que la
   siguiente corrida no reescanee todo.

La primera corrida hace backfill completo (X corta el scroll alrededor de los
800 posts). Las siguientes paran en cuanto ven el checkpoint.

## Setup

### 1. Cookies de X

En Chrome, con sesión iniciada en x.com: DevTools → Application → Cookies →
`https://x.com`. Copia los valores de **`auth_token`** y **`ct0`**.

> Duran varios meses. Cuando caduquen el workflow fallará con timeout — solo
> vuelve a copiarlas.

### 2. Token de GitHub

[Nuevo PAT clásico](https://github.com/settings/tokens/new) con scope **`public_repo`**.
El `GITHUB_TOKEN` que Actions inyecta por defecto **no sirve**: pertenece al repo,
no a ti, y las estrellas irían a la nada.

### 3. Secrets del repo

Settings → Secrets and variables → Actions:

| Secret | Valor |
|---|---|
| `X_AUTH_TOKEN` | cookie `auth_token` |
| `X_CT0` | cookie `ct0` |
| `STAR_TOKEN` | el PAT del paso 2 |

### 4. Subirlo

```bash
git init && git add . && git commit -m "init"
gh repo create star-tom-doerr --private --source=. --push
```

Luego Actions → `star-tom-doerr` → **Run workflow** para la primera corrida.

## Filtros (opcionales)

Los tres están **apagados** por defecto — sin configurar nada, marca todo repo
que aparezca. Se activan en Settings → Secrets and variables → Actions →
pestaña **Variables** (no son secretos). Se pueden combinar.

| Variable | Ejemplo | Qué hace | Coste |
|---|---|---|---|
| `MAX_AGE_MONTHS` | `6` | Solo posts de los últimos N meses | 0 — la fecha sale del propio ID del post (snowflake) |
| `KEYWORDS` | `python,cli,agent,rust` | Solo si el texto del post menciona alguna | 0 — el texto ya viene en el JSON |
| `MIN_STARS` | `100` | Solo repos con ≥ N estrellas | 1 GET por repo |

Se aplican en cascada y en ese orden: los gratis primero, el caro al final, así
un repo descartado por fecha nunca gasta una llamada a la API.

Si `MIN_STARS` no puede consultar un repo (privado, borrado, rate limit), **lo
deja pasar** en vez de descartarlo — mejor una estrella de más que perder un
hallazgo por un fallo de red.

El log dice siempre por qué se descartó cada uno:

```
  * krypton-byte/neonize
  - alguien/proyecto-viejo (post de 2024-03)
  - otro/cosa (12 estrellas)
```

## Local

```bash
pip install -r requirements.txt
playwright install chromium

export X_AUTH_TOKEN=... X_CT0=... GH_TOKEN=...
python star.py
```

Tests: `python test_star.py`

## Seguir a otra cuenta

`export X_USER=otro_usuario` (o añádelo al `env:` del workflow). Borra `state.json`
para forzar backfill desde cero.
