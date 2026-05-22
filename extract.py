import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    import boto3  # pyright: ignore[reportMissingImports]
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError  # pyright: ignore[reportMissingImports]
except ImportError:
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception
    NoCredentialsError = Exception

# ---------------------------------------------------------------------------
# Main configuration (edit here)
# ---------------------------------------------------------------------------

STEAM_API_KEY = "1A6A9917751AA6584621D8B62432F2C0"
OUTPUT_ROOT = Path("datosInfravaloradosSteam")

BASE_API = "https://api.steampowered.com"
BASE_STORE = "https://store.steampowered.com"
STORE_SEARCH = f"{BASE_STORE}/search/results/"
STEAMCHARTS_TOP = "https://steamcharts.com/top"

HEADERS = {
    "Accept-Language": "es-ES,es;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
DEFAULT_REQUEST_DELAY = 0.25

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Active profile for the current run.
# Options: "estricto" | "exploracion"
ACTIVE_PROFILE = "exploracion"

PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "exploracion": {
        "target": 50,
        "store_pages": 30,
        "store_page_size": 50,
        "store_language": "english",
        "steamcharts_pages": 25,
        "max_candidates": 4000,
        "max_evaluations": 200,
        "min_review_score": 9,
        "min_total_reviews": 80,
        "max_total_reviews": 25000,
        "allow_very_positive": False,
        "max_current_players": None,
        "skip_current_players": False,
        "sample_reviews": 5,
        "delay": 0.10,
    },
}

# Optional overrides applied on top of the active profile.
PROFILE_OVERRIDES: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# S3 configuration (edit here)
# ---------------------------------------------------------------------------

S3_UPLOAD_ENABLED = True
S3_BUCKET_NAME = "unam-2026-ingenieriadedatos-equipo3-660864588540-us-east-2-an"
S3_PREFIX = "1bronce"
S3_REGION = "us-east-2"
S3_INCLUDE_INDIVIDUAL_FILES = False


# ---------------------------------------------------------------------------
# Configuration utilities
# ---------------------------------------------------------------------------

def get_runtime_config() -> dict[str, Any]:
    if ACTIVE_PROFILE not in PROFILE_PRESETS:
        raise ValueError(
            f"Invalid ACTIVE_PROFILE: {ACTIVE_PROFILE}. "
            f"Options: {', '.join(sorted(PROFILE_PRESETS))}"
        )

    base_cfg = dict(PROFILE_PRESETS[ACTIVE_PROFILE])
    invalid_override_keys = sorted(set(PROFILE_OVERRIDES) - set(base_cfg))
    if invalid_override_keys:
        raise ValueError(
            "PROFILE_OVERRIDES contains invalid keys: "
            + ", ".join(invalid_override_keys)
        )

    base_cfg.update(PROFILE_OVERRIDES)
    return base_cfg


# ---------------------------------------------------------------------------
# HTTP helpers with retry logic
# ---------------------------------------------------------------------------

def request_json(url: str, params: dict[str, Any] | None = None, timeout: int = 25, retries: int = 5) -> dict:
    """Makes a JSON request with simple retry logic."""
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)

                if status_code == 429:
                    retry_after_seconds: int | None = None
                    retry_after_raw = None if response is None else response.headers.get("Retry-After")

                    if retry_after_raw:
                        try:
                            retry_after_seconds = int(retry_after_raw)
                        except ValueError:
                            retry_after_seconds = None

                    wait_seconds = retry_after_seconds or max(6 * attempt, 8)
                    print(
                        "[RATE LIMIT] 429 on "
                        f"{url}. Retrying in {wait_seconds}s "
                        f"({attempt}/{retries})"
                    )
                    time.sleep(wait_seconds)
                else:
                    time.sleep(0.8 * attempt)
        except ValueError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)

    raise RuntimeError(f"No se pudo obtener JSON de {url}: {last_exc}")


def request_text(url: str, params: dict[str, Any] | None = None, timeout: int = 25, retries: int = 3) -> str:
    """Makes a text/HTML request with simple retry logic."""
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)

    raise RuntimeError(f"No se pudo obtener texto de {url}: {last_exc}")


# ---------------------------------------------------------------------------
# Steam API access functions
# ---------------------------------------------------------------------------

def get_games_by_concurrent_players(limit: int = 100) -> list[dict]:
    """Returns the official Steam concurrent players ranking (public top list)."""
    url = f"{BASE_API}/ISteamChartsService/GetGamesByConcurrentPlayers/v1/"
    params: dict[str, str] = {}

    if STEAM_API_KEY and STEAM_API_KEY != "TU_API_KEY_AQUI":
        params["key"] = STEAM_API_KEY

    payload = request_json(url, params=params, timeout=20)
    ranks = payload.get("response", {}).get("ranks", [])
    return ranks[:limit] if limit > 0 else ranks


