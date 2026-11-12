"""Consultazione del registro: fotografia in RAM, candidati, stato, riepilogo.

Scout propone candidati con le ragioni e i dati mancanti. Non sceglie: la
decisione su una richiesta concreta è di System One. Se nessuna risorsa è
compatibile lo dice in modo esplicito e NON allarga da solo privacy, budget
o autorizzazioni.
"""
from __future__ import annotations

from . import __version__, registry
from .classify import detail, free_safe, io_field, zero_cost
from .config import paths
from .health import load_health
from .util import age_seconds, parse_iso, read_json, utcnow

# ordine di preferenza degli stati di disponibilità (più basso = meglio)
_RANGO_STATO = {"present": 0, "reachable_unverified": 1, "not_checked": 1, "unknown": 2,
                "timeout": 3, "refused": 3, "error": 3, "missing": 4}
_RANGO_PROVA = {"verified": 0, "declared": 1, "heuristic": 2}


class NoRegistry(Exception):
    """Non esiste ancora un registro valido (né corrente né di copia)."""


class Snapshot:
    """Ultima fotografia valida del registro, più lo stato dei servizi.

    È una fotografia: ogni dato porta la sua età. Non è telemetria in tempo reale.
    """

    def __init__(self, reg: dict, health: dict, config: dict, origin: str, now=None):
        self.registry = reg
        self.health = health
        self.config = config
        self.origin = origin          # "current" | "backup"
        self._now = now

    def now(self):
        return self._now or utcnow()

    # ----- freschezza
    def source_stale(self, sid: str) -> bool:
        """Obsoleta se l'ultima raccolta RIUSCITA è troppo vecchia (o non c'è mai stata)."""
        s = self.registry["sources"].get(sid)
        if not s:
            return True
        eta = age_seconds(s["last_success_at"], self.now())
        return eta is None or eta > s["stale_after_seconds"]

    def endpoint_stale(self, ep: dict) -> bool:
        # ciò che viene dalla configurazione non invecchia come un catalogo
        if ep["provenance"]["verification"] == "config":
            return False
        return self.source_stale(ep["source"])

    def stale_sources(self) -> list:
        return sorted(s for s in self.registry["sources"] if self.source_stale(s))

    # ----- disponibilità
    def availability(self, ep: dict) -> dict:
        """Stato di disponibilità di un endpoint, con età e limiti del dato.

        Stati: present | reachable_unverified | missing | unknown | timeout |
        refused | error | not_checked. Nessuno stato certifica che un lavoro
        andrà a buon fine.
        """
        if ep["location"]["compute"] != "private":
            return {"state": "not_checked",
                    "note": "risorsa esterna: presente a catalogo, eseguibilità da verificare"}
        svc = self.health["services"].get(ep["source"])
        if not svc:
            return {"state": "unknown", "note": "servizio mai controllato"}
        base = {"checked_at": svc.get("checked_at"),
                "age_seconds": age_seconds(svc.get("checked_at"), self.now())}
        fino = parse_iso(svc.get("valid_until"))
        if fino is None or self.now() > fino:
            return dict(base, state="unknown", note="ultimo controllo scaduto: stato non più valido")
        stato = svc.get("state")
        if stato != "reachable":
            nota = {"timeout": "nessuna risposta entro il limite: non dimostra che la macchina sia spenta",
                    "refused": "la macchina risponde ma la porta del servizio è chiusa"}.get(
                        stato, "servizio non raggiunto")
            return dict(base, state=stato if stato in ("timeout", "refused", "error") else "unknown",
                        note=nota, error=svc.get("error"))
        nome = ep["endpoint_key"].split("/", 1)[1]
        if ep["kind"] == "workflow":
            wf = (svc.get("workflows") or {}).get(nome[3:] if nome.startswith("wf:") else nome)
            dip = (wf or {}).get("dependencies", "unverified")
            if dip == "missing":
                return dict(base, state="missing", missing=wf.get("missing", []),
                            note="servizio attivo ma mancano dipendenze del workflow")
            if dip == "verified":
                return dict(base, state="present", dependencies_checked_at=wf.get("checked_at"),
                            note="workflow configurato e dipendenze presenti; esecuzione non provata")
            return dict(base, state="reachable_unverified",
                        note="servizio attivo; dipendenze del workflow non verificate")
        if ep["provider"] == "ollama":
            installati = svc.get("installed")
            if installati is None:
                return dict(base, state="reachable_unverified", note="servizio attivo; inventario non disponibile")
            if nome not in installati:
                return dict(base, state="missing", note="servizio attivo ma modello non installato")
            residenti = svc.get("resident")
            return dict(base, state="present",
                        resident=None if residenti is None else nome in residenti,
                        note="modello installato; esecuzione non provata")
        return dict(base, state="reachable_unverified",
                    note="il servizio risponde; capacità non verificata")

    def free_safe(self) -> list:
        return free_safe(self.registry, self.stale_sources())


