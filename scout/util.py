"""Attrezzi piccoli: tempo, JSON su disco in modo atomico, lock su file."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone

FORMATO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FORMATO)


def parse_iso(testo):
    if not isinstance(testo, str):
        return None
    try:
        return datetime.strptime(testo, FORMATO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_seconds(testo, now: datetime):
    """Età in secondi di un timestamp ISO; None se il timestamp manca."""
    quando = parse_iso(testo)
    if quando is None:
        return None
    return max(0, int((now - quando).total_seconds()))


def deep_merge(base: dict, sopra: dict) -> dict:
    """Unisce due dizionari; i valori di `sopra` vincono, i sotto-dizionari si fondono."""
    out = dict(base)
    for k, v in (sopra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str, data) -> None:
    """Scrive su un file temporaneo nella stessa cartella e poi lo sostituisce.

    Chi legge trova sempre o il file vecchio intero o quello nuovo intero.
    """
    cartella = os.path.dirname(os.path.abspath(path))
    os.makedirs(cartella, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=cartella)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class LockBusy(Exception):
    """Un'altra esecuzione di Scout tiene il lock."""


@contextmanager
def file_lock(path: str, stale_seconds: float, wait_seconds: float = 0.0):
    """Lock esclusivo tramite creazione atomica di un file.

    Un lock più vecchio di `stale_seconds` è considerato abbandonato (processo
    morto) e viene rimosso. Con `wait_seconds` > 0 si aspetta un po' prima di
    rinunciare.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    scadenza = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                eta = time.time() - os.path.getmtime(path)
            except OSError:
                continue  # sparito nel frattempo: riprova
            if eta > stale_seconds:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= scadenza:
                raise LockBusy(f"lock occupato da {int(eta)} s: {path}")
            time.sleep(0.05)
    try:
        os.write(fd, f"{os.getpid()} {iso(utcnow())}\n".encode())
        os.close(fd)
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
