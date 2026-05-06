"""
processor.py — ALS processing logic, no UI dependencies.
All campus/routing config is driven by config.json.
"""

import gzip
import json
import os
import re
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
WARP_MODE_COMPLEX = 4
MASTER_ROUTE = {"target": "AudioOut/Master", "upper": "Master", "lower": ""}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# KEY / TRANSPOSITION
# ---------------------------------------------------------------------------

# Enharmonic flat → sharp mapping so "Bb", "Eb" etc. are recognised
ENHARMONIC = {
    "BB": "A#", "DB": "C#", "EB": "D#",
    "GB": "F#", "AB": "G#", "CB": "B",  "FB": "E",
}

def key_index(key_str):
    k = key_str.strip().upper()
    k = ENHARMONIC.get(k, k)
    try:
        return KEYS.index(k)
    except ValueError:
        return None

def semitone_delta(from_key, to_key):
    a, b = key_index(from_key), key_index(to_key)
    if a is None or b is None:
        return None
    diff = (b - a) % 12
    if diff > 6:
        diff -= 12
    return diff

def parse_song_name(raw_name):
    m = re.search(r'\(([^)]+)\)\s*$', raw_name)
    if m:
        return raw_name[:m.start()].strip(), m.group(1).strip()
    return raw_name, None

def format_song_name(base_name, key):
    return f"{base_name} ({key})"

# ---------------------------------------------------------------------------
# XML HELPERS
# ---------------------------------------------------------------------------

def get_name(track_elem):
    e = track_elem.find(".//Name/EffectiveName")
    return e.get("Value", "").strip() if e is not None else ""

def get_group_id(track_elem):
    g = track_elem.find("TrackGroupId")
    return int(g.get("Value", -1)) if g is not None else -1

def set_routing(track_elem, rule, warnings, track_name):
    dc  = track_elem.find("DeviceChain")
    aor = dc.find("AudioOutputRouting") if dc is not None else None
    if aor is None:
        warnings.append(f"No AudioOutputRouting for '{track_name}' — skipped.")
        return
    for xml_tag, key in [("Target", "target"),
                          ("UpperDisplayString", "upper"),
                          ("LowerDisplayString", "lower")]:
        el = aor.find(xml_tag)
        if el is not None:
            el.set("Value", rule[key])

def set_volume(track_elem, db_value, warnings, track_name):
    mixer  = track_elem.find(".//DeviceChain/Mixer")
    vol    = mixer.find("Volume") if mixer is not None else None
    manual = vol.find("Manual") if vol is not None else None
    if manual is None:
        warnings.append(f"No Volume/Manual for '{track_name}' — volume unchanged.")
        return
    manual.set("Value", f"{10 ** (db_value / 20.0):.10f}")

def set_mute(track_elem, warnings, track_name):
    mixer   = track_elem.find(".//DeviceChain/Mixer")
    speaker = mixer.find("Speaker") if mixer is not None else None
    manual  = speaker.find("Manual") if speaker is not None else None
    if manual is None:
        warnings.append(f"No Speaker/Manual for '{track_name}' — mute skipped.")
        return
    manual.set("Value", "false")

def unmute(track_elem, warnings, track_name):
    mixer   = track_elem.find(".//DeviceChain/Mixer")
    speaker = mixer.find("Speaker") if mixer is not None else None
    manual  = speaker.find("Manual") if speaker is not None else None
    if manual is None:
        warnings.append(f"No Speaker/Manual for '{track_name}' — unmute skipped.")
        return
    manual.set("Value", "true")

def reset_volume(track_elem, warnings, track_name):
    mixer  = track_elem.find(".//DeviceChain/Mixer")
    vol    = mixer.find("Volume") if mixer is not None else None
    manual = vol.find("Manual") if vol is not None else None
    if manual is None:
        warnings.append(f"No Volume/Manual for '{track_name}' — volume reset skipped.")
        return
    manual.set("Value", "1.0")

# ---------------------------------------------------------------------------
# TRACK INDEX
# ---------------------------------------------------------------------------

def build_track_index(tracks_elem):
    index, children_of = {}, {}
    for track_elem in tracks_elem:
        tid = int(track_elem.get("Id", -99))
        gid = get_group_id(track_elem)
        index[tid] = {
            "elem":     track_elem,
            "name":     get_name(track_elem),
            "group_id": gid,
            "tag":      track_elem.tag,
        }
        children_of.setdefault(gid, []).append(tid)
    return index, children_of

def get_descendants(pid, children_of):
    result = []
    for cid in children_of.get(pid, []):
        result.append(cid)
        result.extend(get_descendants(cid, children_of))
    return result

def identify_songs(index, children_of, cfg):
    ignored = {n.upper() for n in cfg.get("ignored_song_groups", [])}
    songs = []
    for tid, t in index.items():
        if t["group_id"] == -1 and t["tag"] == "GroupTrack":
            base_name, key = parse_song_name(t["name"])
            songs.append({
                "id":           tid,
                "raw_name":     t["name"],
                "base_name":    base_name,
                "key":          key,
                "ignored":      t["name"].upper() in ignored,
                "category_ids": children_of.get(tid, []),
            })
    return sorted(songs, key=lambda s: s["id"])

