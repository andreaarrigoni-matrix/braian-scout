"""Ollama: inventario dei modelli locali con le API ufficiali.

Usa solo:
    GET /api/tags     modelli installati
    GET /api/ps       modelli caricati in memoria ("residenti")
    GET /api/version  versione del servizio

Non chiama /api/generate, /api/chat, /api/pull né /api/show: la
documentazione non dice se /api/show carica il modello, quindi non lo usiamo.
Di conseguenza il contesto massimo è noto solo per i modelli residenti.
"""
from __future__ import annotations

from ..model import new_endpoint, new_model
from ..net import NetError
from . import Ctx, Result, declared_caps, net_state, private_costs, service_location, service_url


def _norm(nome: str) -> str:
    return nome[:-7] if nome.endswith(":latest") else nome


def _record(ctx: Ctx, nome: str, info: dict | None, residente: dict | None):
    """Costruisce modello ed endpoint per un modello Ollama.

    info None = modello atteso dalla configurazione ma non visto nell'inventario.
    """
    sid = ctx.source_id
    attesi = {_norm(k): v for k, v in (ctx.cfg.get("expected") or {}).items()}
    exp = attesi.get(nome) or {}
    info = info or {}
    dett = info.get("details") if isinstance(info.get("details"), dict) else {}
    digest = info.get("digest") if isinstance(info.get("digest"), str) else None
    mkey = f"local/{nome}"
    m = new_model(mkey, nome, sid, ctx.now, version=digest,
                  details={k: dett.get(k) for k in ("family", "parameter_size", "quantization_level", "format")
                           if dett.get(k) is not None})
    ep = new_endpoint(f"{sid}/{nome}", mkey, sid, "ollama", exp.get("kind", "llm"), ctx.now,
                      "service_inventory" if info else "config", service_location(ctx))
    ep["revision"] = digest[:12] if digest else None
    ep["details"] = {"service_model": info.get("name") or nome}
    for k in ("parameter_size", "quantization_level", "family", "format"):
        if dett.get(k) is not None:
            ep["details"][k] = dett[k]
    if isinstance(info.get("size"), int):
        ep["details"]["size_bytes"] = info["size"]
    # Le modalità non sono esposte da /api/tags: si conoscono solo se dichiarate.
    for campo in ("input_modalities", "output_modalities", "input_formats", "output_formats"):
        if (exp.get("io") or {}).get(campo):
            ep["io"][campo] = list(exp["io"][campo])
    if residente and isinstance(residente.get("context_length"), int):
        ep["limits"]["context_tokens"] = residente["context_length"]
    ep["capabilities"]["declared"] = declared_caps(exp.get("capabilities"), "config:expected")
    ep["private_costs"] = private_costs(ctx)
    ep["access"] = {"in_catalog": bool(info), "credentials": "not_required",
                    "quota": "unknown", "executable": "unverified"}
    return m, ep


def declared(ctx: Ctx) -> Result:
    res = Result(endpoints={})
    for nome in (ctx.cfg.get("expected") or {}):
        m, ep = _record(ctx, _norm(nome), None, None)
        res.models[m["model_key"]] = m
        res.endpoints[ep["endpoint_key"]] = ep
    return res


def collect(ctx: Ctx) -> Result:
    base = service_url(ctx)
    health = {"state": "unknown", "inventory": "unknown", "version": None,
              "installed": None, "resident": None, "workflows": None, "error": None}
    try:
        tags = ctx.client.get_json(base + "/api/tags")
    except NetError as e:
        health.update(state=net_state(e), error=str(e))
        return Result(health=health, error=str(e))
    health["state"] = "reachable"
    lista = tags.get("models") if isinstance(tags, dict) else None
    if not isinstance(lista, list):
        health.update(inventory="unavailable", error="risposta di /api/tags senza 'models'")
        return Result(health=health, error=health["error"])
    installati = {}
    for voce in lista:
        if isinstance(voce, dict) and isinstance(voce.get("name"), str):
            installati[_norm(voce["name"])] = voce
    health.update(inventory="available", installed=sorted(installati))

    residenti = {}
    try:  # "residente" solo se rilevabile: se /api/ps non risponde resta sconosciuto
        ps = ctx.client.get_json(base + "/api/ps")
        if isinstance(ps, dict) and isinstance(ps.get("models"), list):
            for voce in ps["models"]:
                if isinstance(voce, dict) and isinstance(voce.get("name"), str):
                    residenti[_norm(voce["name"])] = voce
            health["resident"] = sorted(residenti)
    except NetError:
        pass
    try:
        v = ctx.client.get_json(base + "/api/version")
        if isinstance(v, dict) and isinstance(v.get("version"), str):
            health["version"] = v["version"][:40]
    except NetError:
        pass

    res = Result(endpoints={}, health=health)
    attesi = [_norm(k) for k in (ctx.cfg.get("expected") or {})]
    for nome in sorted(set(installati) | set(attesi)):
        m, ep = _record(ctx, nome, installati.get(nome), residenti.get(nome))
        res.models[m["model_key"]] = m
        res.endpoints[ep["endpoint_key"]] = ep
    mancanti = [n for n in attesi if n not in installati]
    if mancanti:
        res.notes.append("modelli attesi ma non installati: " + ", ".join(mancanti))
    return res
