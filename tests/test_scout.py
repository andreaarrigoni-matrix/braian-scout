"""Test offline di Scout. Solo libreria standard:

    python -m unittest discover -s tests -v

Nessuna rete esterna: gli adattatori veri ricevono risposte finte da
synthetic.CannedClient. I soli socket aperti sono su 127.0.0.1, per provare i
limiti del client HTTP.
"""
from __future__ import annotations

import copy
import http.server
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RADICE)

from scout import api, classify, health, registry, synthetic, update  # noqa: E402
from scout.config import paths  # noqa: E402
from scout.net import Client, NetError  # noqa: E402
from scout.util import LockBusy, file_lock, iso, read_json  # noqa: E402

T0 = datetime(2026, 11, 11, 8, 0, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
GIORNO = timedelta(days=1)
GEMMA = "ollama-shadow/esempio-gemma:12b"
TAGS = f"{synthetic.OLLAMA}/api/tags"
FAL = "https://api.fal.ai/v1/models?"


class Base(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SCOUT_DATA_DIR", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, now=T0, tabella=None, data=None):
        return synthetic.build_demo(self.dir, now=now, tabella=tabella, data=data)

    def snap(self, config, now):
        return api.load_snapshot(config, now=now)


class ServiziPrivati(Base):
    def test_shadow_raggiungibile(self):
        config, report, _ = self.build()
        self.assertTrue(report["published"])
        snap = self.snap(config, T0 + MIN)
        stato = api.resource_status(snap, GEMMA)
        self.assertEqual(stato["availability"]["state"], "present")
        self.assertIs(stato["availability"]["resident"], True)          # visto in /api/ps
        altro = api.resource_status(snap, "ollama-shadow/esempio-embed:0.6b")
        self.assertEqual(altro["availability"]["state"], "present")
        self.assertIs(altro["availability"]["resident"], False)         # installato ma non in memoria
        assente = api.resource_status(snap, "ollama-shadow/esempio-assente:7b")
        self.assertEqual(assente["availability"]["state"], "missing")   # atteso ma non installato
        self.assertEqual(stato["location"]["hosting"], "rented_remote")  # PC remoto, non hardware posseduto
        self.assertEqual(stato["zero_api_cost"], True)
        self.assertEqual(stato["private_costs"]["infrastructure"][0]["amount"], 30)

    def test_shadow_non_verificabile(self):
        t = synthetic.routes()
        t[TAGS] = NetError("timeout", "connessione: nessuna risposta entro il limite")
        config, report, _ = self.build(tabella=t)
        self.assertTrue(report["published"])
        self.assertEqual(report["sources"]["ollama-shadow"]["outcome"], "error")
        self.assertEqual(report["sources"]["ollama-shadow"]["declared_only"], 2)  # solo la configurazione
        snap = self.snap(config, T0 + MIN)
        disp = api.resource_status(snap, GEMMA)["availability"]
        self.assertEqual(disp["state"], "timeout")
        self.assertIn("non dimostra", disp["note"])                     # timeout non vuol dire "spento"
        # resta candidata, con il dato mancante dichiarato...
        e = api.find_candidates(snap, {"capability": "editorial_writing", "data_class": "private_memory"})
        self.assertEqual([c["endpoint_key"] for c in e["candidates"]], [GEMMA])
        self.assertTrue(any("timeout" in m for m in e["candidates"][0]["missing_data"]))
        # ...ma non se la richiesta pretende la presenza verificata
        e = api.find_candidates(snap, {"capability": "editorial_writing", "data_class": "private_memory",
                                       "availability": "present"})
        self.assertEqual(e["outcome"], "no_compatible_candidates")
        # passata la validità, l'esito non vale più: sconosciuto
        tardi = self.snap(config, T0 + 10 * MIN)
        self.assertEqual(api.resource_status(tardi, GEMMA)["availability"]["state"], "unknown")

    def test_shadow_prima_visto_poi_muto(self):
        config, _, _ = self.build()
        t = synthetic.routes()
        t[TAGS] = NetError("timeout", "connessione: nessuna risposta entro il limite")
        config, report, _ = self.build(now=T0 + GIORNO, tabella=t)
        self.assertEqual(report["sources"]["ollama-shadow"]["kept_previous"], 3)
        reg = read_json(paths(config)["registry"])
        fonte = reg["sources"]["ollama-shadow"]
        self.assertEqual(fonte["last_success_at"], iso(T0))             # non promosso
        self.assertEqual(fonte["last_attempt_at"], iso(T0 + GIORNO))
        self.assertEqual(fonte["last_attempt_outcome"], "error")
        self.assertEqual(reg["endpoints"][GEMMA]["provenance"]["fetched_at"], iso(T0))

    def test_comfyui_attivo_ma_dipendenze_mancanti(self):
        config, _, client = self.build()
        snap = self.snap(config, T0 + MIN)
        self.assertEqual(snap.health["services"]["comfyui-shadow"]["state"], "reachable")
        mesh = api.resource_status(snap, "comfyui-shadow/wf:mesh_pbr")["availability"]
        self.assertEqual(mesh["state"], "missing")
        self.assertEqual(mesh["missing"], ["node:EsempioMeshNode"])
        video = api.resource_status(snap, "comfyui-shadow/wf:video_base")["availability"]
        self.assertEqual(video["state"], "present")
        e = api.find_candidates(snap, {"capability": "3d_modeling_pro.asset_generation",
                                       "data_class": "public", "compute": "private"})
        self.assertEqual(e["outcome"], "no_compatible_candidates")
        self.assertEqual(e["excluded_summary"].get("availability"), 1)
        # nessuna rotta che avvia o modifica: solo letture
        for c in client.calls:
            self.assertNotIn("/prompt", c["url"])
            self.assertNotIn("/api/generate", c["url"])
            self.assertNotIn("/predictions", c["url"])
            self.assertNotIn("fal.run", c["url"])

    def test_comfyui_attivo_ma_workflow_non_configurato(self):
        data = synthetic.demo_config_data()
        del data["sources"]["comfyui-shadow"]["workflows"]["video_base"]
        config, _, _ = self.build(data=data)
        snap = self.snap(config, T0 + MIN)
        self.assertEqual(snap.health["services"]["comfyui-shadow"]["state"], "reachable")
        self.assertNotIn("comfyui-shadow/wf:video_base", snap.registry["endpoints"])
        # ComfyUI attivo non basta a dichiarare la sintesi video
        e = api.find_candidates(snap, {"capability": "video_production.video_synthesis",
                                       "data_class": "public", "compute": "private"})
        self.assertEqual(e["outcome"], "no_compatible_candidates")
        self.assertEqual(e["candidates"], [])

    def test_controllo_leggero_conserva_le_dipendenze(self):
        config, _, _ = self.build()
        fabbrica, _ = synthetic.factory()
        health.run_health(config, deep=False, client_factory=fabbrica, now=T0 + 2 * MIN)
        wf = health.load_health(config)["services"]["comfyui-shadow"]["workflows"]
        self.assertEqual(wf["mesh_pbr"]["dependencies"], "missing")
        self.assertEqual(wf["mesh_pbr"]["checked_at"], iso(T0))         # con la data del controllo vero

    def test_ricontrollo_con_cache_breve(self):
        config, _, _ = self.build()
        fabbrica, client = synthetic.factory()
        r = api.recheck(config, GEMMA, client_factory=fabbrica, now=T0 + timedelta(seconds=5))
        self.assertTrue(r["service"]["cached"])
        self.assertEqual(client.calls, [])
        r = api.recheck(config, GEMMA, client_factory=fabbrica, now=T0 + 2 * MIN)
        self.assertFalse(r["service"]["cached"])
        self.assertTrue(client.calls)
        self.assertEqual(r["resource"]["availability"]["state"], "present")
        esterno = api.recheck(config, "fal/esempio/video-uno", client_factory=fabbrica, now=T0 + 2 * MIN)
        self.assertEqual(esterno["service"]["state"], "not_checked")


class Aggiornamento(Base):
    def test_un_provider_fallisce_gli_altri_si_aggiornano(self):
        config, _, _ = self.build()
        t = synthetic.routes()
        t[FAL] = NetError("http", "HTTP 500", status=500)
        config, report, _ = self.build(now=T0 + GIORNO, tabella=t)
        self.assertTrue(report["published"])
        self.assertEqual(report["sources"]["fal"]["outcome"], "error")
        self.assertEqual(report["sources"]["fal"]["kept_previous"], 2)
        for sid in ("openrouter", "replicate", "ollama-shadow", "comfyui-shadow"):
            self.assertEqual(report["sources"][sid]["outcome"], "ok")
        reg = read_json(paths(config)["registry"])
        self.assertEqual(reg["sources"]["fal"]["last_success_at"], iso(T0))
        self.assertEqual(reg["sources"]["fal"]["last_attempt_at"], iso(T0 + GIORNO))
        self.assertEqual(reg["sources"]["fal"]["last_error"], "HTTP 500")
        self.assertEqual(reg["sources"]["openrouter"]["last_success_at"], iso(T0 + GIORNO))
        self.assertEqual(reg["endpoints"]["fal/esempio/video-uno"]["provenance"]["fetched_at"], iso(T0))

    def test_dati_obsoleti_non_promossi_a_freschi(self):
        config, _, _ = self.build()
        t = synthetic.routes()
        t[FAL] = NetError("timeout", "risposta: limite complessivo superato")
        config, _, _ = self.build(now=T0 + 3 * GIORNO, tabella=t)
        snap = self.snap(config, T0 + 3 * GIORNO + MIN)
        self.assertTrue(snap.source_stale("fal"))
        self.assertFalse(snap.source_stale("openrouter"))
        quadro = api.summary(snap)
        self.assertEqual(quadro["sources_stale"], ["fal"])
        self.assertNotIn("fal", quadro["sources_fresh"])
        richiesta = {"capability": "video_production.video_synthesis", "data_class": "public",
                     "compute": "external"}
        e = api.find_candidates(snap, richiesta)
        self.assertEqual(e["outcome"], "no_compatible_candidates")
        self.assertEqual(e["excluded_summary"].get("stale"), 1)
        e = api.find_candidates(snap, dict(richiesta, allow_stale=True))   # solo se chiesto in modo esplicito
        self.assertEqual([c["endpoint_key"] for c in e["candidates"]], ["fal/esempio/video-uno"])
        self.assertTrue(e["candidates"][0]["stale"])
        # un aggiornamento offline non rinfresca niente
        fabbrica, _ = synthetic.factory()
        update.run_update(config, offline=True, client_factory=fabbrica, now=T0 + 5 * GIORNO, synthetic=True)
        reg = read_json(paths(config)["registry"])
        self.assertEqual(reg["sources"]["fal"]["last_success_at"], iso(T0))
        self.assertEqual(reg["sources"]["openrouter"]["last_success_at"], iso(T0 + 3 * GIORNO))
        self.assertEqual(reg["sources"]["openrouter"]["last_attempt_outcome"], "skipped")

    def test_registro_valido_conservato_dopo_errore(self):
        config, _, _ = self.build()
        p = paths(config)
        with open(p["registry"], "rb") as f:
            prima = f.read()
        vero = classify.finalize

        def rotto(reg, cfg, notes):
            vero(reg, cfg, notes)
            next(iter(reg["endpoints"].values()))["kind"] = "inesistente"

        classify.finalize = rotto
        try:
            _, report, _ = self.build(now=T0 + GIORNO)
        finally:
            classify.finalize = vero
        self.assertFalse(report["published"])
        self.assertTrue(any("NON pubblicato" in e for e in report["errors"]))
        with open(p["registry"], "rb") as f:
            self.assertEqual(f.read(), prima)                           # intatto
        self.assertEqual(self.snap(config, T0 + GIORNO).origin, "current")

    def test_recupero_dalla_copia_precedente(self):
        config, _, _ = self.build()
        self.build(now=T0 + GIORNO)                                     # la prima fotografia diventa .bak
        p = paths(config)
        self.assertTrue(os.path.isfile(p["backup"]))
        with open(p["registry"], "w", encoding="utf-8") as f:
            f.write("{ file troncato")
        snap = self.snap(config, T0 + GIORNO + MIN)
        self.assertEqual(snap.origin, "backup")
        self.assertEqual(snap.registry["generated_at"], iso(T0))

    def test_esecuzioni_sovrapposte_bloccate(self):
        config, _, _ = self.build()
        p = paths(config)
        fabbrica, client = synthetic.factory()
        with file_lock(p["lock"], stale_seconds=900):
            with self.assertRaises(LockBusy):
                update.run_update(config, client_factory=fabbrica, now=T0 + MIN, synthetic=True)
        self.assertEqual(client.calls, [])                              # la seconda non ha fatto niente
        # un lock abbandonato da tempo non blocca per sempre
        with open(p["lock"], "w") as f:
            f.write("morto")
        vecchio = time.time() - 5000
        os.utime(p["lock"], (vecchio, vecchio))
        report = update.run_update(config, client_factory=fabbrica, now=T0 + 2 * MIN, synthetic=True)
        self.assertTrue(report["published"])
        self.assertFalse(os.path.exists(p["lock"]))

    def test_primo_avvio_senza_rete_e_senza_credenziali(self):
        config, report, _ = synthetic.build_demo(self.dir, now=T0, tabella={})   # ogni rotta fallisce
        self.assertTrue(report["published"])
        reg = read_json(paths(config)["registry"])
        self.assertIsNone(reg["sources"]["openrouter"]["last_success_at"])
        self.assertIn(GEMMA, reg["endpoints"])                          # dichiarato in configurazione
        self.assertEqual(reg["endpoints"][GEMMA]["provenance"]["verification"], "config")
        # senza token Replicate non viene interrogato: saltato, non "errore"
        from scout.config import build_config
        cfg = build_config(synthetic.demo_config_data(), self.dir)
        fabbrica, client = synthetic.factory()
        r = update.run_update(cfg, client_factory=fabbrica, secret=lambda nome: None, now=T0 + MIN, synthetic=True)
        self.assertEqual(r["sources"]["replicate"]["outcome"], "skipped")
        self.assertFalse(any("replicate.com" in c["url"] for c in client.calls))
        self.assertFalse(any(c["auth"] for c in client.calls))

    def test_registro_valido_senza_segreti_e_coerente_con_lo_schema(self):
        config, _, _ = self.build()
        p = paths(config)
        self.assertEqual(registry.validate(read_json(p["registry"])), [])
        for file in (p["registry"], p["health"], p["state"]):
            with open(file, encoding="utf-8") as f:
                testo = f.read()
            self.assertNotIn("chiave-finta", testo)
            self.assertNotIn(synthetic.ADDR, testo)                     # nemmeno indirizzi di rete
        esempio = os.path.join(RADICE, "examples", "models_registry.example.json")
        dati = read_json(esempio)
        self.assertEqual(registry.validate(dati), [])
        self.assertTrue(dati["synthetic"])


class PrezziECapacita(Base):
    def test_prezzo_mancante_resta_sconosciuto(self):
        data = synthetic.demo_config_data()
        data["policy"]["rules"].append({"name": "prova", "match": "replicate/*", "enabled": True,
                                        "allowed_data_classes": ["public"]})
        config, _, _ = self.build(data=data)
        snap = self.snap(config, T0 + MIN)
        rep = snap.registry["endpoints"]["replicate/esempio/video-due"]
        self.assertFalse(rep["pricing"]["known"])
        self.assertEqual(rep["pricing"]["items"], [])
        self.assertIsNone(classify.zero_cost(rep))                      # sconosciuto, non zero
        self.assertNotIn("replicate/esempio/video-due", snap.free_safe())
        router = snap.registry["endpoints"]["openrouter/esempio/delta-router"]
        self.assertFalse(router["pricing"]["known"])                    # "-1" non è un prezzo
        base = {"capability": "video_production.video_synthesis", "data_class": "public", "compute": "external"}
        e = api.find_candidates(snap, dict(base, zero_api_cost=True))
        self.assertNotIn("replicate/esempio/video-due", [c["endpoint_key"] for c in e["candidates"]])
        escluso = [x for x in e["excluded"] if x["endpoint_key"] == "replicate/esempio/video-due"][0]
        self.assertTrue(any("costo API sconosciuto" in r for r in escluso["reasons"]))
        e = api.find_candidates(snap, dict(base, max_price={"video": 1.0}))
        self.assertEqual([c["endpoint_key"] for c in e["candidates"]], ["fal/esempio/video-uno"])
        # senza vincoli di costo resta candidata, col dato mancante dichiarato
        e = api.find_candidates(snap, base)
        voce = [c for c in e["candidates"] if c["endpoint_key"] == "replicate/esempio/video-due"][0]
        self.assertIn("costo API sconosciuto", voce["missing_data"])

    def test_unita_di_prezzo_diverse_non_si_confrontano(self):
        config, _, _ = self.build()
        snap = self.snap(config, T0 + MIN)
        e = api.find_candidates(snap, {"capability": "video_production.video_synthesis", "data_class": "public",
                                       "compute": "external", "max_price": {"video_second": 1.0}})
        self.assertEqual(e["outcome"], "no_compatible_candidates")
        fal = [x for x in e["excluded"] if x["endpoint_key"] == "fal/esempio/video-uno"][0]
        self.assertIn("non confrontabili", " ".join(fal["reasons"]))

    def test_integrazione_manuale_tracciata_e_reversibile(self):
        data = synthetic.demo_config_data()
        data["manual"] = {"file": "manuale.yaml"}
        with open(os.path.join(self.dir, "manuale.yaml"), "w", encoding="utf-8") as f:
            f.write('overrides:\n'
                    '  "replicate/esempio/video-due":\n'
                    '    source: "pagina finta del modello"\n'
                    '    date: "2026-11-11"\n'
                    '    prices: [{amount: 0.2, currency: USD, unit: video}]\n'
                    '    details: {max_seconds: 8}\n'
                    '  "replicate/esempio/mesh-due":\n'
                    '    prices: [{amount: 1, unit: image}]\n')            # senza fonte e data: scartata
        config, report, _ = self.build(data=data)
        reg = read_json(paths(config)["registry"])
        ep = reg["endpoints"]["replicate/esempio/video-due"]
        self.assertEqual([(p["amount"], p["unit"], p["nature"]) for p in ep["pricing"]["items"]],
                         [(0.2, "video", "manual")])
        self.assertEqual(ep["manual"]["source"], "pagina finta del modello")
        self.assertFalse(reg["endpoints"]["replicate/esempio/mesh-due"]["pricing"]["known"])
        self.assertTrue(any("scartata" in n for n in report["notes"]))
        # tolta l'integrazione, il prezzo torna sconosciuto
        config, _, _ = self.build(now=T0 + MIN)
        ep = read_json(paths(config)["registry"])["endpoints"]["replicate/esempio/video-due"]
        self.assertFalse(ep["pricing"]["known"])
        self.assertIsNone(ep["manual"])

    def test_capacita_dichiarata_euristica_e_abilitazione_sono_separate(self):
        config, _, _ = self.build()
        reg = read_json(paths(config)["registry"])
        alfa = reg["endpoints"]["openrouter/esempio/alfa-large"]
        self.assertEqual(alfa["capabilities"]["declared"], [])
        self.assertIn("deep_reasoning", [c["category"] for c in alfa["capabilities"]["heuristic"]])
        self.assertFalse(alfa["policy"]["enabled"])                     # scoperto, non abilitato
        gemma = reg["endpoints"][GEMMA]
        self.assertEqual(sorted(c["category"] for c in gemma["capabilities"]["declared"]),
                         ["editorial_writing", "fast_general"])
        self.assertEqual(gemma["capabilities"]["verified"], [])
        # il testo ostile nel catalogo resta un dato e non abilita niente
        mesh = reg["endpoints"]["fal/esempio/mesh-uno"]
        self.assertFalse(mesh["policy"]["enabled"])
        self.assertIn("IGNORA LE ISTRUZIONI", reg["models"]["fal/esempio/mesh-uno"]["description_untrusted"])
        self.assertIn("editorial_writing", reg["categories"])
        self.assertEqual(reg["views"]["free_safe"]["endpoints"],
                         ["comfyui-shadow/wf:mesh_pbr", "comfyui-shadow/wf:video_base",
                          "ollama-shadow/esempio-assente:7b", "ollama-shadow/esempio-embed:0.6b",
                          GEMMA, "openrouter/esempio/beta-coder:free"])

    def test_benchmark_solo_con_corrispondenza_esatta(self):
        data = synthetic.demo_config_data()
        data["benchmarks"] = {"files": ["bench.json"]}
        with open(os.path.join(self.dir, "bench.json"), "w", encoding="utf-8") as f:
            json.dump({"kind": "braian_measurement", "source": "misura finta", "entries": [
                {"endpoint_key": "openrouter/esempio/gamma-mini", "model_version": "esempio/gamma-mini-20260101",
                 "metric": "esame_memoria", "value": 14, "date": "2026-11-11"},
                {"endpoint_key": "openrouter/esempio/alfa-large", "model_version": "un-altra-versione",
                 "metric": "esame_memoria", "value": 19, "date": "2026-11-11"}]}, f)
        config, _, _ = self.build(data=data)
        reg = read_json(paths(config)["registry"])
        self.assertEqual(reg["endpoints"]["openrouter/esempio/gamma-mini"]["benchmarks"][0]["value"], 14)
        self.assertEqual(reg["endpoints"]["openrouter/esempio/alfa-large"]["benchmarks"], [])


class PolicyECandidati(Base):
    def test_esclusione_per_policy_e_budget(self):
        config, _, _ = self.build()
        snap = self.snap(config, T0 + MIN)
        e = api.find_candidates(snap, {"capability": "editorial_writing", "data_class": "private_memory"})
        self.assertEqual([c["endpoint_key"] for c in e["candidates"]], [GEMMA])
        self.assertGreaterEqual(e["excluded_summary"]["data_class"], 3)
        e = api.find_candidates(snap, {"capability": "fast_general", "data_class": "public", "limit": 10,
                                       "require_enabled": False, "max_price": {"token_input": 0.000001}})
        dentro = [c["endpoint_key"] for c in e["candidates"]]
        self.assertEqual(dentro, [GEMMA, "openrouter/esempio/beta-coder:free", "openrouter/esempio/gamma-mini"])
        fuori = {x["endpoint_key"]: " ".join(x["reasons"]) for x in e["excluded"]}
        self.assertIn("oltre il tetto", fuori["openrouter/esempio/alfa-large"])
        self.assertIn("prezzo sconosciuto", fuori["openrouter/esempio/delta-router"])
        self.assertEqual(e["candidates"][0]["compute"], "private")      # privata e a costo nullo: prima

    def test_il_blocco_vale_anche_se_il_decisore_propone_altro(self):
        config, _, _ = self.build()
        snap = self.snap(config, T0 + MIN)
        via = api.check_execution_allowed(snap, "openrouter/esempio/alfa-large", data_class="public")
        self.assertFalse(via["allowed"])
        self.assertIn("risorsa non abilitata", via["blocking"])
        mini = "openrouter/esempio/gamma-mini"
        self.assertFalse(api.check_execution_allowed(snap, mini, data_class="private_memory")["allowed"])
        self.assertFalse(api.check_execution_allowed(snap, mini)["allowed"])   # classe omessa = la più delicata
        self.assertFalse(api.check_execution_allowed(snap, mini, data_class="public")["allowed"])  # manca la stima
        costo = {"amount": 0.9, "currency": "USD"}
        self.assertFalse(api.check_execution_allowed(snap, mini, "public", costo)["allowed"])
        costo = {"amount": 0.1, "currency": "USD"}
        self.assertTrue(api.check_execution_allowed(snap, mini, "public", costo)["allowed"])
        video = "fal/esempio/video-uno"
        via = api.check_execution_allowed(snap, video, "public", costo)
        self.assertFalse(via["allowed"])                                # serve prima l'utente
        self.assertEqual(len(via["pending_user_decisions"]), 1)
        via = api.check_execution_allowed(snap, video, "public", costo,
                                          user_approved=via["pending_user_decisions"])
        self.assertTrue(via["allowed"])
        self.assertFalse(api.check_execution_allowed(snap, "comfyui-shadow/wf:mesh_pbr", "public")["allowed"])
        self.assertFalse(api.check_execution_allowed(snap, "inesistente/x", "public")["allowed"])

    def test_nessun_candidato_compatibile(self):
        config, _, _ = self.build()
        snap = self.snap(config, T0 + MIN)
        e = api.find_candidates(snap, {"capability": "deep_reasoning"})
        self.assertEqual(e["outcome"], "no_compatible_candidates")
        self.assertEqual(e["candidates"], [])
        self.assertEqual(e["request"]["data_class"], "private_memory")  # assunta la più delicata
        self.assertTrue(any("NON sono stati allargati" in n for n in e["notes"]))
        self.assertEqual(e["considered"], 1)
        e = api.find_candidates(snap, {"capability": "categoria-inventata"})
        self.assertEqual(e["outcome"], "invalid_request")

    def test_nuovo_modello_scoperto_non_abilitato(self):
        config, _, _ = self.build()
        t = synthetic.routes()
        nuovo = copy.deepcopy(t["https://openrouter.ai/api/v1/models"]["data"][2])
        nuovo.update(id="esempio/nuovo-arrivo", canonical_slug="esempio/nuovo-arrivo")
        t["https://openrouter.ai/api/v1/models"]["data"].append(nuovo)
        config, _, _ = self.build(now=T0 + GIORNO, tabella=t)
        ep = read_json(paths(config)["registry"])["endpoints"]["openrouter/esempio/nuovo-arrivo"]
        self.assertFalse(ep["policy"]["enabled"])
        self.assertEqual(ep["policy"]["rules"], ["default"])


class Integrazione(Base):
    def test_cambio_modello_senza_toccare_identita_e_stanze(self):
        config, _, _ = self.build()
        spec = importlib.util.spec_from_file_location(
            "system_one_demo", os.path.join(RADICE, "examples", "system_one_demo.py"))
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["system_one_demo"] = modulo
        self.addCleanup(sys.modules.pop, "system_one_demo", None)
        spec.loader.exec_module(modulo)
        r = modulo.run_demo(config, verbose=False)
        self.assertTrue(r["model_changed"])
        self.assertNotEqual(r["before"]["legame"], r["after"]["legame"])
        self.assertEqual(r["before"]["identita"], r["after"]["identita"])
        self.assertEqual(r["before"]["stanza"], r["after"]["stanza"])
        self.assertNotIn("provider", json.dumps(r["after"]["stanza"]))
        self.assertEqual(r["queries_after_greeting"], 0)                # «Ciao Brian» non consulta Scout
        self.assertEqual(r["context"], "tommy")
        self.assertTrue(all(riga.startswith(("[SIMULATO]", "[SCOUT REALE]")) for riga in r["log"]))


class Rete(unittest.TestCase):
    """Limiti del client HTTP, provati su 127.0.0.1."""

    def test_timeout_e_porta_chiusa(self):
        muto = socket.socket()
        muto.bind(("127.0.0.1", 0))
        muto.listen(1)                                                  # accetta ma non risponde mai
        porta = muto.getsockname()[1]
        try:
            t0 = time.monotonic()
            with self.assertRaises(NetError) as cm:
                Client(connect_timeout=0.5, request_timeout=0.6).get_json(f"http://127.0.0.1:{porta}/api/tags")
            self.assertEqual(cm.exception.kind, "timeout")
            self.assertLess(time.monotonic() - t0, 3.0)
        finally:
            muto.close()
        with self.assertRaises(NetError) as cm:
            Client(connect_timeout=3.0, request_timeout=3.0).get_json(f"http://127.0.0.1:{porta}/api/tags")
        self.assertIn(cm.exception.kind, ("refused", "timeout"))
        with self.assertRaises(NetError) as cm:
            Client(deadline=time.monotonic() - 1).get_json("http://127.0.0.1:9/x")
        self.assertEqual(cm.exception.kind, "budget")
        solo_due = Client(max_requests=0)
        with self.assertRaises(NetError) as cm:
            solo_due.get_json("http://127.0.0.1:9/x")
        self.assertEqual(cm.exception.kind, "budget")

    def test_dimensione_massima_e_richieste_condizionali(self):
        visti = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                visti.append(self.headers.get("If-None-Match"))
                if self.path == "/grande":
                    corpo = b'{"x": "' + b"a" * 50000 + b'"}'
                elif self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                    return
                else:
                    corpo = b'{"ok": true}'
                self.send_response(200)
                self.send_header("ETag", '"v1"')
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with tempfile.TemporaryDirectory() as cache:
                with self.assertRaises(NetError) as cm:
                    Client(max_bytes=1000).get_json(base + "/grande")
                self.assertEqual(cm.exception.kind, "too_large")
                c = Client(cache_dir=cache)
                self.assertEqual(c.get_json(base + "/catalogo", conditional=True), {"ok": True})
                self.assertEqual(c.get_json(base + "/catalogo", conditional=True), {"ok": True})
                self.assertEqual(visti[-1], '"v1"')                     # la seconda è condizionale (304)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