# ---------------------------------------------------------------------------
# MIXER ADJUSTMENTS
# ---------------------------------------------------------------------------

def apply_mixer_adjustments(cat_name, cat_elem, children_of, index,
                             warnings, adjustments_map):
    adjustments = next(
        (v for k, v in adjustments_map.items() if k.upper() == cat_name.upper()), None)
    if not adjustments:
        return
    cat_id = int(cat_elem.get("Id", -99))
    for adj in adjustments:
        match, action, db_val = adj["match"], adj["action"], adj.get("value", 0)
        targets, names = [], []
        if match == "__GROUP__":
            targets, names = [cat_elem], [cat_name]
        elif match == "__CHILDREN__":
            targets = [index[cid]["elem"] for cid in children_of.get(cat_id, [])]
            names   = [index[cid]["name"] for cid in children_of.get(cat_id, [])]
        else:
            for cid in children_of.get(cat_id, []):
                if index[cid]["name"].upper() == match.upper():
                    targets.append(index[cid]["elem"])
                    names.append(index[cid]["name"])
        for elem, name in zip(targets, names):
            if action == "volume":
                set_volume(elem, db_val, warnings, name)
            elif action == "mute":
                set_mute(elem, warnings, name)

# ---------------------------------------------------------------------------
# TRANSPOSITION
# ---------------------------------------------------------------------------

def transpose_song(song, new_key, index, children_of, warnings, cfg):
    current_key = song["key"]
    if current_key is None:
        warnings.append(f"'{song['raw_name']}' has no key in name — cannot transpose.")
        return
    delta = semitone_delta(current_key, new_key)
    if delta is None or delta == 0:
        return

    atonal = {n.upper() for n in cfg.get("atonal_categories", [])}
    atonal_ids = set()
    for cat_id in song["category_ids"]:
        if index[cat_id]["name"].upper() in atonal:
            for desc_id in get_descendants(cat_id, children_of):
                atonal_ids.add(desc_id)

    clip_count = 0
    for desc_id in get_descendants(song["id"], children_of):
        t = index[desc_id]
        if t["tag"] != "AudioTrack" or desc_id in atonal_ids:
            continue
        for clip in t["elem"].findall(".//AudioClip"):
            iw = clip.find("IsWarped")
            if iw is not None:
                iw.set("Value", "true")
            wm = clip.find("WarpMode")
            if wm is not None:
                wm.set("Value", str(WARP_MODE_COMPLEX))
            pitch = clip.find("PitchCoarse")
            if pitch is not None:
                pitch.set("Value", str(int(pitch.get("Value", "0")) + delta))
                clip_count += 1

    new_raw = format_song_name(song["base_name"], new_key)
    for attr in [".//Name/EffectiveName", ".//Name/UserName"]:
        el = index[song["id"]]["elem"].find(attr)
        if el is not None:
            el.set("Value", new_raw)

# ---------------------------------------------------------------------------
# UNFOLD ALL TRACKS
# ---------------------------------------------------------------------------

def unfold_all_tracks(index):
    for t in index.values():
        unfolded = t["elem"].find("TrackUnfolded")
        if unfolded is not None:
            unfolded.set("Value", "true")

# ---------------------------------------------------------------------------
# ROUTING HELPERS
# ---------------------------------------------------------------------------

def effective_category_rule(cat_name, campus_cfg, cfg):
    """
    Return the routing rule for a category, respecting campus-level overrides.
    Campus category_routing_overrides take priority over global category_routing.
    """
    overrides = campus_cfg.get("category_routing_overrides", {})
    rule = next((r for k, r in overrides.items() if k.upper() == cat_name.upper()), None)
    if rule:
        return rule
    return next((r for k, r in cfg["category_routing"].items()
                 if k.upper() == cat_name.upper()), None)

def effective_top_level_rules(campus_cfg, cfg):
    """
    Return top-level track routing for this campus.
    Campus-level top_level_tracks override the global ones.
    """
    campus_tl = campus_cfg.get("top_level_tracks")
    return campus_tl if campus_tl else cfg["top_level_tracks"]

# ---------------------------------------------------------------------------
# ROUTING PASSES
# ---------------------------------------------------------------------------