def extract_appids_from_search_html(results_html: str) -> list[int]:
    """Extracts appids from Steam Store search results HTML."""
    appids: list[int] = []

    # Supports both data-ds-appid="123" and data-ds-appid="[123,456]"
    for raw_value in re.findall(r'data-ds-appid="([^"]+)"', results_html):
        value = raw_value.strip()

        if value.startswith("[") and value.endswith("]"):
            for piece in value.strip("[]").split(","):
                token = piece.strip()
                if token.isdigit():
                    appids.append(int(token))
            continue

        if value.isdigit():
            appids.append(int(value))

    return appids


def collect_store_top_reviewed_appids(
    pages: int,
    page_size: int,
    delay: float,
    language: str,
) -> tuple[list[int], int]:
    """Collects appids by paginating the Steam Store sorted by reviews."""
    seen: set[int] = set()
    appids: list[int] = []
    total_count = 0

    for page_idx in range(pages):
        start = page_idx * page_size
        params = {
            "query": "",
            "start": start,
            "count": page_size,
            "dynamic_data": "",
            "sort_by": "Reviews_DESC",
            "supportedlang": language,
            "infinite": 1,
        }

        payload = request_json(STORE_SEARCH, params=params, timeout=30)
        if not total_count:
            total_count = int(payload.get("total_count") or 0)

        results_html = payload.get("results_html", "")
        page_ids = extract_appids_from_search_html(results_html)

        for appid in page_ids:
            if appid not in seen:
                seen.add(appid)
                appids.append(appid)

        print(
            f"[Store {page_idx + 1:>2}/{pages}] "
            f"start={start:>4} -> {len(page_ids):>3} ids (acumulado={len(appids)})"
        )

        if not page_ids:
            break

        time.sleep(delay)

    return appids, total_count


def collect_steamcharts_top_appids(pages: int, delay: float) -> list[int]:
    """Collects appids from paginated SteamCharts (/top, /top/p.2, ...)."""
    seen: set[int] = set()
    appids: list[int] = []

    for page in range(1, pages + 1):
        page_url = STEAMCHARTS_TOP if page == 1 else f"{STEAMCHARTS_TOP}/p.{page}"
        html = request_text(page_url, timeout=30)
        page_ids = [int(x) for x in re.findall(r"/app/(\d+)", html)]

        for appid in page_ids:
            if appid not in seen:
                seen.add(appid)
                appids.append(appid)

        print(
            f"[SteamCharts {page:>2}/{pages}] "
            f"ids={len(page_ids):>3} (acumulado={len(appids)})"
        )

        if not page_ids:
            break

        time.sleep(delay)

    return appids


def get_app_details(appid: int) -> dict:
    """Returns store metadata for a given appid."""
    url = f"{BASE_STORE}/api/appdetails"
    params = {"appids": appid, "l": "spanish"}

    payload = request_json(url, params=params, timeout=20).get(str(appid), {})
    return payload.get("data", {}) if payload.get("success") else {}


def get_app_reviews(appid: int, num_per_page: int = 5) -> dict:
    """Returns the review summary and a short sample of recent reviews."""
    url = f"{BASE_STORE}/appreviews/{appid}"
    params = {
        "json": 1,
        "num_per_page": num_per_page,
        "language": "all",
        "review_type": "all",
        "purchase_type": "all",
        "filter": "recent",
    }

    return request_json(url, params=params, timeout=20)


def get_current_players(appid: int) -> int | None:
    """Returns the current player count for an appid via ISteamUserStats."""
    url = f"{BASE_API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
    params: dict[str, int | str] = {"appid": appid}

    if STEAM_API_KEY and STEAM_API_KEY != "TU_API_KEY_AQUI":
        params["key"] = STEAM_API_KEY

    payload = request_json(url, params=params, timeout=20)
    response = payload.get("response", {})

    if int(response.get("result") or 0) != 1:
        return None

    player_count = response.get("player_count")
    return to_int(player_count)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def normalize_text(text: str | None) -> str:
    """Normalizes text for accent- and case-insensitive comparison."""
    if not text:
        return ""

    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()


