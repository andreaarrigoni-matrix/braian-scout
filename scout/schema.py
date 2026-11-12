"""Validatore minimo di JSON Schema, senza dipendenze.

Copre solo le parole chiave usate da schema/models_registry.schema.json:
type, enum, required, properties, additionalProperties, items, minimum, $ref
(solo riferimenti interni "#/$defs/..."). Così lo schema su file resta
l'unica fonte di verità.
"""
from __future__ import annotations


def _is(valore, tipo: str) -> bool:
    if tipo == "null":
        return valore is None
    if tipo == "boolean":
        return isinstance(valore, bool)
    if tipo == "integer":
        return isinstance(valore, int) and not isinstance(valore, bool)
    if tipo == "number":
        return isinstance(valore, (int, float)) and not isinstance(valore, bool)
    if tipo == "string":
        return isinstance(valore, str)
    if tipo == "array":
        return isinstance(valore, list)
    if tipo == "object":
        return isinstance(valore, dict)
    return False


def check(valore, schema: dict, root: dict | None = None, path: str = "$",
          errors: list | None = None, limit: int = 40) -> list:
    """Ritorna la lista degli errori (vuota se il valore rispetta lo schema)."""
    root = root or schema
    errors = [] if errors is None else errors
    if len(errors) >= limit:
        return errors
    ref = schema.get("$ref")
    if ref:
        bersaglio = root
        for parte in ref.lstrip("#/").split("/"):
            bersaglio = bersaglio[parte]
        return check(valore, bersaglio, root, path, errors, limit)
    tipo = schema.get("type")
    if tipo is not None:
        tipi = tipo if isinstance(tipo, list) else [tipo]
        if not any(_is(valore, t) for t in tipi):
            errors.append(f"{path}: tipo atteso {'/'.join(tipi)}")
            return errors
    if "enum" in schema and valore not in schema["enum"]:
        errors.append(f"{path}: valore non ammesso ({valore!r})")
    if "minimum" in schema and _is(valore, "number") and valore < schema["minimum"]:
        errors.append(f"{path}: sotto il minimo {schema['minimum']}")
    if isinstance(valore, dict):
        for k in schema.get("required", []):
            if k not in valore:
                errors.append(f"{path}: manca '{k}'")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for k, v in valore.items():
            if k in props:
                check(v, props[k], root, f"{path}.{k}", errors, limit)
            elif isinstance(extra, dict):
                check(v, extra, root, f"{path}.{k}", errors, limit)
            elif extra is False:
                errors.append(f"{path}: chiave non prevista '{k}'")
    if isinstance(valore, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(valore):
            check(v, schema["items"], root, f"{path}[{i}]", errors, limit)
    return errors
