"""ComfyUI: stato del servizio più inventario esplicito dei workflow configurati.

Usa solo rotte GET di sola lettura:
    GET /system_stats               il servizio risponde
    GET /object_info/{node_class}   il nodo richiesto da un workflow esiste
    GET /models/{folder}            i file modello richiesti sono presenti

Non chiama mai POST /prompt (è la rotta che avvia un workflow) né le altre
rotte POST che cambiano stato (/interrupt, /free, /queue, /history, /upload).

La sola presenza di ComfyUI non attribuisce nessuna capacità: gli endpoint
sono i workflow elencati in configurazione, e basta. La forma JSON delle
risposte non è documentata ufficialmente: se una risposta non ha la forma
attesa, la dipendenza resta "unverified" invece di essere data per buona.
"""
from __future__ import annotations

from urllib.parse import quote

from ..model import new_endpoint, new_model
from ..net import NetError
from . import Ctx, Result, declared_caps, net_state, private_costs, service_location, service_url


def _records(ctx: Ctx) -> Result:
    """Un endpoint per ogni workflow configurato. Nessuna rete."""
    res = Result(endpoints={})
    sid = ctx.source_id
    for nome, wf in (ctx.cfg.get("workflows") or {}).items():
        wf = wf or {}
        mkey = f"workflow/{sid}/{nome}"
        res.models[mkey] = new_model(mkey, wf.get("title") or nome, sid, ctx.now,
                                     version=wf.get("version"))
        ep = new_endpoint(f"{sid}/wf:{nome}", mkey, sid, "comfyui", "workflow", ctx.now,
                          "config", service_location(ctx))
        ep["revision"] = wf.get("version")
        dip = [f"node:{n}" for n in wf.get("required_nodes") or []]
        for cartella, file in (wf.get("required_models") or {}).items():
            dip += [f"model:{cartella}/{f}" for f in file or []]
        ep["requirements"] = {"workflow": nome, "file": wf.get("file"), "dependencies": dip}
        for campo in ("input_modalities", "output_modalities", "input_formats", "output_formats"):
            if (wf.get("io") or {}).get(campo):
                ep["io"][campo] = list(wf["io"][campo])
        ep["limits"]["generation"] = wf.get("limits") or None
        ep["details"] = dict(wf.get("details") or {})
        ep["capabilities"]["declared"] = declared_caps(wf.get("capabilities"), "config:workflow")
        ep["private_costs"] = private_costs(ctx)
        ep["access"] = {"in_catalog": False, "credentials": "not_required",
                        "quota": "unknown", "executable": "unverified"}
        res.endpoints[ep["endpoint_key"]] = ep
    return res


def declared(ctx: Ctx) -> Result:
    return _records(ctx)


def _dipendenze(ctx: Ctx, base: str, wf: dict, tetto: int) -> dict:
    """Verifica nodi e file modello richiesti da un workflow. Solo GET."""
    mancanti, incerto, fatte = [], False, 0
    for nodo in wf.get("required_nodes") or []:
        if fatte >= tetto:
            incerto = True
            break
        fatte += 1
        try:
            info = ctx.client.get_json(f"{base}/object_info/{quote(str(nodo), safe='')}")
        except NetError as e:
            if e.kind == "http" and e.status == 404:
                mancanti.append(f"node:{nodo}")
            else:
                incerto = True
            continue
        if isinstance(info, dict) and nodo in info:
            continue
        if info == {}:
            mancanti.append(f"node:{nodo}")
        else:
            incerto = True
    for cartella, file in (wf.get("required_models") or {}).items():
        if fatte >= tetto:
            incerto = True
            break
        fatte += 1
        try:
            presenti = ctx.client.get_json(f"{base}/models/{quote(str(cartella), safe='')}")
        except NetError:
            incerto = True
            continue
        if not (isinstance(presenti, list) and all(isinstance(x, str) for x in presenti)):
            incerto = True
            continue
        mancanti += [f"model:{cartella}/{f}" for f in file or [] if f not in presenti]
    niente_da_verificare = not (wf.get("required_nodes") or wf.get("required_models"))
    if mancanti:
        stato = "missing"
    elif incerto or niente_da_verificare:
        stato = "unverified"
    else:
        stato = "verified"
    return {"dependencies": stato, "missing": mancanti, "checked_at": ctx.now}


def collect(ctx: Ctx) -> Result:
    base = service_url(ctx)
    health = {"state": "unknown", "inventory": "unknown", "version": None,
              "installed": None, "resident": None, "workflows": None, "error": None}
    try:
        stats = ctx.client.get_json(base + "/system_stats")
    except NetError as e:
        health.update(state=net_state(e), error=str(e))
        return Result(health=health, error=str(e))
    health["state"] = "reachable"
    if isinstance(stats, dict) and isinstance(stats.get("system"), dict):
        v = stats["system"].get("comfyui_version")
        health["version"] = v[:40] if isinstance(v, str) else None
    res = _records(ctx)
    res.health = health
    if ctx.deep:
        tetto = ctx.config["health"]["max_dependency_checks"]
        health["workflows"] = {nome: _dipendenze(ctx, base, wf or {}, tetto)
                               for nome, wf in (ctx.cfg.get("workflows") or {}).items()}
        health["inventory"] = "available"
        for nome, esito in health["workflows"].items():
            if esito["dependencies"] == "missing":
                res.notes.append(f"workflow '{nome}': dipendenze mancanti ({', '.join(esito['missing'])})")
    return res