def load_snapshot(config: dict, now=None) -> Snapshot:
    """Legge l'ultima fotografia valida (il file corrente, oppure la copia)."""
    p = paths(config)
    reg, origine = registry.load_valid(p["registry"], p["backup"])
    if reg is None:
        raise NoRegistry("nessun registro valido: esegui prima `update`")
    return Snapshot(reg, load_health(config), config, origine, now)


# ---------------------------------------------------------------- candidati
def _evidence(ep: dict, cap: str) -> list:
    return [liv for liv in ("verified", "declared", "heuristic")
            if any(c["category"] == cap for c in ep["capabilities"][liv])]


def find_candidates(snap: Snapshot, request: dict) -> dict:
    """Candidati compatibili con requisiti tecnici e di policy.

    Campi della richiesta (tutti facoltativi tranne `capability`):
      capability            categoria richiesta
      evidence              "any" (predefinito) | "declared" | "verified"
      data_class            classe dei dati da inviare; se manca vale la PIÙ delicata
      compute               "private" | "external"
      trust_domains         elenco dei domini di fiducia ammessi
      input_modalities, output_modalities, output_formats, required_parameters
      min_context_tokens
      needs                 dettagli concreti richiesti, es. {"pbr_textures": true}
      zero_api_cost         true = solo costo API nullo NOTO
      max_price             {unità: tetto}, es. {"token_input": 0.000003}; currency (pred. "USD")
      availability          "not_missing" (predefinito) | "any" | "present"
      require_enabled       true (predefinito). false = mostra anche le non abilitate, segnalate
      allow_stale           false (predefinito)
      allow_unknown         campi che possono restare sconosciuti: context_tokens, price,
                            input_modalities, output_modalities, output_formats, parameters, needs
      sort, limit

    Un dato sconosciuto non soddisfa mai un vincolo, a meno che la richiesta non
    lo ammetta esplicitamente con `allow_unknown`.
    """
    reg, pol = snap.registry, snap.config["policy"]
    cap = request.get("capability")
    data_class = request.get("data_class")
    assunta = data_class is None
    if assunta:
        data_class = pol["data_classes"][-1]
    out = {"outcome": "ok", "decided_by": "nessuno: Scout propone, System One decide",
           "request": dict(request, data_class=data_class), "candidates": [],
           "excluded_summary": {}, "excluded": [], "considered": 0, "notes": [],
           "snapshot": {"generated_at": reg["generated_at"], "origin": snap.origin,
                        "age_seconds": age_seconds(reg["generated_at"], snap.now()),
                        "synthetic": reg["synthetic"]}}
    if assunta:
        out["notes"].append(f"data_class non indicata: assunta la più delicata ('{data_class}')")
    if cap not in reg["categories"]:
        out.update(outcome="invalid_request")
        out["notes"].append(f"categoria sconosciuta: {cap!r}")
        return out
    if data_class not in pol["data_classes"]:
        out.update(outcome="invalid_request")
        out["notes"].append(f"classe di dati sconosciuta: {data_class!r}")
        return out

    ammessi = set(request.get("allow_unknown") or [])
    valuta = request.get("currency", "USD")
    modo_disp = request.get("availability", "not_missing")
    prova_min = request.get("evidence", "any")
    candidati = []

    for key in sorted(reg["endpoints"]):
        ep = reg["endpoints"][key]
        prove = _evidence(ep, cap)
        if not prove:
            continue
        out["considered"] += 1
        fuori, mancanti = [], []
        ragioni = [f"capacità '{cap}': " + ", ".join(prove)]
        p = ep["policy"]
        decisioni = list(p["requires_user_decision"])

        def sconosciuto(campo, testo):
            if campo in ammessi:
                mancanti.append(testo)
            else:
                fuori.append(("missing_data", "dato mancante: " + testo))

        if prova_min == "verified" and "verified" not in prove:
            fuori.append(("evidence", "capacità non verificata"))
        if prova_min == "declared" and not ({"verified", "declared"} & set(prove)):
            fuori.append(("evidence", "capacità solo euristica"))

        # --- policy (mai allargata in silenzio)
        if not p["enabled"]:
            if request.get("require_enabled", True):
                fuori.append(("not_enabled", "risorsa non abilitata"))
            else:
                decisioni.append("abilitazione esplicita della risorsa")
        if data_class not in p["allowed_data_classes"]:
            fuori.append(("data_class", f"classe di dati '{data_class}' non consentita "
                                        f"(consentite: {', '.join(p['allowed_data_classes']) or 'nessuna'})"))
        if request.get("trust_domains") and p["trust_domain"] not in request["trust_domains"]:
            fuori.append(("trust_domain", f"dominio di fiducia '{p['trust_domain']}' non ammesso"))
        if request.get("compute") and ep["location"]["compute"] != request["compute"]:
            fuori.append(("compute", f"calcolo {ep['location']['compute']}, richiesto {request['compute']}"))

        # --- freschezza
        obsoleto = snap.endpoint_stale(ep)
        if obsoleto:
            if request.get("allow_stale"):
                ragioni.append("ATTENZIONE: dati obsoleti, ammessi dalla richiesta")
            else:
                fuori.append(("stale", "dati obsoleti: l'ultima raccolta riuscita è troppo vecchia"))

        # --- requisiti tecnici
        for campo in ("input_modalities", "output_modalities", "output_formats"):
            richiesti = request.get(campo)
            if not richiesti:
                continue
            ha = io_field(ep, campo)
            if ha is None:
                sconosciuto(campo, f"{campo} non noti")
            elif not set(richiesti) <= set(ha):
                fuori.append(("io", f"{campo}: richiesti {sorted(richiesti)}, disponibili {sorted(ha)}"))
        if request.get("min_context_tokens"):
            ctx = ep["limits"]["context_tokens"]
            if ctx is None:
                sconosciuto("context_tokens", "contesto massimo non noto")
            elif ctx < request["min_context_tokens"]:
                fuori.append(("context", f"contesto {ctx} sotto il minimo {request['min_context_tokens']}"))
        if request.get("required_parameters"):
            if ep["supported_parameters"] is None:
                sconosciuto("parameters", "parametri supportati non noti")
            else:
                no = sorted(set(request["required_parameters"]) - set(ep["supported_parameters"]))
                if no:
                    fuori.append(("parameters", "parametri non supportati: " + ", ".join(no)))
        for nome, voluto in (request.get("needs") or {}).items():
            ha = detail(ep, nome)
            if ha is None:
                sconosciuto("needs", f"'{nome}' non documentato")
            elif ha != voluto:
                fuori.append(("needs", f"'{nome}' è {ha!r}, richiesto {voluto!r}"))

        # --- costo (unità diverse non si confrontano; sconosciuto non è zero)
        zero = zero_cost(ep)
        if request.get("zero_api_cost"):
            if zero is None:
                sconosciuto("price", "costo API sconosciuto")
            elif not zero:
                fuori.append(("cost", "ha un costo API"))
        for unita, tetto in (request.get("max_price") or {}).items():
            if zero is True:
                continue
            voci = [x for x in ep["pricing"]["items"] if x["unit"] == unita and x["currency"] == valuta]
            if voci:
                peggiore = max(x["amount"] for x in voci)
                if peggiore > tetto:
                    fuori.append(("price", f"prezzo {peggiore} {valuta}/{unita} oltre il tetto {tetto}"))
                else:
                    ragioni.append(f"prezzo {peggiore} {valuta}/{unita} entro il tetto {tetto}")
            elif ep["pricing"]["known"]:
                altre = sorted({x["unit"] for x in ep["pricing"]["items"]})
                sconosciuto("price", f"prezzo pubblicato in unità non confrontabili con '{unita}' ({', '.join(altre)})")
            else:
                sconosciuto("price", f"prezzo sconosciuto (unità '{unita}')")
        if zero is True:
            ragioni.append("costo API nullo")
        elif zero is None and "costo API sconosciuto" not in mancanti:
            mancanti.append("costo API sconosciuto")

        # --- disponibilità
        disp = snap.availability(ep)
        if modo_disp == "not_missing" and disp["state"] == "missing":
            fuori.append(("availability", disp["note"]))
        elif modo_disp == "present" and disp["state"] != "present":
            fuori.append(("availability", f"disponibilità non confermata ({disp['state']})"))
        elif disp["state"] not in ("present",):
            mancanti.append(f"disponibilità: {disp['state']} ({disp['note']})")

        if fuori:
            for codice, _ in fuori:
                out["excluded_summary"][codice] = out["excluded_summary"].get(codice, 0) + 1
            out["excluded"].append({"endpoint_key": key, "reasons": [t for _, t in fuori]})
            continue
        if ep["location"]["compute"] == "private":
            ragioni.append(f"risorsa privata sul nodo '{ep['location']['node']}' ({ep['location']['hosting']})")
        if p["spend_limit"]:
            ragioni.append(f"limite di spesa {p['spend_limit'].get('amount')} {p['spend_limit'].get('currency')} "
                           f"per {p['spend_limit'].get('per')}: da applicare in esecuzione")
        candidati.append({
            "endpoint_key": key, "model_key": ep["model_key"], "provider": ep["provider"],
            "kind": ep["kind"], "compute": ep["location"]["compute"], "node": ep["location"]["node"],
            "trust_domain": p["trust_domain"], "enabled": p["enabled"],
            "capability_evidence": prove, "zero_api_cost": zero,
            "pricing": ep["pricing"]["items"], "context_tokens": ep["limits"]["context_tokens"],
            "requires_filtered_context": p["requires_filtered_context"],
            "spend_limit": p["spend_limit"], "availability": disp, "stale": obsoleto,
            "benchmarks": ep["benchmarks"], "reasons": ragioni, "missing_data": mancanti,
            "requires_user_decision": decisioni,
        })

    ordine = request.get("sort") or snap.config["ranking"]["default_sort"]
    candidati.sort(key=lambda c: _sort_key(c, ordine))
    limite = request.get("limit") or snap.config["ranking"]["default_limit"]
    out["candidates"] = candidati[:limite]
    out["sort"] = ordine
    out["compatible_total"] = len(candidati)
    out["excluded"] = out["excluded"][:request.get("explain_limit", 10)]
    if not candidati:
        out["outcome"] = "no_compatible_candidates"
        out["notes"].append("Nessuna risorsa compatibile. I vincoli NON sono stati allargati: "
                            "decide System One (o l'utente) se e quali cambiare.")
    return out


