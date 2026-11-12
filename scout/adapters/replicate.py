"""Replicate: modelli per video, mesh e texture.

Fonti ufficiali (verificate l'11/11/2026):
    https://replicate.com/docs/reference/http
    https://replicate.com/docs/topics/predictions/rate-limits
    https://replicate.com/docs/topics/predictions/data-retention
    https://replicate.com/collections

Chiamate usate (solo GET, sempre con token):
    GET /v1/collections/{slug}        modelli di una collezione
    GET /v1/models/{owner}/{name}     un modello della lista configurata

Non si crea mai una prediction: nessuna generazione.

LIMITE REALE: nessun campo dell'API espone i prezzi. Restano sconosciuti
(null) finché non vengono inseriti a mano, con fonte e data, nel file delle
integrazioni manuali. Lo stesso vale per licenza testuale e formati di uscita.
"""
from __future__ import annotations

from ..model import external_location, modalities_from_category, new_endpoint, new_model
from ..net import NetError
from . import Ctx, Result, SourceError, SourceSkipped, mapped_caps

BASE = "https://api.replicate.com/v1"
DOC_LIMITI = "https://replicate.com/docs/topics/predictions/rate-limits"
DOC_DATI = "https://replicate.com/docs/topics/predictions/data-retention"


def _record(ctx: Ctx, res: Result, m: dict, collezione):
    owner, name = m.get("owner"), m.get("name")
    if not (isinstance(owner, str) and isinstance(name, str)):
        return
    sid = ctx.source_id
    mkey = f"replicate/{owner}/{name}"
    chiave = f"{sid}/{owner}/{name}"
    ultima = m.get("latest_version") if isinstance(m.get("latest_version"), dict) else {}
    versione = ultima.get("id") if isinstance(ultima.get("id"), str) else None
    url_lic = m.get("license_url") if isinstance(m.get("license_url"), str) and m["license_url"] else None
    licenza = {"name": None, "url": url_lic,
               "source": "replicate:license_url" if url_lic else None, "open_weight": None}
    if mkey not in res.models:
        res.models[mkey] = new_model(mkey, f"{owner}/{name}", sid, ctx.now, vendor=owner,
                                     version=versione, description=m.get("description"), license=licenza)
    ep = res.endpoints.get(chiave)
    if ep is None:
        ep = new_endpoint(chiave, mkey, sid, "replicate", "media", ctx.now, "catalog",
                          external_location(ctx.cfg))
        ep["revision"] = versione
        ep["details"] = {"provider_categories": []}
        for campo in ("run_count",):
            if isinstance(m.get(campo), int) and not isinstance(m.get(campo), bool):
                ep["details"][campo] = m[campo]
        # Nomi dei parametri di ingresso dallo schema dell'ultima versione (solo i nomi).
        try:
            props = ultima["openapi_schema"]["components"]["schemas"]["Input"]["properties"]
            ep["supported_parameters"] = sorted(str(k)[:60] for k in props)[:40]
        except (KeyError, TypeError):
            pass
        ep["limits"]["rate_limits"] = [
            {"scope": "creazione di prediction", "limit": 600, "per": "minute", "source": DOC_LIMITI},
            {"scope": "altri endpoint", "limit": 3000, "per": "minute", "source": DOC_LIMITI}]
        ep["data_policy"] = {
            "retention": "prediction create via API: input, output, file e log rimossi dopo un'ora, per impostazione predefinita",
            "training_use": None, "source": DOC_DATI}
        ep["access"]["credentials"] = "configured"
        res.endpoints[chiave] = ep
    if collezione:
        ep["details"]["provider_categories"].append(collezione)
        ingressi, uscite = modalities_from_category(collezione)
        if collezione == "3d-models":
            uscite = ["3d"]            # dichiarato dal titolo della collezione; l'ingresso non è indicato
        ep["io"]["input_modalities"] = ep["io"]["input_modalities"] or ingressi
        ep["io"]["output_modalities"] = ep["io"]["output_modalities"] or uscite
        esistenti = {c["category"] for c in ep["capabilities"]["declared"]}
        ep["capabilities"]["declared"] += [c for c in mapped_caps(ctx, "replicate", collezione)
                                           if c["category"] not in esistenti]


def collect(ctx: Ctx) -> Result:
    cfg = ctx.cfg
    token = ctx.secret(cfg.get("key_env", "REPLICATE_API_TOKEN"))
    if not token:
        raise SourceSkipped("token assente: l'API di Replicate richiede sempre l'autenticazione")
    headers = {"Authorization": "Bearer " + token}
    res = Result(endpoints={})
    if cfg.get("mode", "catalog") == "list":
        for nome in cfg.get("models") or []:
            try:
                m = ctx.client.get_json(f"{BASE}/models/{nome}", headers=headers)
            except NetError as e:
                if e.kind == "http" and e.status == 404:
                    res.notes.append(f"non presente a catalogo: {nome}")
                    continue
                raise
            if isinstance(m, dict):
                _record(ctx, res, m, None)
    else:
        tetto = cfg.get("max_items_per_collection", 40)
        for slug in cfg.get("collections") or []:
            data = ctx.client.get_json(f"{BASE}/collections/{slug}", headers=headers)
            lista = data.get("models") if isinstance(data, dict) else None
            if not isinstance(lista, list):
                raise SourceError(f"risposta inattesa per la collezione '{slug}': manca 'models'")
            if len(lista) > tetto:
                res.notes.append(f"collezione '{slug}' troncata a {tetto} voci su {len(lista)}")
            for m in lista[:tetto]:
                if isinstance(m, dict):
                    _record(ctx, res, m, slug)
    if res.endpoints:
        res.notes.append("prezzi non esposti dall'API: sconosciuti salvo integrazione manuale")
    return res
