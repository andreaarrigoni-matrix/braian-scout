# Scout

Scout scopre e tiene aggiornato l'elenco delle **risorse** di .brAIan: modelli, servizi ed endpoint, privati ed esterni. Lo scrive in un registro (`models_registry.json`) che System One consulta e che il Loft potrà mostrare.

**Scout conosce le risorse. La memoria conosce il progetto. System One decide.**

Versione 0.1.0, prima versione. Verificata l'11 novembre 2026.

## Cosa fa e cosa non fa

Fa:

1. raccoglie metadati su modelli, servizi ed endpoint;
2. controlla in modo leggero se i servizi privati rispondono;
3. porta capacità, prezzi, vincoli e limiti in una forma unica;
4. tiene un registro validato e versionato;
5. propone a System One i candidati per una richiesta, con le ragioni e i dati mancanti;
6. espone uno stato sintetico per il Loft.

Non fa, per costruzione:

- non sceglie il modello per una richiesta (lo fa System One) e non sostituisce il regista;
- non crea stanze o agenti, non apre conversazioni, non avvia lavori;
- non legge la memoria personale e non manda contenuti dell'utente a nessuno;
- non esegue inferenze, generazioni o benchmark, non scarica e non carica modelli;
- non usa un modello per leggere i cataloghi: tutto è deterministico;
- non tocca credenziali, rete o servizi esistenti. Fa solo richieste `GET`.

## Prova in un minuto

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -r requirements.txt
```

```bash
.venv\Scripts\python -m scout demo
```

Su Linux e Mac il percorso è `.venv/bin/python`. `demo` usa solo **dati finti** (tutti i nomi cominciano con `esempio`), non apre nessuna connessione e scrive in `examples/demo_data/`.

L'unica dipendenza è PyYAML, per leggere `config.yaml`. Serve Python 3.10 o successivo.

## Struttura

```
scout/
  config.yaml                 la tua configurazione (resta fuori da git)
  config.example.yaml         configurazione d'esempio, commentata voce per voce
  .env.example                nomi delle variabili per le chiavi (nessun segreto)
  requirements.txt
  schema/models_registry.schema.json     schema del registro, versione 1.0.0
  scout/
    api.py                    contratto verso System One
    cli.py                    riga di comando
    config.py                 lettura della configurazione, percorsi, segreti, log
    net.py                    client HTTP in sola lettura con tutti i limiti
    update.py                 aggiornamento: ogni fonte per conto suo, pubblicazione atomica
    health.py                 controllo dei servizi privati e ricontrollo con cache
    classify.py               integrazioni manuali, categorie, benchmark, policy, vista free_safe
    query.py                  fotografia in RAM, candidati, stato, riepilogo
    registry.py, schema.py    validazione, lettura con recupero, scrittura atomica
    model.py, util.py         costruttori dei record, tempo, lock
    synthetic.py              dati finti per demo e test
    adapters/                 ollama, comfyui, generic, openrouter, fal, replicate
  examples/
    models_registry.example.json       registro d'esempio, dati finti
    system_one_demo.py                 come System One potrà usare Scout (simulato)
    regista_proposta.py                proposta di adattatore verso il regista
    manual_overrides.example.yaml      integrazioni manuali
    benchmarks.example.json            misure già fatte da importare
  tests/test_scout.py         26 test offline
  data/                       creato da Scout: registro, stato, cache, log
```

## Comandi

| Comando | Cosa fa |
|---|---|
| `python -m scout update` | Aggiorna cataloghi e inventari, poi pubblica il registro. È il comando da cron. |
| `python -m scout update --only fal` | Aggiorna solo le fonti indicate. Serve a provare un adattatore alla volta. |
| `python -m scout update --offline` | Nessuna rete. Riapplica configurazione, policy e integrazioni manuali ai dati già presenti. |
| `python -m scout health` | Controllo leggero dei servizi privati. Con `--deep` verifica anche le dipendenze dei workflow. |
| `python -m scout status` | Stato sintetico. Con `--json` è ciò che leggerà il Loft. |
| `python -m scout candidates --capability coding --data-class public` | Candidati per una capacità. |
| `python -m scout show CHIAVE` | Stato completo di una risorsa. |
| `python -m scout recheck CHIAVE` | Ricontrollo da fare subito prima dell'esecuzione. |
| `python -m scout validate [FILE]` | Valida un registro contro lo schema. |
| `python -m scout demo` | Prova completa con dati finti. |

`--config percorso` (prima del comando) indica un'altra configurazione; in alternativa la variabile `SCOUT_CONFIG`. `--demo` (prima del comando) fa lavorare qualunque comando sui dati finti.

Codici di uscita: `0` ok, `1` errore o registro non pubblicato, `3` un'altra esecuzione è in corso.

## Contratto con System One

Tutto passa da `scout/api.py`:

```python
from scout import api

