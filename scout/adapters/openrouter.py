"""OpenRouter: catalogo dei modelli testuali.

Fonti ufficiali (verificate l'11/11/2026):
    https://openrouter.ai/docs/api-reference/models/get-models
    https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model
    https://openrouter.ai/docs/guides/overview/models
    https://openrouter.ai/docs/api-reference/limits

Chiamate usate (solo GET):
    GET /api/v1/models                              catalogo
    GET /api/v1/models/{author}/{slug}/endpoints    endpoint per provider (facoltativo)

I prezzi arrivano come stringhe in USD per unità (per token, per richiesta,
per immagine...). Restano nella loro unità. Un valore negativo non è un
prezzo: viene scartato e il prezzo resta sconosciuto.

Non esposto dalle API lette, quindi lasciato sconosciuto: licenza del modello,
conservazione e uso dei dati per endpoint, limiti di frequenza del catalogo.
"""
from __future__ import annotations

import fnmatch
from urllib.parse import urlencode

from ..model import clean_text, external_location, new_endpoint, new_model, price
from ..net import NetError
from . import Ctx, Result, SourceError

BASE = "https://openrouter.ai/api/v1"
DOC = "https://openrouter.ai/docs/api-reference/models/get-models"
DOC_LIMITI = "https://openrouter.ai/docs/api-reference/limits"

# chiave del catalogo -> unità del registro
UNITA = {
    "prompt": "token_input",
    "completion": "token_output",
    "request": "request",
    "image": "image_input",
    "web_search": "web_search",
    "internal_reasoning": "token_reasoning",
    "input_cache_read": "token_cache_read",
    "input_cache_write": "token_cache_write",
}


def _prezzi(pricing, now: str, note: list, chi: str) -> list:
    out = []
    if not isinstance(pricing, dict):
        return out
    for k, v in pricing.items():
        if k in ("discount", "overrides") or isinstance(v, bool):
            continue
        try:
            importo = float(v)
        except (TypeError, ValueError):
            continue
        if importo < 0:
            note.append(f"{chi}: prezzo '{k}' non numerico utile ({v}), lasciato sconosciuto")
            continue
        if importo == 0 and k not in ("prompt", "completion"):
            continue  # voci accessorie a zero: non aggiungono informazione
        out.append(price(importo, "USD", UNITA.get(k, f"openrouter:{k}"), "list_price",
                         f"openrouter:pricing.{k}", now, raw=str(v)))
    return out