def to_int(value: Any) -> int | None:
    """Safely converts a value to int, returning None on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_target_review_label(score_desc: str | None, allow_very_positive: bool) -> bool:
    """Checks whether the review label meets the quality filter."""
    value = normalize_text(score_desc)

    if value in {"extremadamente positivas", "overwhelmingly positive"}:
        return True

    if allow_very_positive and value in {"muy positivas", "very positive"}:
        return True

    return False


def safe_filename(text: str) -> str:
    """Converts a string into a safe filesystem filename."""
    cleaned = "".join(c if c.isalnum() or c in " _-" else "" for c in text).strip()
    return cleaned.replace(" ", "_") or "sin_nombre"


def save_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def save_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Saves records as JSON Lines (one JSON object per line)."""
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def build_entry(
    appid: int,
    details: dict,
    reviews_payload: dict,
    sources: set[str],
    chart_info: dict,
    current_players: int | None,
    cfg: dict[str, Any],
) -> dict:
    """Merges data from hybrid sources into a single unified output record."""
    summary = reviews_payload.get("query_summary", {})
    metacritic = details.get("metacritic") or {}

    def join_values(values: list[str] | None) -> str:
        if not values:
            return ""
        return " | ".join(v for v in values if v)

    return {
        "appid": appid,
        "nombre": details.get("name", f"App {appid}"),
        "jugadores_actuales_api": current_players,
        "desarrolladores": join_values(details.get("developers", [])),
        "editores": join_values(details.get("publishers", [])),
        "generos": join_values([g.get("description") for g in details.get("genres", [])]),
        "fecha_lanzamiento": details.get("release_date", {}).get("date"),
        "precio": details.get("price_overview", {}).get("final_formatted"),
        "metacritic_score": metacritic.get("score"),
        "metacritic_url": metacritic.get("url"),
        "descripcion_corta": details.get("short_description", ""),
        "total_resenas": summary.get("total_reviews"),
        "resenas_positivas": summary.get("total_positive"),
        "resenas_negativas": summary.get("total_negative"),
        "puntuacion": summary.get("review_score"),
        "descripcion_puntuacion": summary.get("review_score_desc"),
    }


# ---------------------------------------------------------------------------
# S3 upload helpers
# ---------------------------------------------------------------------------

def _normalize_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    return prefix.strip().strip("/")


def _build_s3_key(prefix: str, run_name: str, file_name: str) -> str:
    if prefix:
        return f"{prefix}/{run_name}/{file_name}"
    return f"{run_name}/{file_name}"


