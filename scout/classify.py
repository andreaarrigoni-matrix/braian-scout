"""Dopo la raccolta: integrazioni manuali, categorie, benchmark, policy, viste.

Tutto deterministico: regole in configurazione e file locali. Nessun modello
legge o classifica i cataloghi. `finalize` si può ripetere quante volte si
vuole sullo stesso registro: prima toglie ciò che aveva aggiunto, poi lo
ricalcola dalla configurazione corrente.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re

from .config import resolve
from .model import price


# ---------------------------------------------------------------- letture condivise
def zero_cost(ep: dict):
    """True = costo API nullo noto; False = costa; None = non si sa.

    Un prezzo sconosciuto non è mai trattato come zero.
    """
    if ep["location"]["compute"] == "private":
        api = (ep.get("private_costs") or {}).get("api_marginal")
        if not api:
            return None
        return api["amount"] == 0
    if not ep["pricing"]["known"]:
        return None
    return all(p["amount"] == 0 for p in ep["pricing"]["items"])


def detail(ep: dict, nome: str):
    """Un dettaglio concreto: prima l'integrazione manuale, poi il dato raccolto."""
    manuale = ((ep.get("manual") or {}).get("details") or {})
    if nome in manuale:
        return manuale[nome]
    return ep["details"].get(nome)


def io_field(ep: dict, campo: str):
    manuale = ((ep.get("manual") or {}).get("io") or {})
    return manuale.get(campo) or ep["io"].get(campo)


# ---------------------------------------------------------------- file locali
def _load_manual(config: dict, notes: list) -> dict:
    file = config["manual"].get("file")
    if not file:
        return {}
    file = resolve(config, file)
    if not os.path.isfile(file):
        notes.append(f"integrazioni manuali: file non trovato ({os.path.basename(file)})")
        return {}
    import yaml
    with open(file, encoding="utf-8") as f:
        dati = yaml.safe_load(f) or {}
    out = {}
    for chiave, voce in (dati.get("overrides") or {}).items():
        if not isinstance(voce, dict) or not voce.get("source") or not voce.get("date"):
            notes.append(f"integrazione manuale '{chiave}' scartata: mancano 'source' o 'date'")
            continue
        out[chiave] = voce
    return out


def _load_benchmarks(config: dict, notes: list) -> list:
    voci = []
    for file in config["benchmarks"].get("files") or []:
        percorso = resolve(config, file)
        try:
            with open(percorso, encoding="utf-8") as f:
                dati = json.load(f)
        except (OSError, ValueError):
            notes.append(f"benchmark: file illeggibile ({os.path.basename(percorso)})")
            continue
        tipo, fonte = dati.get("kind"), dati.get("source")
        if tipo not in ("public", "provider_claim", "braian_measurement") or not fonte:
            notes.append(f"benchmark: '{os.path.basename(percorso)}' senza 'kind' valido o 'source'")
            continue
        for e in dati.get("entries") or []:
            if isinstance(e, dict) and e.get("metric") and "value" in e and e.get("date") \
                    and (e.get("endpoint_key") or e.get("model_key")):
                voci.append(dict(e, kind=tipo, source=fonte))
            else:
                notes.append(f"benchmark: voce incompleta scartata in '{os.path.basename(percorso)}'")
    return voci


# ---------------------------------------------------------------- passi di finalize
def _strip(reg: dict) -> None:
    for m in reg["models"].values():
        m["manual"] = None
    for ep in reg["endpoints"].values():
        ep["manual"] = None
        ep["benchmarks"] = []
        ep["pricing"]["items"] = [p for p in ep["pricing"]["items"] if p["nature"] == "list_price"]
        caps = ep["capabilities"]
        caps["declared"] = [c for c in caps["declared"] if not str(c.get("source", "")).startswith("manual:")]
        caps["verified"] = []
        caps["heuristic"] = []


def _apply_manual(reg: dict, ep: dict, voce: dict, notes: list) -> None:
    fonte, data = str(voce["source"]), str(voce["date"])
    traccia = {"source": fonte, "date": data, "author": voce.get("author")}
    for p in voce.get("prices") or []:
        importo = p.get("amount") if isinstance(p, dict) else None
        if isinstance(importo, bool) or not isinstance(importo, (int, float)) or importo < 0 \
                or not p.get("unit"):
            notes.append(f"{ep['endpoint_key']}: prezzo manuale scartato (importo o unità mancanti)")
            continue
        natura = p.get("nature") if p.get("nature") in ("manual", "estimate") else "manual"
        ep["pricing"]["items"].append(price(importo, p.get("currency", "USD"), str(p["unit"]), natura,
                                            "manual:" + fonte, data, conditions=p.get("conditions")))
    for cat in voce.get("capabilities") or []:
        ep["capabilities"]["declared"].append({"category": cat, "source": "manual:" + fonte})
    for v in voce.get("verified_capabilities") or []:
        if isinstance(v, dict) and v.get("category") and v.get("evidence"):
            ep["capabilities"]["verified"].append({"category": v["category"], "evidence": str(v["evidence"])})
    extra = {k: voce[k] for k in ("details", "data_policy", "io") if isinstance(voce.get(k), dict)}
    if extra:
        ep["manual"] = dict(traccia, **extra)
    if isinstance(voce.get("license"), dict):
        reg["models"][ep["model_key"]]["manual"] = dict(traccia, license=voce["license"])


