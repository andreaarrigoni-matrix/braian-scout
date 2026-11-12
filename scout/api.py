"""Contratto di Scout verso System One: cinque operazioni più un controllo.

    from scout import api
    config = api.load_config("config.yaml")
    snap   = api.load_snapshot(config)                       # ultima fotografia valida, in RAM
    esito  = api.find_candidates(snap, {"capability": "coding", "data_class": "public"})
    stato  = api.resource_status(snap, "ollama-shadow/gemma4-claude")
    fresco = api.recheck(config, "ollama-shadow/gemma4-claude")   # controllo leggero, cache breve
    quadro = api.summary(snap)                               # per il Loft
    via    = api.check_execution_allowed(snap, chiave, data_class="private_memory")

Scout descrive risorse. Identità degli agenti, conversazioni e stato delle
.Jobs stanno nei loro componenti: qui non entrano.
"""
from __future__ import annotations

from . import health as _health
from .config import load_config  # noqa: F401
from .query import (NoRegistry, Snapshot, check_execution_allowed,  # noqa: F401
                    find_candidates, load_snapshot, resource_status, summary)


def recheck(config: dict, key: str, client_factory=None, secret=None, now=None) -> dict:
    """Ricontrolla il servizio di una risorsa e ritorna il suo stato aggiornato.

    Da chiamare subito prima dell'esecuzione. Tempi stretti (vedi `health` in
    config.yaml) e cache breve: due chiamate ravvicinate non fanno due controlli.
    """
    esito = _health.recheck(config, key, client_factory, secret, now)
    try:
        snap = load_snapshot(config, now)
    except NoRegistry:
        return {"service": esito, "resource": None}
    return {"service": esito,
            "resource": resource_status(snap, key) if key in snap.registry["endpoints"] else None}
