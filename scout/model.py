"""Costruttori dei record del registro: modello, endpoint, prezzo.

Regola generale: quello che non si sa resta None. None non è zero, non è
"gratis", non è "nessun limite".
"""
from __future__ import annotations


def clean_text(valore, limit: int = 240):
    """Ripulisce un testo arrivato da un catalogo.

    È un dato non fidato: viene solo ripulito dai caratteri di controllo e
    accorciato. Non viene mai interpretato come istruzione, codice o comando.
    """
    if not isinstance(valore, str):
        return None
    testo = "".join(ch if ch.isprintable() else " " for ch in valore)
    testo = " ".join(testo.split())
    return testo[:limit] if testo else None


def new_model(key: str, name, source: str, now: str, vendor=None, version=None,
              description=None, license=None, details=None) -> dict:
    lic = {"name": None, "url": None, "source": None, "open_weight": None}
    lic.update(license or {})
    return {
        "model_key": key,
        "name": clean_text(name, 120) or key,
        "vendor": vendor,
        "version": version,
        "license": lic,
        "description_untrusted": clean_text(description),
        "details": details or {},
        "manual": None,
        "provenance": {"sources": [source], "fetched_at": now},
    }


def new_endpoint(key: str, model_key: str, source: str, provider: str, kind: str,
                 now: str, verification: str, location: dict) -> dict:
    """Scheletro di un endpoint con tutti i campi presenti e "sconosciuti"."""
    return {
        "endpoint_key": key,
        "model_key": model_key,
        "source": source,
        "provider": provider,
        "kind": kind,                       # llm | media | workflow | service
        "revision": None,
        "location": location,
        "io": {"input_modalities": None, "output_modalities": None,
               "input_formats": None, "output_formats": None},
        "limits": {"context_tokens": None, "max_output_tokens": None,
                   "generation": None, "rate_limits": None},
        "supported_parameters": None,
        "requirements": None,
        "details": {},
        "capabilities": {"declared": [], "verified": [], "heuristic": []},
        "pricing": {"known": False, "items": []},
        "private_costs": None,
        "data_policy": {"retention": None, "training_use": None, "source": None},
        "access": {"in_catalog": True, "credentials": "unknown",
                   "quota": "unknown", "executable": "unverified"},
        "policy": {"enabled": False, "allowed_data_classes": [], "requires_filtered_context": True,
                   "spend_limit": None, "requires_user_decision": [], "trust_domain": location["trust_domain"],
                   "rules": []},
        "benchmarks": [],
        "manual": None,
        "provenance": {"source": source, "fetched_at": now, "verification": verification},
    }


def price(amount, currency: str, unit: str, nature: str, source: str, now: str,
          conditions=None, raw=None) -> dict:
    """Un prezzo nella sua unità originale. Non si convertono le unità."""
    return {"amount": amount, "currency": currency, "unit": unit, "nature": nature,
            "source": source, "retrieved_at": now, "conditions": conditions, "raw": raw}


def external_location(scfg: dict) -> dict:
    return {"compute": "external", "node": None,
            "trust_domain": scfg.get("trust_domain", "provider_esterno"),
            "hosting": "provider_cloud"}


def modalities_from_category(categoria):
    """"text-to-video" -> (["text"], ["video"]). Se la forma non è x-to-y: (None, None)."""
    if isinstance(categoria, str) and "-to-" in categoria:
        a, b = categoria.split("-to-", 1)
        if a and b:
            return [a], [b]
    return None, None