config = api.load_config("config.yaml")
snap   = api.load_snapshot(config)            # ultima fotografia valida, in RAM

esito  = api.find_candidates(snap, {
    "capability": "editorial_writing",
    "data_class": "filtered_context",
})
stato  = api.resource_status(snap, "ollama-shadow/gemma4-claude")
fresco = api.recheck(config, "ollama-shadow/gemma4-claude")
quadro = api.summary(snap)
via    = api.check_execution_allowed(snap, chiave, data_class="private_memory",
                                     estimated_cost={"amount": 0.02, "currency": "USD"})
```

| Operazione | Funzione |
|---|---|
| Lettura dell'ultima fotografia valida | `load_snapshot` |
| Ricerca dei candidati per capacità e vincoli | `find_candidates` |
| Stato di una risorsa | `resource_status` |
| Controllo leggero aggiornato su richiesta | `recheck` |
| Riepilogo dello stato di Scout | `summary` |
| Blocco in esecuzione | `check_execution_allowed` |

`load_snapshot` si chiama una volta e si tiene in RAM. Un saluto nel Loft non richiede nessuna chiamata a Scout: System One usa il modello che ha già.

### La richiesta di candidati

| Campo | Significato |
|---|---|
| `capability` | Categoria richiesta. È l'unico campo obbligatorio. |
| `data_class` | Classe dei dati da inviare. **Se manca vale la più delicata.** |
| `evidence` | `any` (predefinito), `declared`, `verified`. |
| `compute` | `private` oppure `external`. |
| `trust_domains` | Domini di fiducia ammessi. |
| `input_modalities`, `output_modalities`, `output_formats`, `required_parameters`, `min_context_tokens` | Requisiti tecnici. |
| `needs` | Dettagli concreti, per esempio `{"pbr_textures": true}`. |
| `zero_api_cost` | Solo costo API nullo **noto**. |
| `max_price` | Tetti per unità, per esempio `{"token_input": 0.000003}`. |
| `availability` | `not_missing` (predefinito), `any`, `present`. |
| `require_enabled` | `true` di norma. Con `false` compaiono anche le risorse non abilitate, segnalate. |
| `allow_stale` | Ammette dati obsoleti. Di norma no. |
| `allow_unknown` | Campi che possono restare sconosciuti: `context_tokens`, `price`, `input_modalities`, `output_modalities`, `output_formats`, `parameters`, `needs`. |
| `sort`, `limit` | Ordinamento e numero di candidati. |

Regola di fondo: **un dato sconosciuto non soddisfa mai un vincolo**, a meno che la richiesta non lo ammetta con `allow_unknown`.

### La risposta

- `outcome`: `ok`, `no_compatible_candidates` oppure `invalid_request`.
- `candidates`: ognuno con `reasons` (perché è dentro), `missing_data` (cosa non si sa), `requires_user_decision`, disponibilità, prezzi nella loro unità, evidenza della capacità.
- `excluded` ed `excluded_summary`: chi è rimasto fuori e perché.
- `snapshot`: età della fotografia.

Se non c'è nessun candidato compatibile, Scout lo dice e si ferma lì. **Non allarga da solo privacy, budget o autorizzazioni.**

### Blocco in esecuzione

`check_execution_allowed` va chiamato da chi esegue, qualunque cosa abbia proposto il decisore. Blocca se la risorsa non è abilitata, se la classe di dati non è consentita, se manca una dipendenza, se serve una decisione dell'utente non ancora data, o se c'è un limite di spesa e la stima manca o lo supera.

## Il registro

`data/models_registry.json`, schema `1.0.0` in `schema/models_registry.schema.json`. Un valore `null` vuol dire «sconosciuto». Non vuol dire zero, gratis o senza limiti.

Tre sezioni:

- `sources`: per ogni fonte, ultimo tentativo e ultima raccolta **riuscita**, tenuti separati.
- `models`: il modello in sé (nome, versione, licenza e fonte della licenza, pesi aperti sì/no/sconosciuto).
- `endpoints`: chi eroga quel modello. Lo stesso modello può avere più endpoint con prezzi e condizioni diversi.

Campi principali di un endpoint:

| Campo | Contenuto |
|---|---|
| `location` | `compute` (private/external), `node`, `trust_domain`, `hosting`. Shadow è `rented_remote`: un PC remoto nel cloud non è hardware posseduto. |
| `io`, `limits`, `supported_parameters`, `requirements`, `details` | Modalità, formati, contesto, limiti, workflow e dipendenze, dettagli concreti (formati 3D, texture PBR...). |
| `capabilities` | `declared`, `verified`, `heuristic`: tre elenchi separati. |
| `pricing` | `known` più le voci, ognuna con importo, valuta, unità, condizioni, natura (`list_price`, `estimate`, `manual`), fonte e data. |
| `private_costs` | Per le risorse private: costo API marginale e costi dell'infrastruttura, separati. |
| `data_policy` | Conservazione e uso dei dati, solo se documentati, con la fonte. |
| `access` | A catalogo, credenziali configurate, quota, eseguibilità (sempre `unverified` in questa versione). |
| `policy` | Abilitazione, classi di dati consentite, contesto filtrato, limite di spesa, decisioni che spettano all'utente. |
| `benchmarks` | Misure importate, con metrica, condizioni, data, fonte e tipo. |
| `provenance` | Fonte, data di raccolta, tipo di verifica (`catalog`, `service_inventory`, `config`). |

Il registro **non contiene** indirizzi di rete, chiavi, identità degli agenti, conversazioni o stato delle .Jobs.

Le descrizioni dei cataloghi finiscono in `description_untrusted`: sono testo non fidato, ripulito e accorciato, mai interpretato.

### Capacità: quattro cose distinte

| | Da dove viene |
|---|---|
| Dichiarata | Dal catalogo (categoria del provider) o da te in configurazione. |
| Verificata | Solo con una prova registrata a mano, con l'evidenza. |
| Euristica | Da una regola esplicita in `classification.heuristic_rules`. Segnata come tale. |
| Abilitazione all'uso | Dalla policy. Nessuna capacità abilita niente. |

Categorie: `fast_general`, `deep_reasoning`, `coding`, `editorial_writing`, `3d_modeling_pro.script_and_math`, `3d_modeling_pro.asset_generation`, `video_production.direction_and_ffmpeg`, `video_production.video_synthesis`. Altre si aggiungono in `categories`.

`free_safe` è una vista derivata: risorse abilitate, con costo API nullo noto e con la classe di dati della vista consentita. È un filtro, non una certificazione di sicurezza.

### Prezzi

Ogni prezzo resta nella sua unità: token in ingresso, token in uscita, richiesta, immagine, video, secondo. Scout non converte e non somma unità diverse. Un tetto espresso in un'unità non si applica a un prezzo pubblicato in un'altra: la risorsa esce con il motivo «unità non confrontabili». Un prezzo sconosciuto resta sconosciuto e non passa mai per gratuito.

«Presente a catalogo» non vuol dire «posso usarla»: catalogo, credenziali configurate, quota ed eseguibilità sono campi separati. Un abbonamento a un'app non è prova di accesso API.

## Disponibilità dei servizi privati

Lo stato dei servizi sta in un file suo, `data/health_state.json`, perché cambia più spesso dei cataloghi.

Tempi del controllo leggero (`health` in `config.yaml`):

| Voce | Valore | Significato |
|---|---|---|
| `connect_timeout` | 1 s | Tempo per aprire la connessione. |
| `request_timeout` | 2 s | Limite complessivo della singola richiesta. |
| `check_budget_seconds` | 2 s | Limite complessivo del controllo di un servizio. |
| `validity_seconds` | 300 s | Per quanto vale un esito. Poi torna `unknown`. |
| `recheck_cache_seconds` | 20 s | Cache del ricontrollo prima dell'esecuzione. |

Stati di una risorsa privata:

| Stato | Significato |
|---|---|
| `present` | Il servizio risponde e l'inventario conferma il modello installato, o le dipendenze del workflow. L'esecuzione non è provata. |
| `reachable_unverified` | Il servizio risponde, ma inventario o dipendenze non sono verificati. |
| `missing` | Il servizio risponde, ma il modello non è installato o al workflow manca una dipendenza. |
| `timeout` | Nessuna risposta entro il limite. **Non dimostra che la macchina sia spenta.** |
| `refused` | La macchina risponde ma la porta del servizio è chiusa. |
| `error` | Altro errore di rete. |
| `unknown` | Mai controllato, o controllo scaduto. |
| `not_checked` | Risorsa esterna: Scout non la interroga. |

Per Ollama c'è anche `resident`: vero se il modello è in memoria (`/api/ps`), `null` se non rilevabile.

Una porta aperta non certifica che un lavoro andrà a buon fine. ComfyUI acceso non dà nessuna capacità: valgono solo i workflow elencati in configurazione, e solo se le loro dipendenze risultano presenti.

## Policy e confini della memoria

Scout descrive i vincoli degli endpoint. Non fa il lavoro del Memory Gatekeeper né del filtro di epurazione.

- Tutto ciò che viene scoperto parte **non abilitato**. Si abilita con una regola in `policy.rules`.
- Gratuito, a pesi aperti o «zero retention» non danno il permesso di ricevere memoria privata. Lo dà solo `allowed_data_classes`.
- Classi di dati, dalla meno alla più delicata: `public`, `filtered_context`, `private_memory`.
- `requires_filtered_context`, `spend_limit` e `requires_user_decision` viaggiano con ogni candidato.

## Integrazioni manuali e benchmark

Quello che un catalogo non espone si aggiunge a mano in un file a parte (`examples/manual_overrides.example.yaml`). Ogni voce deve avere fonte e data, altrimenti viene scartata. Nel registro resta marcata come manuale; tolta la voce, al prossimo aggiornamento il dato torna sconosciuto.

I benchmark si importano solo da file locali con misure già fatte (`examples/benchmarks.example.json`), di tre tipi: `public`, `provider_claim`, `braian_measurement`. Si attaccano solo per corrispondenza esatta di endpoint o di modello e versione. Una misura non passa a varianti o quantizzazioni diverse. I voti dell'esame della memoria possono entrare da qui.

## Aggiornamento e cron

Due frequenze separate:

- cataloghi e prezzi: una volta al giorno, con `update`;
- disponibilità dei servizi: su richiesta (`recheck`) o a intervalli (`health`).

Non c'è nessun servizio permanente. Esempio di cron su Linux, con fuso orario e percorsi espliciti (da adattare):

```
CRON_TZ=Europe/Amsterdam
SCOUT_CONFIG=/opt/braian/scout/config.yaml
# cataloghi e inventari: ogni giorno alle 06:15
15 6 * * *    cd /opt/braian/scout && .venv/bin/python -m scout update >> data/logs/cron.log 2>&1
# servizi privati: ogni 10 minuti (facoltativo)
*/10 * * * *  cd /opt/braian/scout && .venv/bin/python -m scout health >> data/logs/cron.log 2>&1
```

Questa consegna **non attiva nessun cron**.

Per il Loft: `python -m scout status --json`. Contiene ultima esecuzione, fonti fresche, fonti obsolete, risorse verificate ed errori, ognuno con la sua età. È una fotografia, non telemetria in tempo reale.

## Resilienza e limiti

- Ogni fonte si aggiorna per conto suo. Se una fallisce, restano i suoi ultimi dati validi con la loro data, il tentativo fallito è registrato a parte e le altre proseguono.
- Passata la soglia (`freshness`), i dati di una fonte ferma sono marcati obsoleti e non entrano nei candidati, salvo richiesta esplicita.
- Primo avvio senza rete o senza chiavi: il registro nasce lo stesso, con ciò che la configurazione dichiara per i servizi privati.
- Il registro viene validato prima di essere pubblicato. La scrittura passa da un file temporaneo nella stessa cartella e da una sostituzione atomica. La copia valida precedente resta in `models_registry.json.bak` e viene usata se il file corrente è rotto.
- Un lock su file impedisce due aggiornamenti insieme. Un lock abbandonato da più di 15 minuti viene rimosso.
- Limiti configurabili in `limits`: durata totale, richieste per fonte, pagine, concorrenza, retry con attesa crescente (rispetta `Retry-After`), dimensione delle risposte, dimensione e rotazione dei log.
- Richieste condizionali (`ETag`, `Last-Modified`) per il catalogo di OpenRouter, se il server manda quelle intestazioni.
- Le chiavi si leggono dall'ambiente o da `.env`. Non finiscono nel registro, nei log o nei messaggi d'errore.

## Provare un adattatore alla volta

```bash
.venv\Scripts\python -m scout update --only ollama-shadow
```

```bash
.venv\Scripts\python -m scout update --only openrouter
```

```bash
.venv\Scripts\python -m scout update --only fal
```

```bash
.venv\Scripts\python -m scout update --only replicate
```

```bash
.venv\Scripts\python -m scout status
```

Le fonti non nominate restano come sono.

## Fonti ufficiali e limiti reali degli adattatori

Documentazione letta l'**11 novembre 2026**.

**Ollama.** Fonti: `https://docs.ollama.com/api`, `https://docs.ollama.com/api/tags`, `https://docs.ollama.com/api/ps`, `https://github.com/ollama/ollama/blob/main/docs/api.md`. Usa `GET /api/tags`, `GET /api/ps`, `GET /api/version`. Limiti: non chiama `/api/show`, perché la documentazione non dice se carica il modello; quindi il contesto massimo è noto solo per i modelli in memoria, e le modalità (testo, immagini, embedding) solo se le dichiari in `expected`.

