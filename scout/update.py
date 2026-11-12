"""Aggiornamento del registro: ogni fonte per conto suo, poi pubblicazione atomica.

Se una fonte fallisce: i suoi ultimi dati validi restano, il timestamp
dell'ultima raccolta riuscita non cambia, il tentativo fallito viene
registrato a parte e le altre fonti proseguono.
"""
from __future__ import annotations

import copy
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import adapters, classify, health, registry
from .config import env_secret, paths
from .net import Client, NetError
from .util import file_lock, iso, read_json, utcnow, write_json_atomic

log = logging.getLogger("scout")


def default_client_factory(config: dict, deadline: float):
    lim, h = config["limits"], config["health"]
    cache = paths(config)["cache"]

    def factory(sid: str, kind: str) -> Client:
        if kind == "service":
            # un servizio privato che non risponde non deve consumare il tempo dei cataloghi
            return Client(connect_timeout=h["connect_timeout"], request_timeout=h["request_timeout"],
                          max_bytes=h["max_response_bytes"], retries=0,
                          deadline=min(deadline, time.monotonic() + h["deep_budget_seconds"]),
                          max_requests=lim["max_requests_per_source"])
        scfg = config["sources"][sid]
        return Client(connect_timeout=lim["connect_timeout"], request_timeout=lim["request_timeout"],
                      max_bytes=scfg.get("max_response_bytes", lim["max_response_bytes"]),
                      retries=lim["retries"], backoff=lim["retry_backoff_seconds"],
                      min_interval=lim["min_interval_seconds"], deadline=deadline,
                      max_requests=lim["max_requests_per_source"], cache_dir=cache)
    return factory


def _run_source(config, sid, client_factory, secret, now_iso, deadline, offline):
    """Ritorna (esito, errore, Result|None). Non solleva mai."""
    scfg = config["sources"][sid]
    kind = adapters.KINDS[scfg["adapter"]]
    if offline:
        return "skipped", "modalità offline", None
    if time.monotonic() >= deadline:
        return "skipped", "tempo totale dell'esecuzione esaurito", None
    try:
        ctx = adapters.Ctx(sid, scfg, config, client_factory(sid, kind), now_iso, secret, True)
        res = adapters.get(scfg["adapter"]).collect(ctx)
    except adapters.SourceSkipped as e:
        return "skipped", str(e), None
    except (adapters.SourceError, NetError) as e:
        return "error", str(e), None
    except Exception as e:
        log.exception("fonte %s: errore interno", sid)
        return "error", f"errore interno ({type(e).__name__})", None
    if res.endpoints is None:
        return "error", res.error or "inventario non ottenuto", res
    return "ok", None, res


def _add(reg: dict, sid: str, models: dict, endpoints: dict) -> None:
    for k, m in models.items():
        cur = reg["models"].get(k)
        if cur is None:
            reg["models"][k] = m
            continue
        if sid not in cur["provenance"]["sources"]:
            cur["provenance"]["sources"].append(sid)
        for campo in ("vendor", "version", "description_untrusted"):
            if cur[campo] is None:
                cur[campo] = m[campo]
    reg["endpoints"].update(endpoints)


def _stale_after(config: dict, kind: str) -> int:
    f = config["freshness"]
    ore = f["service_stale_after_hours"] if kind == "service" else f["catalog_stale_after_hours"]
    return int(ore * 3600)


