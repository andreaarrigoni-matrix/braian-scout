"""PROPOSTA di adattatore fra Scout e il regista esistente (hermes/regista).

Legge il registro di Scout e STAMPA voci nel formato dei "motori" di
regista.json. Non scrive regista.json e non tocca il regista.

Stato: l'integrazione NON è verificata sul regista in esecuzione. Il formato
è stato ricavato leggendo hermes/regista/regista.json (campi: nome, base_url,
modello, privato, nota).

    python examples/regista_proposta.py [--config config.yaml]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import api  # noqa: E402


def proposta(config: dict) -> list:
    snap = api.load_snapshot(config)
    piu_delicata = config["policy"]["data_classes"][-1]
    motori = []
    for key, ep in sorted(snap.registry["endpoints"].items()):
        if ep["provider"] != "ollama" or not ep["policy"]["enabled"]:
            continue
        scfg = config["sources"][ep["source"]]
        nodo = config["nodes"].get(scfg.get("node") or "", {})
        # l'indirizzo viene dalla configurazione: il registro non contiene indirizzi di rete
        base = str(scfg.get("url", "")).format(address=nodo.get("address", "")).rstrip("/")
        disp = snap.availability(ep)
        motori.append({
            "nome": key.replace("/", "-"),
            "base_url": base + "/v1",
            "modello": ep["details"].get("service_model"),
            "privato": piuttosto_privato(ep, piu_delicata),
            "nota": f"proposto da Scout; disponibilità all'ultimo controllo: {disp['state']}",
        })
    return motori


def piuttosto_privato(ep: dict, classe: str) -> bool:
    """Per il regista 'privato' = può ricevere la memoria: lo decide la policy, non il prezzo."""
    return classe in ep["policy"]["allowed_data_classes"]


if __name__ == "__main__":
    percorso = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else None
    print("PROPOSTA (non scritta in regista.json, integrazione non verificata):")
    print(json.dumps(proposta(api.load_config(percorso)), ensure_ascii=False, indent=2))
