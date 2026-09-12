"""OFAC SDN (Specially Designated Nationals) sanctions list ingestion.

The SDN list is a reference lookup, not an event stream, so it gets no Django
model — it lands in data/ofac_sdn_cache.json (git-ignored, refreshed weekly) and
is read back by the extraction layer to flag sanctioned actors.

The published CSV is headerless with a fixed column order:
    ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type,
    Tonnage, GRT, Vess_flag, Vess_owner, Remarks
Null cells are the literal string "-0-".
"""
import csv
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OFAC_CACHE_PATH = Path(settings.BASE_DIR) / "data" / "ofac_sdn_cache.json"

# Not a secret and doesn't vary by environment.
OFAC_SDN_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/download/sdn.csv"

_NULL = "-0-"
_COL_ENT_NUM = 0
_COL_NAME = 1
_COL_TYPE = 2
_COL_PROGRAM = 3


def _clean(value):
    value = (value or "").strip()
    return "" if value == _NULL else value


def download_ofac_sdn(timeout=30):
    """Download and parse the SDN CSV, write the cache, return the entity list.

    Never raises — logs and returns [] on any failure.
    """
    try:
        response = requests.get(OFAC_SDN_CSV_URL, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("OFAC SDN download failed: %s", exc)
        return []

    entities = []
    for row in csv.reader(io.StringIO(response.text)):
        if len(row) <= _COL_PROGRAM:
            logger.debug("skipping malformed SDN row: %r", row)
            continue
        name = _clean(row[_COL_NAME])
        if not name:
            continue
        entities.append(
            {
                "ent_num": _clean(row[_COL_ENT_NUM]),
                "name": name,
                "entity_type": _clean(row[_COL_TYPE]),
                "program": _clean(row[_COL_PROGRAM]),
            }
        )

    logger.info("parsed %d OFAC SDN entities", len(entities))
    save_ofac_cache(entities)
    return entities


def save_ofac_cache(entities):
    """Write the entity list plus a fetch timestamp to OFAC_CACHE_PATH."""
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "entities": entities,
    }
    OFAC_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OFAC_CACHE_PATH.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, OFAC_CACHE_PATH)
    except OSError as exc:
        logger.warning("could not write OFAC cache to %s: %s", OFAC_CACHE_PATH, exc)


def load_ofac_cache():
    """Return the cached payload, or None if it is missing or unreadable."""
    try:
        return json.loads(OFAC_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("OFAC cache unavailable at %s: %s", OFAC_CACHE_PATH, exc)
        return None


def is_sanctioned(name, cache=None):
    """Case-insensitive check of an actor name against the cached SDN list."""
    if not name:
        return False
    if cache is None:
        cache = load_ofac_cache()
    if not cache:
        return False
    needle = name.strip().lower()
    return any(needle in entity.get("name", "").lower() for entity in cache.get("entities", []))