def run_update(config: dict, only=None, offline: bool = False, client_factory=None,
               secret=None, now=None, synthetic: bool = False) -> dict:
    """Esegue un aggiornamento completo. Solleva LockBusy se ne gira già un altro."""
    p = paths(config)
    now = now or utcnow()
    now_iso = iso(now)
    secret = secret or env_secret
    t0 = time.monotonic()
    report = {"started_at": now_iso, "offline": offline, "published": False,
              "sources": {}, "errors": [], "notes": []}

    with file_lock(p["lock"], stale_seconds=config["limits"]["lock_stale_seconds"]):
        prev, origine = registry.load_valid(p["registry"], p["backup"])
        if origine == "backup":
            report["notes"].append("registro corrente illeggibile: ripartito dalla copia precedente")
        prev = prev or registry.empty(now_iso, config["categories"])
        deadline = t0 + config["limits"]["total_seconds"]
        factory = client_factory or default_client_factory(config, deadline)

        attive = [sid for sid, s in config["sources"].items() if s.get("enabled", True)]
        da_fare = [sid for sid in attive if not only or sid in only]
        with ThreadPoolExecutor(max_workers=max(1, config["limits"]["concurrency"])) as ex:
            esiti = dict(zip(da_fare, ex.map(
                lambda s: _run_source(config, s, factory, secret, now_iso, deadline, offline), da_fare)))

        reg = registry.empty(now_iso, config["categories"], synthetic or prev.get("synthetic", False))
        salute = {}
        for sid in attive:
            scfg = config["sources"][sid]
            kind = adapters.KINDS[scfg["adapter"]]
            prima = prev["sources"].get(sid) or {
                "adapter": scfg["adapter"], "kind": kind, "last_attempt_at": None,
                "last_attempt_outcome": "never", "last_error": None, "last_success_at": None,
                "stale_after_seconds": _stale_after(config, kind),
                "counts": {"models": 0, "endpoints": 0}, "notes": []}
            vecchi = {k: copy.deepcopy(e) for k, e in prev["endpoints"].items() if e["source"] == sid}
            stato = dict(prima, adapter=scfg["adapter"], kind=kind,
                         stale_after_seconds=_stale_after(config, kind))
            if sid in esiti:
                esito, errore, res = esiti[sid]
                stato.update(last_attempt_at=now_iso, last_attempt_outcome=esito, last_error=errore)
                if res is not None and res.health is not None:
                    salute[sid] = res.health
                if esito == "ok":
                    stato.update(last_success_at=now_iso, notes=list(res.notes))
                    _add(reg, sid, res.models, res.endpoints)
                    nuovi = res.endpoints
                else:
                    nuovi = None
                    if errore and esito == "error":
                        report["errors"].append(f"{sid}: {errore}")
                report["sources"][sid] = {"outcome": esito, "error": errore}
            else:
                nuovi = None
                report["sources"][sid] = {"outcome": "not_run", "error": None}
            if nuovi is None:
                if vecchi:
                    # restano gli ultimi dati validi, con la loro data originale
                    modelli = {e["model_key"]: copy.deepcopy(prev["models"][e["model_key"]])
                               for e in vecchi.values()}
                    _add(reg, sid, modelli, vecchi)
                    report["sources"][sid]["kept_previous"] = len(vecchi)
                elif kind == "service":
                    # primo avvio senza risposta: almeno ciò che dichiara la configurazione
                    ctx = adapters.Ctx(sid, scfg, config, None, now_iso, secret, False)
                    dich = adapters.get(scfg["adapter"]).declared(ctx)
                    _add(reg, sid, dich.models, dich.endpoints or {})
                    report["sources"][sid]["declared_only"] = len(dich.endpoints or {})
            miei = [e for e in reg["endpoints"].values() if e["source"] == sid]
            stato["counts"] = {"models": len({e["model_key"] for e in miei}), "endpoints": len(miei)}
            reg["sources"][sid] = stato
            report["sources"][sid]["endpoints"] = len(miei)

        classify.finalize(reg, config, report["notes"])
        try:
            registry.publish(p["registry"], reg, p["backup"])
            report["published"] = True
        except registry.RegistryInvalid as e:
            # il registro valido precedente resta dov'è
            report["errors"].append("registro NON pubblicato: " + "; ".join(e.errors[:3]))
            log.error("registro non pubblicato: %s", "; ".join(e.errors[:10]))

        if salute:
            health.store_entries(config, {sid: health.make_entry(config, sid, h, now)
                                          for sid, h in salute.items()}, now)

        report["finished_at"] = iso(utcnow()) if not synthetic else now_iso
        report["duration_seconds"] = round(time.monotonic() - t0, 2)
        try:
            stato_file = read_json(p["state"])
        except (OSError, ValueError):
            stato_file = {}
        stato_file["last_run"] = {k: report[k] for k in
                                  ("started_at", "finished_at", "duration_seconds", "offline",
                                   "published", "sources", "errors")}
        if report["published"]:
            stato_file["last_published_at"] = now_iso
        write_json_atomic(p["state"], stato_file)
    log.info("update: pubblicato=%s fonti=%s errori=%d", report["published"],
             {s: v["outcome"] for s, v in report["sources"].items()}, len(report["errors"]))
    return report
