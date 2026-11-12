"""Il registro su disco: validazione, lettura con recupero, pubblicazione atomica."""
from __future__ import annotations

import os

from . import SCHEMA_VERSION, __version__, schema
from .util import read_json, write_json_atomic

_SCHEMA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "schema", "models_registry.schema.json")
_schema_cache = None


class RegistryInvalid(Exception):
    def __init__(self, errors: list):
        super().__init__("registro non valido: " + "; ".join(errors[:5]))
        self.errors = errors


def load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = read_json(_SCHEMA_FILE)
    return _schema_cache


def empty(now: str, categories: list, synthetic: bool = False) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "generator": {"name": "scout", "version": __version__},
        "synthetic": synthetic,
        "categories": list(categories),
        "sources": {},
        "models": {},
        "endpoints": {},
        "views": {"free_safe": {"policy": {}, "endpoints": []}},
    }


def validate(reg) -> list:
    """Errori di struttura (schema) e di coerenza interna. Lista vuota = valido."""
    errors = schema.check(reg, load_schema())
    if errors:
        return errors
    cats = set(reg["categories"])
    for key, ep in reg["endpoints"].items():
        dove = f"endpoints[{key}]"
        if ep["endpoint_key"] != key:
            errors.append(f"{dove}: endpoint_key diverso dalla chiave")
        if ep["model_key"] not in reg["models"]:
            errors.append(f"{dove}: modello '{ep['model_key']}' assente")
        if ep["source"] not in reg["sources"]:
            errors.append(f"{dove}: fonte '{ep['source']}' assente")
        if ep["pricing"]["known"] != bool(ep["pricing"]["items"]):
            errors.append(f"{dove}: pricing.known incoerente con pricing.items")
        for livello in ("declared", "verified", "heuristic"):
            for c in ep["capabilities"][livello]:
                if c["category"] not in cats:
                    errors.append(f"{dove}: categoria sconosciuta '{c['category']}'")
    for key, m in reg["models"].items():
        if m["model_key"] != key:
            errors.append(f"models[{key}]: model_key diverso dalla chiave")
    for key in reg["views"]["free_safe"]["endpoints"]:
        if key not in reg["endpoints"]:
            errors.append(f"views.free_safe: endpoint '{key}' assente")
    return errors[:40]


def load_valid(path: str, backup: str | None = None):
    """Ritorna (registro, origine). Se il file corrente è rotto prova la copia.

    origine: "current" | "backup" | "none"
    """
    for file, origine in ((path, "current"), (backup or path + ".bak", "backup")):
        try:
            reg = read_json(file)
        except (OSError, ValueError):
            continue
        if not validate(reg):
            return reg, origine
    return None, "none"


def publish(path: str, reg: dict, backup: str | None = None) -> None:
    """Valida e pubblica. Se non è valido non tocca niente e solleva RegistryInvalid.

    Prima di sostituire il file corrente ne conserva una copia valida in .bak.
    """
    errors = validate(reg)
    if errors:
        raise RegistryInvalid(errors)
    backup = backup or path + ".bak"
    try:
        corrente = read_json(path)
        if not validate(corrente):
            write_json_atomic(backup, corrente)
    except (OSError, ValueError):
        pass  # nessun file corrente valido da conservare
    write_json_atomic(path, reg)
