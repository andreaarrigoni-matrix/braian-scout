"""Lettura di config.yaml, valori predefiniti, percorsi e segreti.

Tutti i percorsi sono relativi alla cartella del file di configurazione, così
Scout si sposta da una macchina all'altra copiando la cartella.
"""
from __future__ import annotations

import logging
import logging.handlers
import os

from .util import deep_merge

CATEGORIE = [
    "fast_general",
    "deep_reasoning",
    "coding",
    "editorial_writing",
    "3d_modeling_pro.script_and_math",
    "3d_modeling_pro.asset_generation",
    "video_production.direction_and_ffmpeg",
    "video_production.video_synthesis",
]

DEFAULTS = {
    "paths": {"data_dir": "data"},
    "limits": {
        "total_seconds": 120,          # durata massima di un intero `update`
        "connect_timeout": 3.0,        # cataloghi: apertura della connessione
        "request_timeout": 20.0,       # cataloghi: limite complessivo per richiesta
        "max_response_bytes": 16_000_000,
        "max_requests_per_source": 40,
        "max_pages": 5,
        "concurrency": 3,
        "retries": 2,
        "retry_backoff_seconds": 1.0,
        "min_interval_seconds": 0.25,  # pausa minima fra richieste alla stessa fonte
        "log_max_bytes": 1_000_000,
        "log_backups": 3,
        "lock_stale_seconds": 900,
    },
    "health": {
        "connect_timeout": 1.0,        # servizi privati: apertura della connessione
        "request_timeout": 2.0,        # limite complessivo della singola richiesta
        "check_budget_seconds": 2.0,   # limite complessivo del controllo leggero di un servizio
        "deep_budget_seconds": 8.0,    # controllo con inventario delle dipendenze (update, --deep)
        "validity_seconds": 300,       # per quanto un esito resta valido
        "recheck_cache_seconds": 20,   # cache breve del ricontrollo prima dell'esecuzione
        "max_response_bytes": 4_000_000,
        "max_dependency_checks": 20,
    },
    "freshness": {
        "catalog_stale_after_hours": 36,
        "service_stale_after_hours": 336,
    },
    "categories": CATEGORIE,
    "nodes": {},
    "sources": {},
    "classification": {"provider_category_map": {}, "heuristic_rules": []},
    "manual": {"file": None},
    "benchmarks": {"files": []},
    "policy": {
        # dalla meno alla più delicata: se la richiesta non dice la classe, vale l'ultima
        "data_classes": ["public", "filtered_context", "private_memory"],
        "default": {
            "enabled": False,
            "allowed_data_classes": ["public"],
            "requires_filtered_context": True,
            "spend_limit": None,
            "requires_user_decision": [],
        },
        "rules": [],
        "free_safe": {"required_data_class": "public", "trust_domains": []},
    },
    "ranking": {
        "default_sort": ["availability", "private_first", "zero_api_cost", "evidence", "context_desc"],
        "default_limit": 5,
    },
}


class ConfigError(Exception):
    pass


def find_config(path: str | None = None) -> str:
    candidati = [path, os.environ.get("SCOUT_CONFIG"), "config.yaml",
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")]
    for c in candidati:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    raise ConfigError("config.yaml non trovato: usa --config o la variabile SCOUT_CONFIG")


def load_config(path: str | None = None) -> dict:
    try:
        import yaml
    except ImportError:
        raise ConfigError("manca PyYAML: pip install -r requirements.txt")
    file = find_config(path)
    with open(file, encoding="utf-8") as f:
        dati = yaml.safe_load(f) or {}
    if not isinstance(dati, dict):
        raise ConfigError("config.yaml: la radice deve essere un dizionario")
    cfg = build_config(dati, os.path.dirname(file))
    load_dotenv(os.path.join(os.path.dirname(file), ".env"))
    return cfg


def build_config(dati: dict, base_dir: str) -> dict:
    """Completa una configurazione grezza con i predefiniti e la controlla."""
    cfg = deep_merge(DEFAULTS, dati)
    cfg["_base_dir"] = os.path.abspath(base_dir)
    data_dir = os.environ.get("SCOUT_DATA_DIR") or cfg["paths"]["data_dir"]
    cfg["_data_dir"] = resolve(cfg, data_dir)
    for cat in CATEGORIE:
        if cat not in cfg["categories"]:
            raise ConfigError(f"categories: manca la categoria obbligatoria '{cat}'")
    from .adapters import KINDS
    for sid, s in cfg["sources"].items():
        if not isinstance(s, dict) or s.get("adapter") not in KINDS:
            raise ConfigError(f"sources.{sid}: 'adapter' assente o sconosciuto")
        if "/" in sid:
            raise ConfigError(f"sources.{sid}: il nome di una fonte non può contenere '/'")
        nodo = s.get("node")
        if nodo and nodo not in cfg["nodes"]:
            raise ConfigError(f"sources.{sid}: nodo '{nodo}' non definito in 'nodes'")
    classi = cfg["policy"]["data_classes"]
    for regola in [cfg["policy"]["default"]] + list(cfg["policy"]["rules"]):
        for c in regola.get("allowed_data_classes", []):
            if c not in classi:
                raise ConfigError(f"policy: classe di dati sconosciuta '{c}'")
    return cfg


def resolve(cfg: dict, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cfg["_base_dir"], path)


def paths(cfg: dict) -> dict:
    d = cfg["_data_dir"]
    reg = os.path.join(d, "models_registry.json")
    return {
        "data": d,
        "registry": reg,
        "backup": reg + ".bak",
        "health": os.path.join(d, "health_state.json"),
        "state": os.path.join(d, "scout_state.json"),
        "lock": os.path.join(d, "scout.lock"),
        "health_lock": os.path.join(d, "health.lock"),
        "cache": os.path.join(d, "cache"),
        "log": os.path.join(d, "logs", "scout.log"),
    }


def load_dotenv(file: str) -> None:
    """Carica KEY=VALORE da un file .env, senza sovrascrivere l'ambiente."""
    if not os.path.isfile(file):
        return
    with open(file, encoding="utf-8") as f:
        for riga in f:
            riga = riga.strip()
            if not riga or riga.startswith("#") or "=" not in riga:
                continue
            k, v = riga.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def env_secret(nome: str | None):
    """Legge un segreto dall'ambiente. Il valore non va mai scritto altrove."""
    if not nome:
        return None
    return os.environ.get(nome) or None


def setup_logging(cfg: dict) -> logging.Logger:
    log = logging.getLogger("scout")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
        return log
    file = paths(cfg)["log"]
    os.makedirs(os.path.dirname(file), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        file, maxBytes=cfg["limits"]["log_max_bytes"],
        backupCount=cfg["limits"]["log_backups"], encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    return log