def _sort_key(c: dict, ordine: list) -> tuple:
    """Chiave di ordinamento. I dati mancanti finiscono in fondo: niente punteggi inventati."""
    k = []
    for voce in ordine:
        if voce == "availability":
            k.append(_RANGO_STATO.get(c["availability"]["state"], 5))
        elif voce == "private_first":
            k.append(0 if c["compute"] == "private" else 1)
        elif voce == "zero_api_cost":
            k.append(0 if c["zero_api_cost"] is True else 1)
        elif voce == "evidence":
            k.append(min(_RANGO_PROVA[p] for p in c["capability_evidence"]))
        elif voce == "context_desc":
            k.append(-(c["context_tokens"] or 0))
        elif voce.startswith("price:"):
            voci = [x["amount"] for x in c["pricing"] if x["unit"] == voce[6:]]
            k.append((0, max(voci)) if voci else ((0, 0.0) if c["zero_api_cost"] is True else (1, 0.0)))
        elif voce.startswith("benchmark:"):
            voci = [b["value"] for b in c["benchmarks"]
                    if b["metric"] == voce[10:] and isinstance(b["value"], (int, float))]
            k.append((0, -max(voci)) if voci else (1, 0.0))
    k.append(c["endpoint_key"])   # spareggio stabile
    return tuple(k)


