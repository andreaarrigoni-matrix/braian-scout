"""Endpoint generico configurabile: per risorse future (Numero Uno, altri nodi).

Fa una sola GET all'indirizzo di controllo indicato. Una risposta dice solo
che il servizio risponde: le capacità restano quelle dichiarate in
configurazione, non verificate.
"""
from __future__ import annotations

from ..model import new_endpoint, new_model
from ..net import NetError
from . import Ctx, Result, declared_caps, net_state, private_costs, service_location, service_url


def declared(ctx: Ctx) -> Result:
    res = Result(endpoints={})
    sid = ctx.source_id
    for nome, e in (ctx.cfg.get("endpoints") or {}).items():
        e = e or {}
        mkey = e.get("model") or f"service/{sid}/{nome}"
        res.models[mkey] = new_model(mkey, e.get("title") or nome, sid, ctx.now, version=e.get("version"))
        ep = new_endpoint(f"{sid}/{nome}", mkey, sid, e.get("provider", "generic"),
                          e.get("kind", "service"), ctx.now, "config", service_location(ctx))
        ep["revision"] = e.get("version")
        for campo in ("input_modalities", "output_modalities", "input_formats", "output_formats"):
            if (e.get("io") or {}).get(campo):
                ep["io"][campo] = list(e["io"][campo])
        if isinstance(e.get("context_tokens"), int):
            ep["limits"]["context_tokens"] = e["context_tokens"]
        ep["details"] = dict(e.get("details") or {})
        ep["capabilities"]["declared"] = declared_caps(e.get("capabilities"), "config:endpoint")
        if ep["location"]["compute"] == "private":
            ep["private_costs"] = private_costs(ctx)
        ep["access"] = {"in_catalog": False,
                        "credentials": "not_required" if not ctx.cfg.get("key_env") else
                        ("configured" if ctx.secret(ctx.cfg["key_env"]) else "missing"),
                        "quota": "unknown", "executable": "unverified"}
        res.endpoints[ep["endpoint_key"]] = ep
    return res


def collect(ctx: Ctx) -> Result:
    health = {"state": "unknown", "inventory": "unknown", "version": None,
              "installed": None, "resident": None, "workflows": None, "error": None}
    url = service_url(ctx) + str(ctx.cfg.get("health_path", "/"))
    try:
        ctx.client.get_json(url, raw=True)
    except NetError as e:
        health.update(state=net_state(e), error=str(e))
        return Result(health=health, error=str(e))
    health["state"] = "reachable"
    res = declared(ctx)
    res.health = health
    return res
