"""Adattatori: uno per fonte. Ognuno traduce una fonte nei record del registro.

Contratto comune:
    collect(ctx) -> Result      raccoglie (solo GET, nessuna generazione)
    declared(ctx) -> Result     (solo servizi) cosa dichiara la configurazione,
                                senza rete: serve al primo avvio se il servizio
                                non risponde.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field

from ..model import price
from ..net import NetError

# adattatore -> tipo di fonte
KINDS = {
    "ollama": "service",
    "comfyui": "service",
    "generic": "service",
    "openrouter": "catalog",
    "fal": "catalog",
    "replicate": "catalog",
}


class SourceError(Exception):
    """La fonte ha risposto in modo inutilizzabile."""


class SourceSkipped(Exception):
    """La fonte non è stata interrogata (per esempio mancano le credenziali)."""


@dataclass
class Ctx:
    source_id: str
    cfg: dict          # configurazione della fonte
    config: dict       # configurazione intera
    client: object     # net.Client o un sostituto per i test
    now: str           # timestamp ISO dell'esecuzione
    secret: object     # funzione nome_variabile -> valore | None
    deep: bool = True  # True: verifica anche le dipendenze dei workflow


@dataclass
class Result:
    models: dict = field(default_factory=dict)
    endpoints: dict | None = None      # None = inventario non ottenuto
    notes: list = field(default_factory=list)
    health: dict | None = None         # solo per i servizi
    error: str | None = None


def get(name: str):
    return importlib.import_module(f"{__name__}.{name}")


# ---------------------------------------------------------------- attrezzi comuni
def net_state(e: NetError) -> str:
    """Traduce un errore di rete nello stato del servizio.

    timeout non vuol dire "macchina spenta": vuol dire solo che non è arrivata
    risposta entro il limite.
    """
    if e.kind in ("timeout", "budget"):
        return "timeout"
    if e.kind == "refused":
        return "refused"
    return "error"


def service_url(ctx: Ctx) -> str:
    nodo = ctx.config["nodes"].get(ctx.cfg.get("node") or "", {})
    return str(ctx.cfg.get("url", "")).format(address=nodo.get("address", "")).rstrip("/")


def service_location(ctx: Ctx) -> dict:
    """Dove gira il calcolo e in quale dominio di fiducia.

    `hosting` distingue l'hardware posseduto da un PC remoto in affitto: una
    macchina privata nel cloud non è la stessa cosa di una macchina in casa.
    """
    nome = ctx.cfg.get("node")
    nodo = ctx.config["nodes"].get(nome or "", {})
    return {
        "compute": "external" if ctx.cfg.get("external") else "private",
        "node": nome,
        "trust_domain": ctx.cfg.get("trust_domain") or nodo.get("trust_domain", "non_definito"),
        "hosting": nodo.get("hosting", "unknown"),
    }


def private_costs(ctx: Ctx) -> dict:
    """Costo API marginale (anche nullo) separato dai costi dell'infrastruttura."""
    nodo = ctx.config["nodes"].get(ctx.cfg.get("node") or "", {})
    marginale = ctx.cfg.get("api_marginal_cost")
    api = None
    if isinstance(marginale, dict) and isinstance(marginale.get("amount"), (int, float)):
        api = price(marginale["amount"], marginale.get("currency", "USD"),
                    marginale.get("unit", "request"), "manual", "config.yaml", ctx.now)
    return {"api_marginal": api, "infrastructure": list(nodo.get("infrastructure_costs") or [])}


def declared_caps(categorie, fonte: str) -> list:
    return [{"category": c, "source": fonte} for c in (categorie or [])]


def mapped_caps(ctx: Ctx, provider: str, categoria_provider) -> list:
    """Capacità dichiarate dal catalogo, tradotte con la mappa in configurazione."""
    mappa = ctx.config["classification"]["provider_category_map"].get(provider, {})
    return declared_caps(mappa.get(categoria_provider, []), f"{provider}:category={categoria_provider}")