# ---------------------------------------------------------------- stato e controlli
def resource_status(snap: Snapshot, key: str) -> dict:
    ep = snap.registry["endpoints"].get(key)
    if ep is None:
        return {"found": False, "endpoint_key": key}
    fonte = snap.registry["sources"][ep["source"]]
    return {
        "found": True, "endpoint_key": key, "model_key": ep["model_key"],
        "provider": ep["provider"], "kind": ep["kind"], "revision": ep["revision"],
        "location": ep["location"], "policy": ep["policy"], "access": ep["access"],
        "capabilities": {liv: sorted({c["category"] for c in ep["capabilities"][liv]})
                         for liv in ("declared", "verified", "heuristic")},
        "pricing_known": ep["pricing"]["known"], "pricing": ep["pricing"]["items"],
        "private_costs": ep["private_costs"], "zero_api_cost": zero_cost(ep),
        "data_policy": ep["data_policy"], "manual": ep["manual"],
        "availability": snap.availability(ep), "stale": snap.endpoint_stale(ep),
        "source": {"id": ep["source"], "last_success_at": fonte["last_success_at"],
                   "age_seconds": age_seconds(fonte["last_success_at"], snap.now()),
                   "last_attempt_outcome": fonte["last_attempt_outcome"]},
        "fetched_at": ep["provenance"]["fetched_at"],
        "verification": ep["provenance"]["verification"],
    }


