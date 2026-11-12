"""fal.ai: catalogo dei servizi per video, mesh e texture.

Fonti ufficiali (verificate l'11/11/2026; docs.fal.ai rimanda a fal.ai/docs):
    https://fal.ai/docs/platform-apis/v1/models
    https://fal.ai/docs/platform-apis/v1/models/pricing

Chiamate usate (solo GET):
    GET https://api.fal.ai/v1/models            ricerca per categoria o per endpoint_id
    GET https://api.fal.ai/v1/models/pricing    prezzi (richiede la chiave)

Non si chiama mai fal.run / queue.fal.run: nessuna generazione.

Non esposto dalle API lette, quindi lasciato sconosciuto: formati di uscita,
supporto texture PBR, limiti di frequenza numerici, conservazione e uso dei
dati. L'elenco completo delle categorie non è documentato: quelle usate qui
sono le categorie indicate in configurazione.
"""
from __future__ import annotations

from urllib.parse import urlencode

from ..model import clean_text, external_location, modalities_from_category, new_endpoint, new_model, price
from ..net import NetError
from . import Ctx, Result, SourceError, mapped_caps

BASE = "https://api.fal.ai/v1"


def _pagine(ctx: Ctx, params: dict, headers: dict, note: list) -> list:
    """Segue next_cursor fino al limite di pagine configurato."""
    voci, cursore = [], None
    for _ in range(ctx.config["limits"]["max_pages"]):
        q = dict(params)
        if cursore:
            q["cursor"] = cursore
        data = ctx.client.get_json(f"{BASE}/models?{urlencode(q, doseq=True)}", headers=headers)
        lista = data.get("models") if isinstance(data, dict) else None
        if not isinstance(lista, list):
            raise SourceError("risposta inattesa: manca la lista 'models'")
        voci += [v for v in lista if isinstance(v, dict)]
        cursore = data.get("next_cursor")
        if not (isinstance(cursore, str) and cursore):
            return voci
    note.append("paginazione interrotta al limite di pagine (max_pages): elenco parziale")
    return voci


def collect(ctx: Ctx) -> Result:
    cfg, sid = ctx.cfg, ctx.source_id
    chiave = ctx.secret(cfg.get("key_env", "FAL_KEY"))
    headers = {"Authorization": "Key " + chiave} if chiave else {}
    res = Result(endpoints={})
    voci = []
    if cfg.get("mode", "catalog") == "list":
        ids = [str(x) for x in cfg.get("endpoints") or []]
        for i in range(0, len(ids), 50):           # la documentazione ammette 1-50 id per richiesta
            voci += _pagine(ctx, {"endpoint_id": ids[i:i + 50]}, headers, res.notes)
        visti = {v.get("endpoint_id") for v in voci}
        assenti = [i for i in ids if i not in visti]
        if assenti:
            res.notes.append("non presenti a catalogo: " + ", ".join(assenti))
    else:
        for cat in cfg.get("categories") or []:
            voci += _pagine(ctx, {"category": cat, "status": "active",
                                  "limit": cfg.get("page_size", 50)}, headers, res.notes)
    tetto = cfg.get("max_items", 200)
    if len(voci) > tetto:
        res.notes.append(f"catalogo troncato a {tetto} voci su {len(voci)} (max_items)")
        voci = voci[:tetto]

    for v in voci:
        eid = v.get("endpoint_id")
        md = v.get("metadata") if isinstance(v.get("metadata"), dict) else {}
        if not isinstance(eid, str) or md.get("kind") == "training":
            continue
        cat = md.get("category") if isinstance(md.get("category"), str) else None
        mkey = f"fal/{eid}"
        licenza = {"name": md.get("license_type") if isinstance(md.get("license_type"), str) else None,
                   "url": md.get("github_url") if isinstance(md.get("github_url"), str) else None,
                   "source": "fal:metadata.license_type" if md.get("license_type") else None,
                   "open_weight": None}   # il tipo di licenza non dice se i pesi sono aperti
        res.models[mkey] = new_model(mkey, md.get("display_name") or eid, sid, ctx.now,
                                     vendor=eid.split("/")[0], description=md.get("description"),
                                     license=licenza)
        ep = new_endpoint(f"{sid}/{eid}", mkey, sid, "fal", "media", ctx.now, "catalog",
                          external_location(cfg))
        ep["revision"] = md.get("updated_at") if isinstance(md.get("updated_at"), str) else None
        ep["io"]["input_modalities"], ep["io"]["output_modalities"] = modalities_from_category(cat)
        ep["details"] = {"provider_category": cat,
                         "status": md.get("status") if isinstance(md.get("status"), str) else None}
        if isinstance(md.get("tags"), list):
            ep["details"]["tags"] = [t for t in (clean_text(x, 40) for x in md["tags"][:10]) if t]
        if isinstance(md.get("duration_estimate"), (int, float)) and not isinstance(md.get("duration_estimate"), bool):
            ep["details"]["duration_estimate_minutes"] = md["duration_estimate"]
        ep["capabilities"]["declared"] = mapped_caps(ctx, "fal", cat)
        ep["access"]["credentials"] = "configured" if chiave else "missing"
        res.endpoints[ep["endpoint_key"]] = ep

    # Prezzi: endpoint separato che richiede la chiave. Se fallisce, restano sconosciuti.
    ids = [k.split("/", 1)[1] for k in res.endpoints]
    if not chiave:
        if ids:
            res.notes.append("prezzi non richiesti: chiave assente (l'API dei prezzi richiede autenticazione)")
        return res
    for i in range(0, len(ids), 50):
        try:
            data = ctx.client.get_json(f"{BASE}/models/pricing?" + urlencode({"endpoint_id": ",".join(ids[i:i + 50])}),
                                       headers=headers)
        except NetError as e:
            res.notes.append(f"prezzi non ottenuti ({e}): restano sconosciuti")
            break
        for p in data.get("prices", []) if isinstance(data, dict) and isinstance(data.get("prices"), list) else []:
            if not isinstance(p, dict):
                continue
            ep = res.endpoints.get(f"{sid}/{p.get('endpoint_id')}")
            importo, unita = p.get("unit_price"), p.get("unit")
            if ep is None or isinstance(importo, bool) or not isinstance(importo, (int, float)) \
                    or importo < 0 or not isinstance(unita, str) or not unita:
                continue
            valuta = p.get("currency") if isinstance(p.get("currency"), str) else "USD"
            ep["pricing"]["items"].append(price(importo, valuta, clean_text(unita, 40), "list_price",
                                                "fal:/v1/models/pricing", ctx.now))
    return res
