"""Client HTTP minimo, in sola lettura (GET), con limiti espliciti.

Due tempi distinti:
- connect_timeout: tempo massimo per aprire la connessione (TCP + TLS);
- request_timeout: limite complessivo della singola richiesta, connessione
  compresa. Viene controllato a ogni blocco letto, quindi una risposta lenta
  a gocce non può sforare di molto.

In più: tetto alla dimensione della risposta, numero massimo di richieste,
intervallo minimo fra richieste, retry con attesa crescente (rispettando
Retry-After) e una scadenza assoluta condivisa (`deadline`) per l'intera
esecuzione. Le intestazioni non vengono mai scritte nei log né negli errori:
è lì che stanno le chiavi.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


class NetError(Exception):
    """Errore di rete classificato.

    kind: timeout | refused | connect | http | too_large | invalid | budget
    """

    def __init__(self, kind: str, message: str, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


class Client:
    def __init__(self, connect_timeout: float = 2.0, request_timeout: float = 10.0,
                 max_bytes: int = 8_000_000, retries: int = 0, backoff: float = 1.0,
                 min_interval: float = 0.0, deadline: float | None = None,
                 max_requests: int | None = None, cache_dir: str | None = None,
                 user_agent: str = "scout-catalog/0.1"):
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.backoff = backoff
        self.min_interval = min_interval
        self.deadline = deadline          # istante time.monotonic() oltre cui fermarsi
        self.max_requests = max_requests
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.requests = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def remaining(self):
        return None if self.deadline is None else self.deadline - time.monotonic()

    # ------------------------------------------------------------------
    def get_json(self, url: str, headers: dict | None = None,
                 conditional: bool = False, raw: bool = False):
        """GET che ritorna il JSON decodificato (o None se raw=True).

        conditional=True usa ETag / Last-Modified, se il server li manda, per
        non riscaricare un catalogo invariato.
        """
        tentativo = 0
        while True:
            try:
                return self._get(url, headers or {}, conditional, raw)
            except NetError as e:
                ripetibile = e.kind in ("timeout", "connect") or (
                    e.kind == "http" and (e.status == 429 or (e.status or 0) >= 500))
                if not ripetibile or tentativo >= self.retries:
                    raise
                attesa = e.retry_after if e.retry_after is not None else self.backoff * (2 ** tentativo)
                resto = self.remaining()
                if resto is not None and attesa >= resto:
                    raise
                time.sleep(attesa)
                tentativo += 1

    # ------------------------------------------------------------------
    def _get(self, url: str, headers: dict, conditional: bool, raw: bool):
        with self._lock:
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise NetError("budget", "limite di richieste raggiunto")
            self.requests += 1
            pausa = self.min_interval - (time.monotonic() - self._last)
            if pausa > 0:
                time.sleep(pausa)
            self._last = time.monotonic()

        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise NetError("invalid", "URL non valido")
        inizio = time.monotonic()
        limite = inizio + self.request_timeout
        if self.deadline is not None:
            limite = min(limite, self.deadline)
        if limite - inizio <= 0:
            raise NetError("budget", "tempo esaurito prima della richiesta")

        hdrs = {"Accept": "application/json", "Accept-Encoding": "identity",
                "User-Agent": self.user_agent, "Connection": "close"}
        hdrs.update(headers)
        meta_path = body_path = None
        if conditional and self.cache_dir:
            chiave = hashlib.sha256(url.encode()).hexdigest()[:32]
            meta_path = os.path.join(self.cache_dir, chiave + ".meta.json")
            body_path = os.path.join(self.cache_dir, chiave + ".body")
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                if os.path.exists(body_path):
                    if meta.get("etag"):
                        hdrs["If-None-Match"] = meta["etag"]
                    if meta.get("last_modified"):
                        hdrs["If-Modified-Since"] = meta["last_modified"]
            except (OSError, ValueError):
                pass

        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=min(self.connect_timeout, limite - inizio))
        try:
            try:
                conn.connect()
            except (socket.timeout, TimeoutError):
                raise NetError("timeout", "connessione: nessuna risposta entro il limite")
            except ConnectionRefusedError:
                raise NetError("refused", "connessione rifiutata (porta chiusa)")
            except socket.gaierror:
                raise NetError("connect", "nome host non risolto")
            except (ssl.SSLError, OSError) as e:
                raise NetError("connect", f"connessione fallita ({type(e).__name__})")
            resto = limite - time.monotonic()
            if resto <= 0:
                raise NetError("timeout", "limite complessivo superato dopo la connessione")
            conn.sock.settimeout(resto)
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            try:
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                status = resp.status
                corpo = self._read(resp, limite)
                etag = resp.getheader("ETag")
                modificato = resp.getheader("Last-Modified")
                dopo = resp.getheader("Retry-After")
            except NetError:
                raise
            except (socket.timeout, TimeoutError):
                raise NetError("timeout", "risposta: limite complessivo superato")
            except (http.client.HTTPException, OSError) as e:
                raise NetError("connect", f"scambio interrotto ({type(e).__name__})")
        finally:
            conn.close()

        if status == 304 and body_path and os.path.exists(body_path):
            with open(body_path, "rb") as f:
                corpo = f.read()
        elif status != 200:
            attesa = float(dopo) if dopo and dopo.strip().isdigit() else None
            # Il corpo dell'errore è testo non fidato: non lo riportiamo.
            raise NetError("http", f"HTTP {status}", status=status, retry_after=attesa)
        elif meta_path and (etag or modificato):
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                with open(body_path, "wb") as f:
                    f.write(corpo)
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump({"etag": etag, "last_modified": modificato}, f)
            except OSError:
                pass
        if raw:
            return None
        try:
            return json.loads(corpo.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise NetError("invalid", "risposta non in JSON valido")

    def _read(self, resp, limite: float) -> bytes:
        dichiarata = resp.getheader("Content-Length")
        if dichiarata and dichiarata.isdigit() and int(dichiarata) > self.max_bytes:
            raise NetError("too_large", f"risposta oltre {self.max_bytes} byte")
        pezzi, totale = [], 0
        while True:
            if time.monotonic() > limite:
                raise NetError("timeout", "risposta: limite complessivo superato")
            pezzo = resp.read(65536)
            if not pezzo:
                break
            totale += len(pezzo)
            if totale > self.max_bytes:
                raise NetError("too_large", f"risposta oltre {self.max_bytes} byte")
            pezzi.append(pezzo)
        return b"".join(pezzi)