**ComfyUI.** Fonti: `https://docs.comfy.org/development/comfyui-server/comms_routes`, `https://docs.comfy.org/development/comfyui-server/comms_overview`. Usa `GET /system_stats`, `GET /object_info/{nodo}`, `GET /models/{cartella}`. Non chiama mai `POST /prompt`. Limiti: la forma JSON delle risposte non è documentata; se una risposta non ha la forma attesa la dipendenza resta `unverified`. Non provato su un ComfyUI vero.

**OpenRouter.** Fonti: `https://openrouter.ai/docs/api-reference/models/get-models`, `https://openrouter.ai/docs/api/api-reference/endpoints/list-all-endpoints-for-a-model`, `https://openrouter.ai/docs/guides/overview/models`, `https://openrouter.ai/docs/api-reference/limits`. Usa `GET /api/v1/models` e, a richiesta, `GET /api/v1/models/{autore}/{slug}/endpoints`. Limiti: la documentazione non è univoca sull'obbligo della chiave per il catalogo; la licenza del modello, la conservazione dei dati per endpoint e i limiti di frequenza del catalogo non sono esposti e restano sconosciuti; i prezzi condizionali (`overrides`) sono solo segnalati; il campo `benchmarks` del catalogo non è importato.

**fal.ai.** Fonti: `https://fal.ai/docs/platform-apis/v1/models`, `https://fal.ai/docs/platform-apis/v1/models/pricing` (`docs.fal.ai` rimanda lì). Usa `GET https://api.fal.ai/v1/models` e `GET https://api.fal.ai/v1/models/pricing`. Limiti: i prezzi richiedono la chiave; l'elenco completo delle categorie non è documentato (`text-to-3d` non compare); formati di uscita, texture PBR, limiti di frequenza numerici e conservazione dei dati non sono esposti.

