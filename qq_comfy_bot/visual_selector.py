"""Reuse Anima Tools catalogs during visual planning, before camera selection."""
from functools import lru_cache
import json
import os
from pathlib import Path
import re

from prompt_spec import clauses, without_excluded

# Associations require a cue already in the authorized scene or action. Each
# mapping selects an exact catalog ID and admits only the stated vocabulary.
POSES = (
    (r'\b(?:rest(?:ing)?|lean(?:ing)?)\b[^,.;]{0,45}\bforearm\b',
     'pose_1792200000000', {'arm support'}),
    (r'\b(?:running|jogging|mid[- ]stride|mid[- ]run|running stride)\b',
     'pose_1792200000263', {'running'}),
    (r'\b(?:sitting|seated)\b', 'pose_1792200000227', {'sitting'}),
    (r'\blean(?:ing|s)?\b[^,.;]{0,18}\bforward\b',
     'pose_1792200000220', {'leaning forward'}),
    (r'\blean(?:ing|s)?\b[^,.;]{0,20}\bto (?:the|one) side\b',
     'pose_1792200000240', {'leaning to the side'}),
)
BACKGROUNDS = (
    (r'\bschool track\b', 'bg_1782248710716', {'school track', 'outdoors'}),
    (r'\b(?:athletic[s]? |running )track\b', 'bg_1782248710716', {'outdoors'}),
    (r'\bschool (?:corridor|hallway)\b',
     'bg_1782248768982', {'hallway', 'school building'}),
    (r'\bclassroom\b(?=.*\b(?:school )?desk\b)|\bdesk\b(?=.*\bclassroom\b)',
     'bg_1782250705764', {'classroom', 'desk'}),
    (r'\b(?:riverside|river bank|riverbank)\b',
     'bg_1782250129738', {'outdoors', 'water'}),
    (r'\b(?:indoors?|room|interior)\b(?=.*\bwindow\b)|\bwindow\b(?=.*\b(?:indoors?|room|interior)\b)',
     'bg_1782250420670', {'indoor', 'window'}),
)
CLOTHES = (
    (r'\bschool uniform\b|\bschool blazer\b',
     '1741126270296', {'school uniform'}),
    (r'\bblack baseball cap\b(?=.*\bblack sweater\b)|\bblack sweater\b(?=.*\bblack baseball cap\b)',
     '1741125546512', {'black baseball cap', 'black sweater'}),
)


@lru_cache(maxsize=12)
def _read_catalog(path, modified_ns):
    try:
        javascript = path.read_text(encoding='utf-8-sig')
        # The frontend data wraps a JSON array in a JS assignment. Decode the
        # array as data, preserving URLs containing //.
        _, _, payload = javascript.partition('=')
        rows, _ = json.JSONDecoder().raw_decode(payload.lstrip())
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(rows, list):
        return {}
    return {row['id']: row for row in rows
            if isinstance(row, dict) and isinstance(row.get('id'), str)
            and isinstance(row.get('name'), str) and isinstance(row.get('tags'), str)}


def _catalog_directory(root, locator=None):
    if locator:
        return Path(locator)
    if settings_path := os.environ.get('ANIMA_SELECTOR_CATALOG_DIR'):
        return Path(settings_path)
    try:
        config = json.loads((Path(root) / 'launcher.json').read_text(encoding='utf-8-sig'))
        comfy_dir = Path(config['comfy_cwd'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return comfy_dir / 'custom_nodes' / 'comfyui-anima-tools' / 'js'


def _select(directory, catalog, rules, current, exclusions):
    # A negated clause is never evidence for adding a positive catalog tag.
    positive = ', '.join(part for part in current.split(',')
                         if not re.search(r'\b(?:not|no|without|never)\b', part, re.I))
    match = next(((identifier, allowed) for pattern, identifier, allowed in rules
                  if re.search(pattern, positive, re.I)), None)
    if match is None:
        return None
    identifier, allowed = match
    path = directory / f'{catalog}_data.js'
    try:
        entry = _read_catalog(path, path.stat().st_mtime_ns).get(identifier)
    except OSError:
        return None
    if entry is None:
        return None
    tags = [tag.strip() for tag in clauses(entry['tags']).split(',')
            if tag.strip().casefold() in allowed]
    selected = without_excluded(', '.join(tags), exclusions)
    if not selected:
        return None
    return {'id': identifier, 'name': entry['name'], 'tags': selected}


def _merge(value, additional, first=False):
    existing = {part.strip().casefold() for part in value.split(',')}
    novel = ', '.join(part.strip() for part in additional.split(',')
                      if part.strip().casefold() not in existing)
    parts = (novel, value) if first else (value, novel)
    return ', '.join(part for part in parts if part)


def apply(root, snapshot, locator=None):
    """Enrich a frozen daily scene without updating character or session state."""
    if snapshot.get('style') != 'life':
        return
    directory = _catalog_directory(root, locator)
    if directory is None:
        return
    exclusions = snapshot.get('visual_exclusions', [])
    overrides = snapshot.get('visual_overrides', [])
    used = {}

    pose = _select(directory, 'pose', POSES, snapshot.get('action', ''), exclusions)
    if pose:
        snapshot['action'] = _merge(snapshot['action'], pose['tags'], first=True)
        used['pose'] = pose

    if 'setting' not in overrides:
        setting = snapshot.get('setting_tags', '')
        background = _select(directory, 'background', BACKGROUNDS, setting, exclusions)
        if background:
            # Existing location remains the anchor; lighting and time are
            # supplied by the frozen scene and prompt compiler.
            snapshot['setting_tags'] = _merge(setting, background['tags'])
            used['background'] = background

    if 'outfit' not in overrides:
        outfit = snapshot.get('outfit_tags', '')
        clothing = _select(directory, 'clothing', CLOTHES, outfit, exclusions)
        if clothing:
            snapshot['outfit_tags'] = _merge(outfit, clothing['tags'], first=True)
            used['clothing'] = clothing

    if used:
        snapshot['visual_selector'] = used
