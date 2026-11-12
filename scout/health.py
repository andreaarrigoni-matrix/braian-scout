"""Disponibilità dei servizi privati: controllo leggero, su richiesta.

Frequenza separata da quella dei cataloghi: lo stato sta in un file suo
(health_state.json), così un controllo non riscrive il registro.

Tempi del controllo leggero di un servizio (configurabili in `health`):
- connect_timeout       1 s   apertura della connessione
- request_timeout       2 s   limite complessivo della singola richiesta
- check_budget_seconds  2 s   limite complessivo dell'intero controllo
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from . import SCHEMA_VERSION, adapters
from .config import env_secret, paths
from .net import Client
from .util import age_seconds, file_lock, iso, read_json, utcnow, write_json_atomic


def load_health(config: dict) -> dict:
    try:
        dati = read_json(paths(config)["health"])
        if isinstance(dati, dict) and isinstance(dati.get("services"), dict):
            return dati
    except (OSError, ValueError):
        pass
    return {"schema_version": SCHEMA_VERSION, "updated_at": None, "services": {}}


def make_entry(config: dict, sid: str, h: dict | None, now, latency_ms=None) -> dict:
    scfg = config["sources"][sid]
    entry = {"source": sid, "adapter": scfg["adapter"], "node": scfg.get("node"),
             "state": "unknown", "inventory": "unknown", "version": None,
             "installed": None, "resident": None, "workflows": None, "error": None}
    entry.update(h or {})
    entry["checked_at"] = iso(now)
    entry["valid_until"] = iso(now + timedelta(seconds=config["health"]["validity_seconds"]))
    entry["latency_ms"] = latency_ms
    return entry


def store_entries(config: dict, entries: dict, now) -> dict:
    """Aggiunge gli esiti al file dello stato senza perdere quelli degli altri servizi."""
    p = paths(config)
    with file_lock(p["health_lock"], stale_seconds=60, wait_seconds=3):
        stato = load_health(config)
        for sid, e in entries.items():
            prima = stato["services"].get(sid) or {}
            if e.get("workflows") is None:
                # il controllo leggero non verifica le dipendenze: restano quelle
                # dell'ultimo controllo approfondito, con la loro data
                e["workflows"] = prima.get("workflows")
            stato["services"][sid] = e
        stato["updated_at"] = iso(now)
        write_json_atomic(p["health"], stato)
    return stato


def _client(config: dict, deep: bool) -> Client:
    h = config["health"]
    budget = h["deep_budget_seconds"] if deep else h["check_budget_seconds"]
    return Client(connect_timeout=h["connect_timeout"], request_timeout=h["request_timeout"],
                  max_bytes=h["max_response_bytes"], retries=0,
                  deadline=time.monotonic() + budget)


def check_service(config: dict, sid: str, deep: bool = False, client_factory=None,
                  secret=None, now=None) -> dict:
    """Controlla un servizio privato. Solo GET: non avvia workflow, non carica modelli."""
    now = now or utcnow()
    scfg = config["sources"][sid]
    client = client_factory(sid, "service") if client_factory else _client(config, deep)
    ctx = adapters.Ctx(sid, scfg, config, client, iso(now), secret or env_secret, deep)
    t0 = time.monotonic()
    try:
        h = adapters.get(scfg["adapter"]).collect(ctx).health
    except Exception as e:  # un adattatore rotto non deve fermare gli altri controlli
        h = {"state": "error", "error": f"errore interno ({type(e).__name__})"}
    return make_entry(config, sid, h, now, int((time.monotonic() - t0) * 1000))


def service_sources(config: dict, only=None) -> list:
    return [sid for sid, s in config["sources"].items()
            if adapters.KINDS[s["adapter"]] == "service" and s.get("enabled", True)
            and (not only or sid in only)]


def run_health(config: dict, only=None, deep: bool = False, client_factory=None,
               secret=None, now=None) -> dict:
    now = now or utcnow()
    sids = service_sources(config, only)
    with ThreadPoolExecutor(max_workers=max(1, config["limits"]["concurrency"])) as ex:
        esiti = list(ex.map(lambda s: check_service(config, s, deep, client_factory, secret, now), sids))
    entries = dict(zip(sids, esiti))
    if entries:
        store_entries(config, entries, now)
    return entries


def source_of(config: dict, key: str):
    """Fonte di una chiave: la chiave stessa, o la parte prima della prima '/'."""
    if key in config["sources"]:
        return key
    sid = key.split("/", 1)[0]
    return sid if sid in config["sources"] else None


def recheck(config: dict, key: str, client_factory=None, secret=None, now=None) -> dict:
    """Ricontrollo da fare subito prima dell'esecuzione.

    Cache breve: se l'ultimo controllo è più recente di `recheck_cache_seconds`
    si riusa quello ("cached": true), altrimenti si rifà con i tempi stretti del
    controllo leggero.
    """
    now = now or utcnow()
    sid = source_of(config, key)
    if sid is None:
        return {"source": None, "state": "unknown", "error": "risorsa sconosciuta", "cached": False}
    if adapters.KINDS[config["sources"][sid]["adapter"]] != "service":
        return {"source": sid, "state": "not_checked", "cached": False,
                "error": "risorsa esterna: Scout non la interroga per verificarne l'eseguibilità"}
    prima = load_health(config)["services"].get(sid)
    eta = age_seconds((prima or {}).get("checked_at"), now)
    if prima and eta is not None and eta <= config["health"]["recheck_cache_seconds"]:
        return dict(prima, cached=True)
    entry = check_service(config, sid, False, client_factory, secret, now)
    stato = store_entries(config, {sid: entry}, now)
    return dict(stato["services"][sid], cached=False)