def check_execution_allowed(snap: Snapshot, key: str, data_class: str | None = None,
                            estimated_cost: dict | None = None, user_approved=()) -> dict:
    """Controllo da far fare al componente di ESECUZIONE, qualunque cosa abbia proposto il decisore.

    Blocca se: la risorsa non esiste, non è abilitata, la classe di dati non è
    consentita, una dipendenza manca, serve una decisione dell'utente non
    ancora data, o il limite di spesa non è verificabile o è superato.
    estimated_cost: {"amount": n, "currency": "USD"} è una stima dichiarata da
    chi chiama, non un prezzo pubblicato.
    """
    ep = snap.registry["endpoints"].get(key)
    if ep is None:
        return {"allowed": False, "blocking": ["risorsa sconosciuta"], "warnings": [], "pending_user_decisions": []}
    p = ep["policy"]
    classi = snap.config["policy"]["data_classes"]
    data_class = data_class or classi[-1]
    blocchi, avvisi = [], []
    if not p["enabled"]:
        blocchi.append("risorsa non abilitata")
    if data_class not in p["allowed_data_classes"]:
        blocchi.append(f"classe di dati '{data_class}' non consentita per questa risorsa")
    elif p["requires_filtered_context"] and data_class != classi[0]:
        avvisi.append("il contesto deve essere già passato dal filtro di epurazione")
    in_attesa = [d for d in p["requires_user_decision"] if d not in user_approved]
    if in_attesa:
        blocchi.append("serve una decisione dell'utente: " + "; ".join(in_attesa))
    limite = p["spend_limit"]
    if limite:
        if not estimated_cost:
            blocchi.append("limite di spesa attivo ma nessuna stima di costo fornita")
        elif estimated_cost.get("currency") != limite.get("currency"):
            blocchi.append("stima e limite di spesa in valute diverse: non confrontabili")
        elif limite.get("per") == "task" and estimated_cost["amount"] > limite["amount"]:
            blocchi.append(f"stima {estimated_cost['amount']} oltre il limite {limite['amount']} {limite['currency']} per lavoro")
        elif limite.get("per") != "task":
            avvisi.append(f"limite di spesa per '{limite.get('per')}': Scout non tiene il conto, va verificato da chi lo tiene")
    disp = snap.availability(ep)
    if disp["state"] == "missing":
        blocchi.append(disp["note"])
    elif disp["state"] != "present":
        avvisi.append(f"disponibilità: {disp['state']} ({disp['note']})")
    if snap.endpoint_stale(ep):
        avvisi.append("dati del registro obsoleti per questa risorsa")
    return {"allowed": not blocchi, "blocking": blocchi, "warnings": avvisi,
            "pending_user_decisions": in_attesa, "data_class": data_class}