**Replicate.** Fonti: `https://replicate.com/docs/reference/http`, `https://replicate.com/docs/topics/predictions/rate-limits`, `https://replicate.com/docs/topics/predictions/data-retention`, `https://replicate.com/collections`. Usa `GET /v1/collections/{slug}` e `GET /v1/models/{proprietario}/{nome}`. Limiti: **nessun campo dell'API espone i prezzi**, che restano sconosciuti finché non li inserisci a mano; il token è sempre obbligatorio; della licenza c'è solo l'indirizzo; non esiste una collezione dedicata alle texture.

## Test

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

26 test, solo libreria standard, nessuna rete esterna. Coprono: Shadow raggiungibile e non verificabile; ComfyUI attivo con dipendenze mancanti e con workflow non configurato; un provider che fallisce mentre gli altri si aggiornano; prezzo mancante che resta sconosciuto; unità di prezzo non confrontabili; dati obsoleti non promossi a freschi; registro valido conservato dopo un errore e recupero dalla copia; esecuzioni sovrapposte bloccate; esclusione per policy e budget; nessun candidato compatibile; nuovo modello non abilitato; testo ostile nel catalogo senza effetto; cambio di modello senza toccare identità e stanze; limiti del client HTTP.

## Cosa è simulato

- `demo`, i test e `examples/system_one_demo.py` usano risposte finte che imitano la forma delle API documentate.
- System One, le stanze, gli agenti e le attività nell'esempio sono finti. Ogni riga dell'esempio dice se è `[SIMULATO]` o `[SCOUT REALE]`.
- `examples/regista_proposta.py` stampa una proposta nel formato di `regista.json`. Non scrive niente e non è verificata sul regista in esecuzione.

## Assunzioni fatte

- Le capacità di `gemma4-claude` e `lfm-everest` in `config.yaml` sono una mia ipotesi: confermale o cambiale.
- La regola che abilita i due Ollama alla memoria privata ricalca il `privato: true` già presente in `regista.json`.
- Le quattro regole euristiche sono un punto di partenza, non un giudizio di qualità.
- Il costo mensile di Shadow è lasciato vuoto.
- Scout può girare su Shadow, su Everest o sulla Numero Uno: cambia solo `nodes` in `config.yaml`. Non fa migrazioni né sincronizzazioni.