def _lista(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else None


def _intero(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def collect(ctx: Ctx) -> Result:
    cfg, sid = ctx.cfg, ctx.source_id
    chiave = ctx.secret(cfg.get("key_env", "OPENROUTER_API_KEY"))
    headers = {"Authorization": "Bearer " + chiave} if chiave else {}
    query = {k: v for k, v in (cfg.get("query") or {}).items() if v is not None}
    url = BASE + "/models" + ("?" + urlencode(query) if query else "")
    data = ctx.client.get_json(url, headers=headers, conditional=True)
    voci = data.get("data") if isinstance(data, dict) else None
    if not isinstance(voci, list):
        raise SourceError("risposta inattesa: manca la lista 'data'")
    per_id = {m["id"]: m for m in voci if isinstance(m, dict) and isinstance(m.get("id"), str)}

    res = Result(endpoints={})
    modo = cfg.get("mode", "list")
    if modo == "list":
        voluti = [str(x) for x in cfg.get("models") or []]
        scelti = [i for i in voluti if i in per_id]
        assenti = [i for i in voluti if i not in per_id]
        if assenti:
            res.notes.append("non presenti a catalogo: " + ", ".join(assenti))
    else:
        filtri = cfg.get("include") or ["*"]
        scelti = sorted(i for i in per_id if any(fnmatch.fnmatchcase(i, f) for f in filtri))
        tetto = cfg.get("max_items", 400)
        if len(scelti) > tetto:
            res.notes.append(f"catalogo troncato a {tetto} voci su {len(scelti)} (max_items)")
            scelti = scelti[:tetto]

    for mid in scelti:
        m = per_id[mid]
        base_id = mid.split(":")[0]
        arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
        top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
        slug = m.get("canonical_slug") if isinstance(m.get("canonical_slug"), str) else None
        dettagli = {}
        if isinstance(m.get("hugging_face_id"), str) and m["hugging_face_id"]:
            dettagli["hugging_face_id"] = m["hugging_face_id"][:120]
        res.models.setdefault(base_id, new_model(
            base_id, m.get("name"), sid, ctx.now, vendor=base_id.split("/")[0],
            version=slug, description=m.get("description"), details=dettagli))
        ep = new_endpoint(f"{sid}/{mid}", base_id, sid, "openrouter", "llm", ctx.now,
                          "catalog", external_location(cfg))
        ep["revision"] = slug
        ep["io"]["input_modalities"] = _lista(arch.get("input_modalities"))
        ep["io"]["output_modalities"] = _lista(arch.get("output_modalities"))
        ep["limits"]["context_tokens"] = _intero(m.get("context_length"))
        ep["limits"]["max_output_tokens"] = _intero(top.get("max_completion_tokens"))
        ep["supported_parameters"] = _lista(m.get("supported_parameters"))
        for campo in ("tokenizer",):
            if isinstance(arch.get(campo), str):
                ep["details"][campo] = arch[campo][:60]
        if isinstance(top.get("is_moderated"), bool):
            ep["details"]["is_moderated"] = top["is_moderated"]
        if isinstance(m.get("expiration_date"), str):
            ep["details"]["expiration_date"] = m["expiration_date"][:40]
        if isinstance(m.get("pricing"), dict) and m["pricing"].get("overrides"):
            ep["details"]["conditional_pricing_present"] = True
        ep["pricing"]["items"] = _prezzi(m.get("pricing"), ctx.now, res.notes, mid)
        if mid.endswith(":free"):
            ep["limits"]["rate_limits"] = [
                {"scope": "varianti :free", "limit": 20, "per": "minute", "source": DOC_LIMITI},
                {"scope": "varianti :free", "note": "50 oppure 1000 richieste al giorno secondo i crediti acquistati",
                 "source": DOC_LIMITI}]
        ep["access"]["credentials"] = "configured" if chiave else "missing"
        res.endpoints[ep["endpoint_key"]] = ep

    # Dettaglio per provider: solo in modalità lista, solo se richiesto e con chiave.
    if cfg.get("endpoints_detail") and modo == "list":
        if not chiave:
            res.notes.append("dettaglio per provider saltato: chiave assente")
        for mid in scelti if chiave else []:
            try:
                d = ctx.client.get_json(f"{BASE}/models/{mid.split(':')[0]}/endpoints", headers=headers)
            except NetError as e:
                res.notes.append(f"{mid}: dettaglio per provider non ottenuto ({e})")
                continue
            lista = d.get("data", {}).get("endpoints") if isinstance(d, dict) and isinstance(d.get("data"), dict) else None
            for e in lista if isinstance(lista, list) else []:
                if not isinstance(e, dict) or not isinstance(e.get("provider_name"), str):
                    continue
                tag = e.get("tag") if isinstance(e.get("tag"), str) else e["provider_name"]
                tag = clean_text(tag, 60) or "provider"
                base_id = mid.split(":")[0]
                ep = new_endpoint(f"{sid}/{mid}@{tag}", base_id, sid, clean_text(e["provider_name"], 60),
                                  "llm", ctx.now, "catalog", external_location(cfg))
                genitore = res.endpoints[f"{sid}/{mid}"]
                ep["revision"] = genitore["revision"]
                ep["io"] = dict(genitore["io"])
                ep["limits"]["context_tokens"] = _intero(e.get("context_length"))
                ep["limits"]["max_output_tokens"] = _intero(e.get("max_completion_tokens"))
                ep["supported_parameters"] = _lista(e.get("supported_parameters"))
                if isinstance(e.get("quantization"), str):
                    ep["details"]["quantization"] = e["quantization"][:20]
                # Misure riportate da OpenRouter al momento della raccolta: una fotografia.
                riportate = {k: e[k] for k in ("uptime_last_30m", "uptime_last_1d")
                             if isinstance(e.get(k), (int, float)) and not isinstance(e.get(k), bool)}
                if riportate:
                    ep["details"]["provider_reported"] = riportate
                ep["pricing"]["items"] = _prezzi(e.get("pricing"), ctx.now, res.notes, f"{mid}@{tag}")
                ep["access"]["credentials"] = "configured"
                res.endpoints[ep["endpoint_key"]] = ep
    return res