def route_standard(root, index, children_of, songs, campus_cfg,
                   cfg, transpose_map, warnings):
    """Apply campus routing, mixer adjustments, and transposition."""
    adjustments_map = campus_cfg.get("mixer_adjustments", {})
    top_level_rules = effective_top_level_rules(campus_cfg, cfg)

    # Top-level tracks (CLICK, GUIDE, CUES)
    for tid, t in index.items():
        if t["group_id"] == -1 and t["tag"] != "GroupTrack":
            rule = next((r for k, r in top_level_rules.items()
                         if k.upper() == t["name"].upper()), None)
            if rule:
                set_routing(t["elem"], rule, warnings, t["name"])
            else:
                warnings.append(f"Unrecognized top-level track '{t['name']}' — untouched.")

    for song in songs:
        if song["ignored"]:
            continue
        for cat_id in song["category_ids"]:
            cat      = index[cat_id]
            cat_name = cat["name"]

            rule = effective_category_rule(cat_name, campus_cfg, cfg)
            if rule is None:
                warnings.append(f"'{song['raw_name']}' → '{cat_name}' no routing rule — untouched.")
                continue

            # Route the category group track
            set_routing(cat["elem"], rule, warnings, cat_name)

            # Route children — but check if any individual child has its own override
            # (e.g. DRUMS inside PERC routed to 7/8 while PERC group goes to 9/10)
            for child_id in children_of.get(cat_id, []):
                child     = index[child_id]
                child_rule = effective_category_rule(child["name"], campus_cfg, cfg)
                set_routing(child["elem"], child_rule or rule, warnings, child["name"])

            apply_mixer_adjustments(cat_name, cat["elem"], children_of,
                                    index, warnings, adjustments_map)

        new_key = transpose_map.get(song["id"])
        if new_key and new_key != song["key"]:
            transpose_song(song, new_key, index, children_of, warnings, cfg)

def route_practice(root, index, children_of, songs, transpose_map, warnings, cfg):
    """Transpose, route everything to Master, reset volumes, unmute, expand tracks."""
    for song in songs:
        if song["ignored"]:
            continue
        new_key = transpose_map.get(song["id"])
        if new_key and new_key != song["key"]:
            transpose_song(song, new_key, index, children_of, warnings, cfg)

    for tid, t in index.items():
        if t["group_id"] == -1 and t["tag"] != "GroupTrack":
            set_routing(t["elem"], MASTER_ROUTE, warnings, t["name"])
            reset_volume(t["elem"], warnings, t["name"])
            unmute(t["elem"], warnings, t["name"])

    for song in songs:
        if song["ignored"]:
            continue
        for cat_id in song["category_ids"]:
            cat = index[cat_id]
            set_routing(cat["elem"], MASTER_ROUTE, warnings, cat["name"])
            reset_volume(cat["elem"], warnings, cat["name"])
            unmute(cat["elem"], warnings, cat["name"])
            for child_id in children_of.get(cat_id, []):
                child = index[child_id]
                set_routing(child["elem"], MASTER_ROUTE, warnings, child["name"])
                reset_volume(child["elem"], warnings, child["name"])
                unmute(child["elem"], warnings, child["name"])

    unfold_all_tracks(index)

# ---------------------------------------------------------------------------
# MAIN PROCESSING FUNCTION
# ---------------------------------------------------------------------------

def process_als(als_bytes, campus_key, transpose_map, cfg, practice=False):
    """
    Process an ALS file (as bytes).
    Returns (output_bytes, warnings).
    """
    warnings = []
    root = ET.fromstring(gzip.decompress(als_bytes))
    tracks_elem = root.find(".//Tracks")
    if tracks_elem is None:
        raise ValueError("No <Tracks> element found.")

    index, children_of = build_track_index(tracks_elem)
    songs = identify_songs(index, children_of, cfg)
    campus_cfg = cfg["campuses"][campus_key]

    if practice:
        route_practice(root, index, children_of, songs,
                       transpose_map, warnings, cfg)
    else:
        route_standard(root, index, children_of, songs,
                       campus_cfg, cfg, transpose_map, warnings)

    # Prepend the exact XML declaration Ableton expects.
    # ET.tostring with xml_declaration=True uses single quotes which breaks Ableton.
    xml_body = ET.tostring(root, encoding="unicode")
    xml_decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
    out_bytes = gzip.compress((xml_decl + xml_body).encode("utf-8"))
    return out_bytes, warnings

def scan_songs(als_bytes, cfg):
    root = ET.fromstring(gzip.decompress(als_bytes))
    tracks_elem = root.find(".//Tracks")
    if tracks_elem is None:
        return []
    index, children_of = build_track_index(tracks_elem)
    return identify_songs(index, children_of, cfg)

def scan_unknown_categories(als_bytes, cfg):
    """Return category names that have no rule in global OR any campus override."""
    root = ET.fromstring(gzip.decompress(als_bytes))
    tracks_elem = root.find(".//Tracks")
    if tracks_elem is None:
        return set()
    index, children_of = build_track_index(tracks_elem)
    songs = identify_songs(index, children_of, cfg)

    # Collect all known category names across global + all campus overrides
    known = {k.upper() for k in cfg["category_routing"]}
    for campus in cfg["campuses"].values():
        known.update(k.upper() for k in campus.get("category_routing_overrides", {}))

    unknowns = set()
    for song in songs:
        if song["ignored"]:
            continue
        for cat_id in song["category_ids"]:
            name = index[cat_id]["name"]
            if name.upper() not in known:
                unknowns.add(name)
    return unknowns
