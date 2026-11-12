"""Esempio SINTETICO di come il futuro System One potrà consumare Scout.

Qui System One è finto: poche regole scritte a mano per far vedere il
contratto dati. Ogni riga del registro dice se è
    [SIMULATO]     decisione o dato inventato per l'esempio
    [SCOUT REALE]  chiamata vera alle funzioni di Scout (su dati finti)
Niente viene eseguito: nessuna stanza creata davvero, nessun lavoro avviato,
nessun modello chiamato.

Il punto dell'esempio: identità dell'agente, stanza di lavoro e modello sono
tre cose diverse. Cambiare modello non tocca né l'identità né la stanza.

    python examples/system_one_demo.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scout import api, synthetic  # noqa: E402


@dataclass(frozen=True)
class AgentIdentity:
    """Identità di un agente. Vive nel suo componente, non in Scout."""
    name: str
    role: str
    persona_version: str


@dataclass
class JobsRoom:
    """Una .Jobs. Conserva contesto e bisogni, MAI un provider."""
    name: str
    purpose: str
    needs: dict                      # capacità e classe di dati: requisiti, non un fornitore
    state: dict = field(default_factory=dict)


class FakeSystemOne:
    def __init__(self, config: dict, snap):
        self.config, self.snap = config, snap
        self.scout_queries = 0
        self.log = []
        self.agents = {"brian": AgentIdentity("Brian", "interlocutore principale", "soul-finto-1"),
                       "tommy": AgentIdentity("Tommy", "assistente personale", "tommy-finto-1")}
        self.rooms = {}
        # legame corrente fra un contesto e una risorsa: sostituibile in ogni momento
        self.bindings = {"loft": "ollama-shadow/esempio-gemma:12b"}
        self.context = "loft"
        self.activities = {"media manager": {"stato": "in corso", "attività": "montaggio bozza (finta)",
                                             "avanzamento": "40%"}}

    def dico(self, tipo: str, testo: str) -> None:
        self.log.append(f"[{tipo}] {testo}")

    def candidati(self, richiesta: dict) -> dict:
        self.scout_queries += 1
        esito = api.find_candidates(self.snap, richiesta)
        nomi = [c["endpoint_key"] for c in esito["candidates"]]
        self.dico("SCOUT REALE", f"find_candidates({richiesta}) -> {esito['outcome']}: {nomi or 'nessuno'}")
        return esito

    # ----- i cinque flussi
    def saluto(self) -> None:
        self.dico("SIMULATO", f"«Ciao Brian»: uso il modello già legato al Loft ({self.bindings['loft']}). "
                              "Nessuna consultazione di Scout, nessuna scansione dei cataloghi.")

    def riprendi_progetto(self, nome: str) -> None:
        self.dico("SIMULATO", f"«Riprendiamo {nome}»: la MEMORIA (finta) restituisce la scheda del progetto. "
                              "Scout non la legge.")
        ok = self.candidati({"capability": "fast_general", "data_class": "filtered_context"})
        niente = self.candidati({"capability": "deep_reasoning", "data_class": "filtered_context"})
        self.dico("SIMULATO", f"Il livello conversazionale può spiegare: {len(ok['candidates'])} alternative per il "
                              f"lavoro ordinario; per il ragionamento profondo esito '{niente['outcome']}' "
                              "(vincoli non allargati: si chiede all'utente).")

    def passami(self, agente: str) -> None:
        self.context = agente
        self.dico("SIMULATO", f"«Passami {self.agents[agente].name}»: cambio di contesto. "
                              "Nessun nuovo agente, nessun cambio di modello, nessuna consultazione di Scout.")

    def mostra_attivita(self, chi: str) -> None:
        self.dico("SIMULATO", f"«Cosa sta facendo il {chi}»: leggo lo stato esistente {self.activities[chi]}. "
                              "Nessun nuovo lavoro, nessuna generazione.")

    def crea_jobs(self, nome: str) -> None:
        stanza = JobsRoom(nome, "scrittura editoriale",
                          needs={"capability": "editorial_writing", "data_class": "filtered_context"},
                          state={"materiali": ["scaletta (finta)"], "attività": ["bozza capitolo 1"], "note": []})
        self.rooms[nome] = stanza
        self.dico("SIMULATO", f"Creo la .Jobs '{nome}' (la crea System One, non Scout). "
                              "La stanza salva i suoi bisogni, non un provider.")
        esito = self.candidati(stanza.needs)
        if esito["candidates"]:
            self.lega(nome, esito["candidates"][0]["endpoint_key"])

    def lega(self, contesto: str, chiave: str) -> bool:
        classe = self.rooms[contesto].needs["data_class"] if contesto in self.rooms else "public"
        via = api.check_execution_allowed(self.snap, chiave, data_class=classe,
                                          estimated_cost={"amount": 0.01, "currency": "USD"})
        self.dico("SCOUT REALE", f"check_execution_allowed({chiave}, {classe}) -> "
                                 f"{'consentito' if via['allowed'] else 'BLOCCATO: ' + '; '.join(via['blocking'])}")
        if via["allowed"]:
            self.bindings[contesto] = chiave
            self.dico("SIMULATO", f"Legame corrente di '{contesto}': {chiave}")
        return via["allowed"]


def run_demo(config: dict | None = None, verbose: bool = True) -> dict:
    tmp = None
    if config is None:
        tmp = tempfile.TemporaryDirectory()
        config, _, _ = synthetic.build_demo(tmp.name)
    try:
        s1 = FakeSystemOne(config, api.load_snapshot(config))
        s1.saluto()
        query_dopo_saluto = s1.scout_queries
        s1.riprendi_progetto("la bicicletta modulare")
        s1.passami("tommy")
        s1.mostra_attivita("media manager")
        s1.crea_jobs("redazione")

        # --- cambio di modello: identità e stanza devono restare identiche
        prima = {"identita": asdict(s1.agents["tommy"]), "stanza": copy.deepcopy(asdict(s1.rooms["redazione"])),
                 "legame": s1.bindings["redazione"]}
        esito = s1.candidati(dict(s1.rooms["redazione"].needs, limit=5))
        altri = [c["endpoint_key"] for c in esito["candidates"] if c["endpoint_key"] != prima["legame"]]
        cambiato = bool(altri) and s1.lega("redazione", altri[0])
        dopo = {"identita": asdict(s1.agents["tommy"]), "stanza": asdict(s1.rooms["redazione"]),
                "legame": s1.bindings["redazione"]}
        s1.dico("SIMULATO", f"Cambio di modello per 'redazione': {prima['legame']} -> {dopo['legame']}. "
                            f"Identità invariata: {prima['identita'] == dopo['identita']}. "
                            f"Stanza invariata: {prima['stanza'] == dopo['stanza']}.")
        if verbose:
            print("\n".join(s1.log))
        return {"before": prima, "after": dopo, "model_changed": cambiato,
                "queries_after_greeting": query_dopo_saluto, "scout_queries": s1.scout_queries,
                "context": s1.context, "log": s1.log}
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    run_demo()