def _heuristic(ep: dict, rules: list) -> list:
    """Categorie dedotte da regole esplicite. Dato mancante = regola non soddisfatta."""
    out = []
    for r in rules:
        if r.get("kind") and ep["kind"] != r["kind"]:
            continue
        if r.get("compute") and ep["location"]["compute"] != r["compute"]:
            continue
        if r.get("id_regex") and not re.search(r["id_regex"], ep["endpoint_key"], re.IGNORECASE):
            continue
        if r.get("supported_parameter") and r["supported_parameter"] not in (ep["supported_parameters"] or []):
            continue
        if r.get("min_context_tokens") and (ep["limits"]["context_tokens"] or 0) < r["min_context_tokens"]:
            continue
        if r.get("output_modality") and r["output_modality"] not in (ep["io"]["output_modalities"] or []):
            continue
        if r.get("input_modality") and r["input_modality"] not in (ep["io"]["input_modalities"] or []):
            continue
        out.append({"category": r["category"], "rule": r.get("name") or "regola senza nome"})
    return out


def _benchmarks(reg: dict, ep: dict, voci: list) -> list:
    """Solo corrispondenze esatte: niente punteggi ereditati da varianti o quantizzazioni."""
    out = []
    modello = reg["models"][ep["model_key"]]
    quant = ep["details"].get("quantization") or ep["details"].get("quantization_level")
    for e in voci:
        versione = e.get("model_version")
        if e.get("endpoint_key"):
            if e["endpoint_key"] != ep["endpoint_key"]:
                continue
            if versione and versione != ep["revision"]:
                continue
        else:
            if e["model_key"] != ep["model_key"]:
                continue
            if versione and versione not in (modello["version"], ep["revision"]):
                continue
            if quant and quant != e.get("quantization"):
                continue
        out.append({k: e.get(k) for k in ("metric", "value", "unit", "conditions", "date",
                                           "source", "kind", "model_version")})
    return out


def resolve_policy(key: str, ep: dict, config: dict) -> dict:
    """Policy di un endpoint: predefinito, poi le regole in ordine (l'ultima vince).

    Un endpoint appena scoperto, senza regole che lo riguardano, resta non abilitato.
    """
    pol = config["policy"]
    out = dict(pol["default"])
    nomi = ["default"]
    for r in pol["rules"]:
        if not fnmatch.fnmatchcase(key, r.get("match", "")):
            continue
        if r.get("compute") and r["compute"] != ep["location"]["compute"]:
            continue
        if r.get("trust_domain") and r["trust_domain"] != ep["location"]["trust_domain"]:
            continue
        for campo in ("enabled", "allowed_data_classes", "requires_filtered_context",
                      "spend_limit", "requires_user_decision"):
            if campo in r:
                out[campo] = r[campo]
        nomi.append(r.get("name") or r["match"])
    return {
        "enabled": bool(out.get("enabled")),
        "allowed_data_classes": list(out.get("allowed_data_classes") or []),
        "requires_filtered_context": bool(out.get("requires_filtered_context", True)),
        "spend_limit": out.get("spend_limit"),
        "requires_user_decision": list(out.get("requires_user_decision") or []),
        "trust_domain": ep["location"]["trust_domain"],
        "rules": nomi,
    }


def free_safe(reg: dict, stale_sources=()) -> list:
    """Vista derivata `free_safe`: gratuiti che rispettano la policy della vista.

    Non è una certificazione di sicurezza: è un filtro. Richiede endpoint
    abilitato, costo API nullo NOTO, classe di dati ammessa, dominio di
    fiducia fra quelli indicati (se indicati) e fonte non obsoleta.
    """
    regole = reg["views"]["free_safe"]["policy"]
    classe = regole.get("required_data_class", "public")
    domini = regole.get("trust_domains") or []
    out = []
    for key, ep in sorted(reg["endpoints"].items()):
        p = ep["policy"]
        if not p["enabled"] or zero_cost(ep) is not True or classe not in p["allowed_data_classes"]:
            continue
        if domini and p["trust_domain"] not in domini:
            continue
        # stessa regola di Snapshot.endpoint_stale: ciò che viene dalla
        # configurazione non invecchia come un catalogo
        if ep["source"] in stale_sources and ep["provenance"]["verification"] != "config":
            continue
        out.append(key)
    return out


def finalize(reg: dict, config: dict, notes: list) -> None:
    cats = list(config["categories"])
    reg["categories"] = cats
    _strip(reg)
    manuali = _load_manual(config, notes)
    bench = _load_benchmarks(config, notes)
    for chiave in manuali:
        if chiave not in reg["endpoints"]:
            notes.append(f"integrazione manuale '{chiave}': endpoint non presente nel registro")
    for key, ep in reg["endpoints"].items():
        if key in manuali:
            _apply_manual(reg, ep, manuali[key], notes)
        ep["capabilities"]["heuristic"] = _heuristic(ep, config["classification"]["heuristic_rules"])
        for livello in ("declared", "verified", "heuristic"):
            buone = [c for c in ep["capabilities"][livello] if c["category"] in cats]
            if len(buone) != len(ep["capabilities"][livello]):
                notes.append(f"{key}: categorie non configurate scartate")
            ep["capabilities"][livello] = buone
        ep["benchmarks"] = _benchmarks(reg, ep, bench)
        ep["pricing"]["known"] = bool(ep["pricing"]["items"])
        ep["policy"] = resolve_policy(key, ep, config)
    reg["views"]["free_safe"] = {"policy": dict(config["policy"]["free_safe"]), "endpoints": []}
    reg["views"]["free_safe"]["endpoints"] = free_safe(reg)