def upload_json_run_to_s3(
    run_folder: Path,
    bucket_name: str,
    prefix: str = "",
    region_name: str | None = None,
    include_individual_files: bool = True,
) -> dict[str, Any]:
    """Uploads the JSON files from a run folder to S3 and returns a summary."""
    if boto3 is None:
        raise RuntimeError("boto3 no esta instalado. Ejecuta: pip install boto3")

    if not run_folder.exists() or not run_folder.is_dir():
        raise FileNotFoundError(f"No existe la carpeta de corrida: {run_folder}")

    bucket = bucket_name.strip()
    if not bucket:
        raise ValueError("bucket_name no puede estar vacio")

    normalized_prefix = _normalize_prefix(prefix)
    s3_client = boto3.client("s3", region_name=region_name) if region_name else boto3.client("s3")

    summary_files = [
        run_folder / "infravalorados_steam.jsonl",
    ]
    files_to_upload: list[Path] = [f for f in summary_files if f.exists()]

    if include_individual_files:
        for jsonl_file in sorted(run_folder.glob("*.jsonl")):
            if jsonl_file not in files_to_upload:
                files_to_upload.append(jsonl_file)

    uploaded: list[str] = []
    failed: list[dict[str, str]] = []

    for local_path in files_to_upload:
        s3_key = _build_s3_key(normalized_prefix, run_folder.name, local_path.name)
        try:
            s3_client.upload_file(str(local_path), bucket, s3_key)
            uploaded.append(f"s3://{bucket}/{s3_key}")
            print(f"[S3] Subido: {local_path.name} -> s3://{bucket}/{s3_key}")
        except (NoCredentialsError, ClientError, BotoCoreError, FileNotFoundError) as exc:
            failed.append({"file": str(local_path), "error": str(exc)})
            print(f"[S3] Error al subir {local_path.name}: {exc}")

    return {
        "bucket": bucket,
        "prefix": normalized_prefix,
        "run_folder": str(run_folder),
        "include_individual_files": include_individual_files,
        "uploaded_count": len(uploaded),
        "failed_count": len(failed),
        "uploaded": uploaded,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Main script
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = get_runtime_config()
    print(f"[INFO] Perfil activo: {ACTIVE_PROFILE}")

    if not STEAM_API_KEY or STEAM_API_KEY == "1A6A9917751AA6584621D8B62432F2C0":
        print("[AVISO] No se encontro una API key valida en STEAM_API_KEY.")
        print("        El script puede funcionar igual con endpoints publicos.\n")

    run_folder = OUTPUT_ROOT / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_folder.mkdir(parents=True, exist_ok=True)

    print("Recolectando candidatos desde Steam Store (paginas por reseñas)...")
    store_appids, store_total_count = collect_store_top_reviewed_appids(
        pages=cfg["store_pages"],
        page_size=cfg["store_page_size"],
        delay=cfg["delay"],
        language=cfg["store_language"],
    )
    print(f"  -> Store total_count reportado: {store_total_count}")
    print(f"  -> Candidatos unicos desde Store: {len(store_appids)}\n")

    print("Recolectando candidatos desde SteamCharts...")
    steamcharts_appids = collect_steamcharts_top_appids(
        pages=cfg["steamcharts_pages"],
        delay=cfg["delay"],
    )
    print(f"  -> Candidatos unicos desde SteamCharts: {len(steamcharts_appids)}\n")

    print("Obteniendo top oficial de concurrencia (Steam API) para enriquecer datos...")
    chart_rows: list[dict] = []
    try:
        chart_rows = get_games_by_concurrent_players(100)
        print(f"  -> Registros de top concurrencia: {len(chart_rows)}\n")
    except Exception as e:
        print(f"  -> No se pudo leer top concurrencia ({e})\n")

    chart_map: dict[int, dict] = {}
    for row in chart_rows:
        appid = to_int(row.get("appid"))
        if appid is not None:
            chart_map[appid] = row

    source_map: dict[int, set[str]] = {}
    ordered_candidates: list[int] = []

    def add_candidates(appids: list[int], source_name: str) -> None:
        for appid in appids:
            if appid not in source_map:
                source_map[appid] = set()
                ordered_candidates.append(appid)
            source_map[appid].add(source_name)

    add_candidates(store_appids, "steam_store_reviews")
    add_candidates(steamcharts_appids, "steamcharts_top")
    add_candidates(list(chart_map.keys()), "steam_api_top100")

    if cfg["max_candidates"] > 0:
        ordered_candidates = ordered_candidates[: cfg["max_candidates"]]

    print(f"Candidatos unicos combinados a evaluar: {len(ordered_candidates)}\n")

    resultados: list[dict] = []
    stats = {
        "evaluados": 0,
        "error_detalles": 0,
        "error_resenas": 0,
        "descartado_no_juego": 0,
        "descartado_label": 0,
        "descartado_puntaje": 0,
        "descartado_cantidad_resenas": 0,
        "descartado_jugadores_actuales": 0,
    }

    print(
        "Aplicando filtros: "
        f"label={'Extremadamente+' if cfg['allow_very_positive'] else 'Extremadamente'}, "
        f"score>={cfg['min_review_score']}, "
        f"resenas entre {cfg['min_total_reviews']} y {cfg['max_total_reviews']}, "
        f"max evaluados por ejecucion={cfg['max_evaluations']}.\n"
    )

    for idx, appid in enumerate(ordered_candidates, start=1):
        if len(resultados) >= cfg["target"]:
            break

        if stats["evaluados"] >= cfg["max_evaluations"]:
            print(
                f"\n[INFO] Limite alcanzado: {cfg['max_evaluations']} juegos evaluados en esta ejecucion."
            )
            break

        stats["evaluados"] += 1
        chart_info = chart_map.get(appid, {})

        print(
            f"[{idx:>4}/{len(ordered_candidates)}] appid {appid}",
            end="  ",
            flush=True,
        )

        try:
            details = get_app_details(appid)
            if details.get("type") != "game":
                stats["descartado_no_juego"] += 1
                print("omitido (no es juego)")
                time.sleep(cfg["delay"])
                continue

            metacritic_score = to_int((details.get("metacritic") or {}).get("score"))
            meta_info = "sin metacritic" if metacritic_score is None else f"metacritic={metacritic_score}"
            print(f"detalles ok ({meta_info})", end="  ", flush=True)
        except Exception as e:
            stats["error_detalles"] += 1
            print(f"detalles error ({e})")
            time.sleep(cfg["delay"])
            continue

        time.sleep(cfg["delay"])

        try:
            reviews_payload = get_app_reviews(appid, num_per_page=cfg["sample_reviews"])
            summary = reviews_payload.get("query_summary", {})
            score_desc = summary.get("review_score_desc", "")
            review_score = to_int(summary.get("review_score"))
            total_reviews = to_int(summary.get("total_reviews")) or 0

            if not is_target_review_label(score_desc, allow_very_positive=cfg["allow_very_positive"]):
                stats["descartado_label"] += 1
                print(f"omitido (label: {score_desc})")
                time.sleep(cfg["delay"])
                continue

            if review_score is None or review_score < cfg["min_review_score"]:
                stats["descartado_puntaje"] += 1
                print(f"omitido (score={review_score})")
                time.sleep(cfg["delay"])
                continue

            if total_reviews < cfg["min_total_reviews"] or total_reviews > cfg["max_total_reviews"]:
                stats["descartado_cantidad_resenas"] += 1
                print(f"omitido (resenas totales: {total_reviews})")
                time.sleep(cfg["delay"])
                continue

            current_players: int | None = None
            if not cfg["skip_current_players"]:
                try:
                    current_players = get_current_players(appid)
                except Exception:
                    current_players = None

                time.sleep(cfg["delay"])

            max_players = cfg["max_current_players"]
            if max_players is not None and current_players is not None and current_players > max_players:
                stats["descartado_jugadores_actuales"] += 1
                print(f"omitido (jugadores actuales={current_players})")
                continue

            entry = build_entry(
                appid=appid,
                details=details,
                reviews_payload=reviews_payload,
                sources=source_map.get(appid, set()),
                chart_info=chart_info,
                current_players=current_players,
                cfg=cfg,
            )
            resultados.append(entry)

            print(
                "guardado "
                f"(label={score_desc}, score={review_score}, total={total_reviews}, "
                f"players={current_players})"
            )
        except Exception as e:
            stats["error_resenas"] += 1
            print(f"resenas error ({e})")

        time.sleep(cfg["delay"])

    combined_path = run_folder / "infravalorados_steam.jsonl"
    save_jsonl(combined_path, resultados)

    meta_folder = run_folder / "_meta"
    meta_folder.mkdir(parents=True, exist_ok=True)
    metadata_path = meta_folder / "metadata_busqueda.json"
    metadata = {
        "fecha_ejecucion": datetime.now().isoformat(timespec="seconds"),
        "perfil_activo": ACTIVE_PROFILE,
        "parametros": cfg,
        "s3": {
            "enabled": S3_UPLOAD_ENABLED,
            "bucket": S3_BUCKET_NAME,
            "prefix": S3_PREFIX,
            "region": S3_REGION,
            "include_individual_files": S3_INCLUDE_INDIVIDUAL_FILES,
        },
        "fuentes": {
            "store_total_count_reportado": store_total_count,
            "store_ids_unicos": len(store_appids),
            "steamcharts_ids_unicos": len(steamcharts_appids),
            "steam_api_top100_ids": len(chart_map),
            "candidatos_unicos_combinados": len(ordered_candidates),
        },
        "estadisticas_evaluacion": stats,
        "total_resultados": len(resultados),
        "carpeta_salida": str(run_folder),
        "nota": (
            "Enfoque hibrido: candidatos desde Store paginado y SteamCharts, "
            "validados con appdetails/appreviews de Steam API/Store."
        ),
    }
    save_json(metadata_path, metadata)

    s3_upload_result: dict[str, Any] | None = None
    if S3_UPLOAD_ENABLED:
        try:
            print("[S3] Iniciando subida de resultados a AWS S3...")
            s3_upload_result = upload_json_run_to_s3(
                run_folder=run_folder,
                bucket_name=S3_BUCKET_NAME,
                prefix=S3_PREFIX,
                region_name=S3_REGION,
                include_individual_files=S3_INCLUDE_INDIVIDUAL_FILES,
            )
            print(
                "[S3] Subida finalizada: "
                f"ok={s3_upload_result['uploaded_count']}, "
                f"fallidos={s3_upload_result['failed_count']}"
            )
        except Exception as e:
            print(f"[S3] Error general de subida: {e}")

    print(f"\n{'-' * 60}")
    print("Proceso completado")
    print(f"  • Carpeta de salida      : {run_folder}")
    print(f"  • Archivo combinado      : {combined_path}")
    print(f"  • Archivo de metadata    : {metadata_path}")
    print(f"  • Juegos infravalorados  : {len(resultados)}")
    if s3_upload_result is not None:
        print(f"  • Archivos subidos a S3  : {s3_upload_result['uploaded_count']}")
        print(f"  • Errores de subida S3   : {s3_upload_result['failed_count']}")


if __name__ == "__main__":
    main()