def summary(snap: Snapshot) -> dict:
    """Stato sintetico di Scout, serializzabile: è ciò che potrà mostrare il Loft.

    Ogni informazione porta la sua età. È una fotografia, non telemetria.
    """
    reg, now = snap.registry, snap.now()
    fonti = []
    for sid, s in sorted(reg["sources"].items()):
        fonti.append({"id": sid, "kind": s["kind"], "adapter": s["adapter"],
                      "stale": snap.source_stale(sid),
                      "last_success_at": s["last_success_at"],
                      "age_seconds": age_seconds(s["last_success_at"], now),
                      "last_attempt_at": s["last_attempt_at"],
                      "last_attempt_outcome": s["last_attempt_outcome"],
                      "last_error": s["last_error"], "endpoints": s["counts"]["endpoints"]})
    servizi = []
    for sid, svc in sorted(snap.health["services"].items()):
        fino = parse_iso(svc.get("valid_until"))
        valido = fino is not None and now <= fino
        servizi.append({"id": sid, "node": svc.get("node"),
                        "state": svc.get("state") if valido else "unknown",
                        "last_state": svc.get("state"), "valid": valido,
                        "checked_at": svc.get("checked_at"),
                        "age_seconds": age_seconds(svc.get("checked_at"), now),
                        "error": svc.get("error") if valido else None})
    eps = reg["endpoints"].values()
    try:
        ultima = read_json(paths(snap.config)["state"]).get("last_run")
    except (OSError, ValueError):
        ultima = None
    errori = [f"{f['id']}: {f['last_error']}" for f in fonti if f["last_attempt_outcome"] == "error"]
    return {
        "scout_version": __version__, "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclaimer": "fotografia con età dichiarata, non telemetria in tempo reale",
        "registry": {"generated_at": reg["generated_at"], "origin": snap.origin,
                     "age_seconds": age_seconds(reg["generated_at"], now),
                     "schema_version": reg["schema_version"], "synthetic": reg["synthetic"],
                     "models": len(reg["models"]), "endpoints": len(reg["endpoints"]),
                     "enabled": sum(1 for e in eps if e["policy"]["enabled"]),
                     "free_safe": len(snap.free_safe())},
        "last_run": ultima,
        "sources": fonti,
        "sources_fresh": [f["id"] for f in fonti if not f["stale"]],
        "sources_stale": [f["id"] for f in fonti if f["stale"]],
        "services": servizi,
        "verified_resources": sum(1 for e in eps if snap.availability(e)["state"] == "present"),
        "errors": errori,
    }
