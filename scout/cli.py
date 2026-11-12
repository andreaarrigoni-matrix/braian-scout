"""Riga di comando di Scout.

    python -m scout demo                         prova completa con dati finti, senza rete
    python -m scout update                       aggiorna cataloghi e inventari (da cron)
    python -m scout health                       controllo leggero dei servizi privati
    python -m scout status                       stato sintetico (quello che vedrà il Loft)
    python -m scout candidates --capability X    candidati per System One
    python -m scout show CHIAVE                  stato di una risorsa
    python -m scout recheck CHIAVE               ricontrollo prima dell'esecuzione
    python -m scout validate [FILE]              valida un registro

Codici di uscita: 0 ok, 1 errore, 3 un'altra esecuzione è in corso.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import api, health, registry, synthetic
from .config import ConfigError, load_config, paths, setup_logging
from .update import run_update
from .util import LockBusy, read_json

_DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "examples", "demo_data")


def _eta(sec) -> str:
    if sec is None:
        return "mai"
    if sec < 90:
        return f"{sec} s fa"
    if sec < 5400:
        return f"{sec // 60} min fa"
    if sec < 172800:
        return f"{sec // 3600} h fa"
    return f"{sec // 86400} g fa"


def _json(dati) -> None:
    print(json.dumps(dati, ensure_ascii=False, indent=2))


def _stampa_report(r: dict) -> None:
    print("Registro pubblicato." if r["published"] else "Registro NON pubblicato: resta valido il precedente.")
    for sid, s in r["sources"].items():
        extra = ""
        if s.get("kept_previous"):
            extra = f" (conservati {s['kept_previous']} endpoint precedenti)"
        elif s.get("declared_only"):
            extra = f" (solo quanto dichiarato in configurazione: {s['declared_only']})"
        errore = f": {s['error']}" if s.get("error") else ""
        print(f"  {sid:<18} {s['outcome']:<8} endpoint {s.get('endpoint', s.get('endpoints', 0))}{errore}{extra}")
    for n in r["notes"][:12]:
        print("  nota:", n)
    print(f"Durata: {r['duration_seconds']} s")


def _stampa_stato(q: dict) -> None:
    reg = q["registry"]
    finto = "  [DATI FINTI]" if reg["synthetic"] else ""
    print(f"Scout {q['scout_version']}{finto}  ({q['disclaimer']})")
    print(f"Registro: generato {_eta(reg['age_seconds'])}, {reg['endpoints']} endpoint, "
          f"{reg['models']} modelli, {reg['enabled']} abilitati, {reg['free_safe']} in free_safe"
          + ("  [letto dalla copia di recupero]" if reg["origin"] == "backup" else ""))
    print("Fonti:")
    for f in q["sources"]:
        stato = "OBSOLETA" if f["stale"] else "fresca"
        errore = f"  ultimo tentativo: {f['last_attempt_outcome']}" + (f" ({f['last_error']})" if f["last_error"] else "")
        print(f"  {f['id']:<18} {stato:<9} ultima raccolta riuscita: {_eta(f['age_seconds']):<12} "
              f"endpoint {f['endpoints']:<4}{errore}")
    print("Servizi privati:")
    if not q["services"]:
        print("  nessun controllo registrato")
    for s in q["services"]:
        scaduto = "" if s["valid"] else f"  (controllo scaduto; ultimo stato visto: {s['last_state']})"
        print(f"  {s['id']:<18} {s['state']:<10} controllato {_eta(s['age_seconds'])}{scaduto}"
              + (f"  {s['error']}" if s["error"] else ""))
    print(f"Risorse private con presenza verificata: {q['verified_resources']}")


def _stampa_candidati(e: dict) -> None:
    finto = "  [DATI FINTI]" if e["snapshot"]["synthetic"] else ""
    print(f"Esito: {e['outcome']}{finto}   (fotografia di {_eta(e['snapshot']['age_seconds'])})")
    for n in e["notes"]:
        print("  nota:", n)
    for i, c in enumerate(e["candidates"], 1):
        dove = f"privata su {c['node']}" if c["compute"] == "private" else "esterna"
        print(f"{i}. {c['endpoint_key']}  [{dove}, {c['availability']['state']}]")
        for r in c["reasons"]:
            print("     +", r)
        for m in c["missing_data"]:
            print("     ?", m)
        for d in c["requires_user_decision"]:
            print("     ! serve l'utente:", d)
    if e.get("excluded"):
        print(f"Esclusi ({sum(e['excluded_summary'].values())} motivi su {len(e['excluded'])} risorse mostrate):")
        for x in e["excluded"]:
            print(f"   - {x['endpoint_key']}: " + "; ".join(x["reasons"]))


def _richiesta(a) -> dict:
    r = {"capability": a.capability}
    for campo in ("data_class", "compute", "availability", "evidence", "limit"):
        if getattr(a, campo) is not None:
            r[campo] = getattr(a, campo)
    if a.min_context:
        r["min_context_tokens"] = a.min_context
    if a.zero_cost:
        r["zero_api_cost"] = True
    if a.include_disabled:
        r["require_enabled"] = False
    if a.allow_stale:
        r["allow_stale"] = True
    for campo, attr in (("output_formats", "output_format"), ("output_modalities", "output_modality"),
                        ("input_modalities", "input_modality"), ("allow_unknown", "allow_unknown"),
                        ("required_parameters", "parameter")):
        if getattr(a, attr):
            r[campo] = getattr(a, attr)
    if a.max_price:
        r["max_price"] = {}
        for voce in a.max_price:
            unita, _, valore = voce.partition("=")
            r["max_price"][unita] = float(valore)
    if a.need:
        r["needs"] = {}
        for voce in a.need:
            nome, _, valore = voce.partition("=")
            r["needs"][nome] = {"true": True, "false": False}.get(valore.lower(), valore)
    return r


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scout", description="Scout: registro delle risorse di .brAIan")
    ap.add_argument("--config", help="percorso di config.yaml (oppure variabile SCOUT_CONFIG)")
    ap.add_argument("--demo", action="store_true", help="usa i dati finti di examples/demo_data (nessuna rete)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="costruisce i dati finti e mostra stato e candidati")
    u = sub.add_parser("update", help="aggiorna cataloghi e inventari")
    u.add_argument("--only", nargs="+", help="solo queste fonti")
    u.add_argument("--offline", action="store_true", help="nessuna rete: riapplica solo configurazione e policy")
    u.add_argument("--json", action="store_true")
    h = sub.add_parser("health", help="controllo leggero dei servizi privati")
    h.add_argument("--only", nargs="+")
    h.add_argument("--deep", action="store_true", help="verifica anche le dipendenze dei workflow")
    h.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="stato sintetico di Scout")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("candidates", help="candidati per una capacità")
    c.add_argument("--capability", required=True)
    c.add_argument("--data-class", dest="data_class")
    c.add_argument("--compute", choices=["private", "external"])
    c.add_argument("--availability", choices=["not_missing", "any", "present"])
    c.add_argument("--evidence", choices=["any", "declared", "verified"])
    c.add_argument("--min-context", type=int)
    c.add_argument("--zero-cost", action="store_true")
    c.add_argument("--max-price", nargs="+", metavar="UNITA=TETTO")
    c.add_argument("--output-format", nargs="+")
    c.add_argument("--output-modality", nargs="+")
    c.add_argument("--input-modality", nargs="+")
    c.add_argument("--parameter", nargs="+")
    c.add_argument("--need", nargs="+", metavar="NOME=VALORE")
    c.add_argument("--allow-unknown", nargs="+")
    c.add_argument("--include-disabled", action="store_true")
    c.add_argument("--allow-stale", action="store_true")
    c.add_argument("--limit", type=int)
    c.add_argument("--json", action="store_true")
    sh = sub.add_parser("show", help="stato di una risorsa")
    sh.add_argument("key")
    rc = sub.add_parser("recheck", help="ricontrollo prima dell'esecuzione")
    rc.add_argument("key")
    v = sub.add_parser("validate", help="valida un registro")
    v.add_argument("file", nargs="?")
    return ap


def main(argv=None) -> int:
    for flusso in (sys.stdout, sys.stderr):
        try:
            flusso.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    a = _parser().parse_args(argv)
    demo = a.demo or a.cmd == "demo"
    try:
        if demo:
            from .config import build_config
            config = build_config(synthetic.demo_config_data(), _DEMO_DIR)
            fabbrica, _ = synthetic.factory()
            segreti = synthetic.SECRETS.get
            if a.cmd == "demo" or not os.path.isfile(paths(config)["registry"]):
                run_update(config, client_factory=fabbrica, secret=segreti, synthetic=True)
        else:
            config = load_config(a.config)
            fabbrica = segreti = None
            setup_logging(config)

        if a.cmd == "demo":
            print("=== DEMO CON DATI FINTI: nessuna rete, nessun servizio reale ===\n")
            snap = api.load_snapshot(config)
            _stampa_stato(api.summary(snap))
            for titolo, richiesta in (
                    ("Scrittura editoriale con memoria privata", {"capability": "editorial_writing",
                                                                  "data_class": "private_memory"}),
                    ("Sintesi video, dati pubblici", {"capability": "video_production.video_synthesis",
                                                      "data_class": "public"}),
                    ("Mesh con texture PBR in GLB", {"capability": "3d_modeling_pro.asset_generation",
                                                     "data_class": "public", "output_formats": ["glb"],
                                                     "needs": {"pbr_textures": True}})):
                print(f"\n--- Candidati: {titolo} ---")
                _stampa_candidati(api.find_candidates(snap, richiesta))
            print(f"\nRegistro finto scritto in: {paths(config)['registry']}")
            return 0

        if a.cmd == "update":
            r = run_update(config, only=a.only, offline=a.offline, client_factory=fabbrica,
                           secret=segreti, synthetic=demo)
            _json(r) if a.json else _stampa_report(r)
            return 0 if r["published"] else 1

        if a.cmd == "health":
            esiti = health.run_health(config, only=a.only, deep=a.deep, client_factory=fabbrica, secret=segreti)
            if a.json:
                _json(esiti)
            else:
                if not esiti:
                    print("Nessun servizio privato configurato.")
                for sid, e in esiti.items():
                    extra = []
                    if e["installed"] is not None:
                        extra.append(f"{len(e['installed'])} modelli installati")
                    if e["resident"]:
                        extra.append("in memoria: " + ", ".join(e["resident"]))
                    for nome, wf in (e["workflows"] or {}).items():
                        extra.append(f"workflow {nome}: dipendenze {wf['dependencies']}")
                    if e["error"]:
                        extra.append(e["error"])
                    print(f"  {sid:<18} {e['state']:<10} {e['latency_ms']} ms  " + "; ".join(extra))
                if any(e["state"] == "timeout" for e in esiti.values()):
                    print("  (timeout = nessuna risposta entro il limite: non dimostra che la macchina sia spenta)")
            return 0

        if a.cmd == "validate":
            file = a.file or paths(config)["registry"]
            errori = registry.validate(read_json(file))
            print("Registro valido." if not errori else "Registro NON valido:")
            for e in errori:
                print("  ", e)
            return 0 if not errori else 1

        if a.cmd == "recheck":
            _json(api.recheck(config, a.key, client_factory=fabbrica, secret=segreti))
            return 0

        snap = api.load_snapshot(config)
        if a.cmd == "status":
            q = api.summary(snap)
            _json(q) if a.json else _stampa_stato(q)
        elif a.cmd == "candidates":
            e = api.find_candidates(snap, _richiesta(a))
            _json(e) if a.json else _stampa_candidati(e)
        elif a.cmd == "show":
            _json(api.resource_status(snap, a.key))
        return 0
    except LockBusy as e:
        print("Un'altra esecuzione di Scout è in corso:", e, file=sys.stderr)
        return 3
    except (ConfigError, api.NoRegistry, OSError, ValueError) as e:
        print("Errore:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
