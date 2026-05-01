import gzip
import json
import os
import re
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import filedialog, messagebox

# ---------------------------------------------------------------------------
# ROUTING LIBRARY
# ---------------------------------------------------------------------------

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
LIBRARY_PATH = os.path.join(SCRIPT_DIR, "routing_library.json")

DEFAULT_CATEGORY_RULES = {
    "SYNTH BASS": {"target": "AudioOut/External/M1", "upper": "Ext. Out", "lower": "2"},
    "BASS":       {"target": "AudioOut/External/M1", "upper": "Ext. Out", "lower": "2"},
    "HOOKS":      {"target": "AudioOut/External/S1", "upper": "Ext. Out", "lower": "3/4"},
    "BGV":        {"target": "AudioOut/External/S2", "upper": "Ext. Out", "lower": "5/6"},
    "KEYS":       {"target": "AudioOut/External/S2", "upper": "Ext. Out", "lower": "5/6"},
    "STRINGS":    {"target": "AudioOut/External/S2", "upper": "Ext. Out", "lower": "5/6"},
    "GUITARS":    {"target": "AudioOut/External/S2", "upper": "Ext. Out", "lower": "5/6"},
    "PERC":       {"target": "AudioOut/External/S3", "upper": "Ext. Out", "lower": "7/8"},
    "PERCUSSION": {"target": "AudioOut/External/S3", "upper": "Ext. Out", "lower": "7/8"},
}

OUTPUT_OPTIONS = {
    "1 (mono)":               {"target": "AudioOut/External/M0", "upper": "Ext. Out", "lower": "1"},
    "2 (mono)":               {"target": "AudioOut/External/M1", "upper": "Ext. Out", "lower": "2"},
    "3/4 (stereo)":           {"target": "AudioOut/External/S1", "upper": "Ext. Out", "lower": "3/4"},
    "5/6 (stereo)":           {"target": "AudioOut/External/S2", "upper": "Ext. Out", "lower": "5/6"},
    "7/8 (stereo)":           {"target": "AudioOut/External/S3", "upper": "Ext. Out", "lower": "7/8"},
    "Skip (leave untouched)": None,
}

# Tracks at the top level (outside any song group) that get routed to ext outputs
TOP_LEVEL_TRACK_RULES = {
    "CLICK": {"target": "AudioOut/External/M0", "upper": "Ext. Out", "lower": "1"},
    "GUIDE": {"target": "AudioOut/External/M0", "upper": "Ext. Out", "lower": "1"},
    "CUES":  {"target": "AudioOut/External/M0", "upper": "Ext. Out", "lower": "1"},
}

# Top-level track names that are skipped in the Practice file (not instrument tracks)
PRACTICE_SKIP_TOP_LEVEL = {"CLICK", "GUIDE", "CUES", "SMPTE", "MARKERS"}

# Song-level group names to ignore entirely
IGNORED_SONG_GROUPS = {"MIDI"}

# Categories whose tracks are never pitch-shifted (atonal)
ATONAL_CATEGORIES = {"PERC", "PERCUSSION"}

# Routing used for the Practice file — all instrument tracks go straight to master
MASTER_ROUTE = {"target": "AudioOut/Master", "upper": "Master", "lower": ""}

# ---------------------------------------------------------------------------
# MIXER ADJUSTMENTS
# ---------------------------------------------------------------------------
# match="__GROUP__"     → the category GroupTrack itself
# match="__CHILDREN__"  → every track inside the category
# match="TRACK NAME"   → only that specific named track
# action: "volume" (value in dB) | "mute"

MIXER_ADJUSTMENTS_ENG = {
    "BGV": [
        {"match": "__GROUP__",    "action": "volume", "value": -10},
        {"match": "__CHILDREN__", "action": "volume", "value": -10},
    ],
    "PERC": [
        {"match": "DRUMS", "action": "mute"},
    ],
    "PERCUSSION": [
        {"match": "DRUMS", "action": "mute"},
    ],
    "SYNTH BASS": [
        {"match": "BASS", "action": "mute"},
    ],
    "BASS": [
        {"match": "BASS", "action": "mute"},
    ],
}

MIXER_ADJUSTMENTS_ESP = {
    **MIXER_ADJUSTMENTS_ENG,
    "BGV": [
        {"match": "__GROUP__",    "action": "mute"},
        {"match": "__CHILDREN__", "action": "mute"},
    ],
}

# ---------------------------------------------------------------------------
# KEY / TRANSPOSITION
# ---------------------------------------------------------------------------

KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
WARP_MODE_COMPLEX = 4

def key_index(key_str):
    try:
        return KEYS.index(key_str.strip().upper())
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
# LIBRARY LOAD / SAVE
# ---------------------------------------------------------------------------

def load_library():
    rules = dict(DEFAULT_CATEGORY_RULES)
    if os.path.exists(LIBRARY_PATH):
        try:
            with open(LIBRARY_PATH, "r") as f:
                rules.update(json.load(f))
            print(f"Loaded routing library: {LIBRARY_PATH}")
        except Exception as e:
            print(f"Warning: could not load library ({e}), using defaults.")
    return rules

def save_library(rules):
    to_save = {k: v for k, v in rules.items() if k not in DEFAULT_CATEGORY_RULES}
    with open(LIBRARY_PATH, "w") as f:
        json.dump(to_save, f, indent=2)
    print(f"Saved routing library: {LIBRARY_PATH}")

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
    print(f"      Volume → {db_value}dB on '{track_name}'")

def set_mute(track_elem, warnings, track_name):
    mixer   = track_elem.find(".//DeviceChain/Mixer")
    speaker = mixer.find("Speaker") if mixer is not None else None
    manual  = speaker.find("Manual") if speaker is not None else None
    if manual is None:
        warnings.append(f"No Speaker/Manual for '{track_name}' — mute skipped.")
        return
    manual.set("Value", "false")
    print(f"      Muted '{track_name}'")

def unmute(track_elem, warnings, track_name):
    """Ensure a track is unmuted (Speaker/Manual = true)."""
    mixer   = track_elem.find(".//DeviceChain/Mixer")
    speaker = mixer.find("Speaker") if mixer is not None else None
    manual  = speaker.find("Manual") if speaker is not None else None
    if manual is None:
        warnings.append(f"No Speaker/Manual for '{track_name}' — unmute skipped.")
        return
    manual.set("Value", "true")

def reset_volume(track_elem, warnings, track_name):
    """Reset track volume to 0dB (linear 1.0)."""
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

def identify_songs(index, children_of):
    songs = []
    for tid, t in index.items():
        if t["group_id"] == -1 and t["tag"] == "GroupTrack":
            base_name, key = parse_song_name(t["name"])
            ignored = t["name"].upper() in {n.upper() for n in IGNORED_SONG_GROUPS}
            songs.append({
                "id":           tid,
                "raw_name":     t["name"],
                "base_name":    base_name,
                "key":          key,
                "ignored":      ignored,
                "category_ids": children_of.get(tid, []),
            })
    return sorted(songs, key=lambda s: s["id"])

# ---------------------------------------------------------------------------
# MIXER ADJUSTMENT APPLIER
# ---------------------------------------------------------------------------

def apply_mixer_adjustments(cat_name, cat_elem, children_of, index, warnings, adjustments_map):
    adjustments = next(
        (v for k, v in adjustments_map.items() if k.upper() == cat_name.upper()), None)
    if not adjustments:
        return
    cat_id = int(cat_elem.get("Id", -99))
    for adj in adjustments:
        match, action, db_val = adj["match"], adj["action"], adj.get("value", 0)
        targets = []
        names   = []
        if match == "__GROUP__":
            targets = [cat_elem]
            names   = [cat_name]
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

def transpose_song(song, new_key, index, children_of, warnings):
    current_key = song["key"]
    if current_key is None:
        warnings.append(f"'{song['raw_name']}' has no key in its name — cannot transpose.")
        return
    delta = semitone_delta(current_key, new_key)
    if delta is None:
        warnings.append(f"Unrecognised key for '{song['raw_name']}' — skipping.")
        return
    if delta == 0:
        return

    print(f"  Transposing '{song['raw_name']}': {current_key} → {new_key} ({delta:+d} st)")

    # Collect IDs of tracks inside atonal categories — don't pitch-shift these
    atonal_track_ids = set()
    for cat_id in song["category_ids"]:
        if index[cat_id]["name"].upper() in ATONAL_CATEGORIES:
            for desc_id in get_descendants(cat_id, children_of):
                atonal_track_ids.add(desc_id)
            print(f"    Skipping '{index[cat_id]['name']}' category (atonal)")

    clip_count = 0
    for desc_id in get_descendants(song["id"], children_of):
        t = index[desc_id]
        if t["tag"] != "AudioTrack" or desc_id in atonal_track_ids:
            continue
        for clip in t["elem"].findall(".//AudioClip"):
            is_warped = clip.find("IsWarped")
            if is_warped is not None:
                is_warped.set("Value", "true")
            warp_mode = clip.find("WarpMode")
            if warp_mode is not None:
                warp_mode.set("Value", str(WARP_MODE_COMPLEX))
            pitch = clip.find("PitchCoarse")
            if pitch is not None:
                pitch.set("Value", str(int(pitch.get("Value", "0")) + delta))
                clip_count += 1

    # Rename song group
    new_raw_name = format_song_name(song["base_name"], new_key)
    for attr_path in [".//Name/EffectiveName", ".//Name/UserName"]:
        el = index[song["id"]]["elem"].find(attr_path)
        if el is not None:
            el.set("Value", new_raw_name)
    print(f"    Renamed to '{new_raw_name}', adjusted {clip_count} clip(s)")

# ---------------------------------------------------------------------------
# ROUTING PASSES
# ---------------------------------------------------------------------------

def route_standard(root, index, children_of, songs, category_rules,
                   adjustments_map, transpose_map, warnings):
    """Apply standard campus routing (English or Español)."""
    tracks_elem = root.find(".//Tracks")

    # Top-level tracks (CLICK, GUIDE, CUES)
    for tid, t in index.items():
        if t["group_id"] == -1 and t["tag"] != "GroupTrack":
            rule = TOP_LEVEL_TRACK_RULES.get(t["name"].upper())
            if rule:
                set_routing(t["elem"], rule, warnings, t["name"])
                print(f"  [TOP-LEVEL] '{t['name']}' → {rule['lower']}")
            else:
                warnings.append(f"Unrecognized top-level track '{t['name']}' — untouched.")

    for song in songs:
        if song["ignored"]:
            continue
        print(f"\n  Song: {song['raw_name']}")

        for cat_id in song["category_ids"]:
            cat      = index[cat_id]
            cat_name = cat["name"]
            rule = next((r for k, r in category_rules.items()
                         if k.upper() == cat_name.upper()), None)
            if rule is None:
                warnings.append(f"'{song['raw_name']}' → '{cat_name}' has no routing rule — untouched.")
                print(f"    [NO RULE] '{cat_name}'")
                continue
            set_routing(cat["elem"], rule, warnings, cat_name)
            print(f"    {cat_name} → {rule['lower']}")
            for child_id in children_of.get(cat_id, []):
                child = index[child_id]
                set_routing(child["elem"], rule, warnings, child["name"])
                print(f"      '{child['name']}' → {rule['lower']}")
            apply_mixer_adjustments(cat_name, cat["elem"], children_of, index,
                                    warnings, adjustments_map)

        # Transposition
        new_key = transpose_map.get(song["id"])
        if new_key and new_key != song["key"]:
            transpose_song(song, new_key, index, children_of, warnings)

def unfold_all_tracks(index):
    """Set TrackUnfolded=true on every track so everything is expanded on open."""
    count = 0
    for tid, t in index.items():
        unfolded = t["elem"].find("TrackUnfolded")
        if unfolded is not None:
            unfolded.set("Value", "true")
            count += 1
    print(f"  Expanded {count} track(s)")

def route_practice(root, index, children_of, songs, warnings):
    """
    Route ALL tracks to Master for practice MP3 export — including click/guide/cues.
    Resets all volumes to 0dB and unmutes everything.
    """
    print("\n  [PRACTICE] Routing all tracks to Master...")

    # Top-level tracks (CLICK, GUIDE, CUES, etc.) — all go to Master
    for tid, t in index.items():
        if t["group_id"] == -1 and t["tag"] != "GroupTrack":
            set_routing(t["elem"], MASTER_ROUTE, warnings, t["name"])
            reset_volume(t["elem"], warnings, t["name"])
            unmute(t["elem"], warnings, t["name"])
            print(f"  [TOP-LEVEL] '{t['name']}' → Master")

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
# FILE BUILDER
# ---------------------------------------------------------------------------

def build_output(input_path, suffix, modifier_fn):
    """
    Load the .als, apply modifier_fn(root, index, children_of, songs),
    save as <base>_<suffix>.als and return the output path.
    """
    with gzip.open(input_path, "rb") as f:
        root = ET.fromstring(f.read())

    tracks_elem = root.find(".//Tracks")
    index, children_of = build_track_index(tracks_elem)
    songs = identify_songs(index, children_of)

    modifier_fn(root, index, children_of, songs)

    base        = os.path.splitext(input_path)[0]
    output_path = f"{base}_{suffix}.als"
    counter     = 1
    while os.path.exists(output_path):
        output_path = f"{base}_{suffix}_{counter}.als"
        counter += 1

    with gzip.open(output_path, "wb") as f:
        f.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))

    return output_path

# ---------------------------------------------------------------------------
# DIALOGS
# ---------------------------------------------------------------------------

def show_campus_dialog():
    """Ask which outputs to generate. Returns set of selected campus keys."""
    selected = {}
    dialog = tk.Toplevel()
    dialog.title("Generate Outputs")
    dialog.resizable(False, False)
    dialog.grab_set()

    tk.Label(dialog, text="Which outputs would you like to generate?",
             font=("", 10, "bold"), padx=16, pady=10).pack(anchor="w")

    options = [
        ("eng", "English  —  routed .als + practice .als (for MP3 export)"),
        ("esp", "Español  —  routed .als + practice .als (for MP3 export)"),
    ]
    vars_ = {}
    for key, label in options:
        var = tk.BooleanVar(value=True)
        vars_[key] = var
        tk.Checkbutton(dialog, text=label, variable=var,
                       padx=20, anchor="w").pack(fill="x")

    result = {}

    def on_ok():
        for key, var in vars_.items():
            result[key] = var.get()
        dialog.destroy()

    def on_cancel():
        dialog.destroy()

    btn = tk.Frame(dialog)
    btn.pack(pady=12)
    tk.Button(btn, text="Continue", command=on_ok,     width=12).pack(side="left", padx=6)
    tk.Button(btn, text="Cancel",   command=on_cancel, width=12).pack(side="left", padx=6)

    dialog.wait_window()
    return result

def show_transpose_dialog(songs, label=""):
    keyed = [s for s in songs if not s["ignored"] and s["key"] is not None]
    if not keyed:
        return {}

    dialog = tk.Toplevel()
    dialog.title(f"Transpose Songs — {label}" if label else "Transpose Songs")
    dialog.resizable(False, False)
    dialog.grab_set()

    for col, h in enumerate(["Song", "Current Key", "New Key"]):
        tk.Label(dialog, text=h, font=("", 10, "bold"), padx=10, pady=6,
                 bg="#ddd").grid(row=0, column=col, sticky="ew", padx=1, pady=1)

    vars_ = {}
    for row, song in enumerate(keyed, start=1):
        tk.Label(dialog, text=song["base_name"], padx=10, anchor="w").grid(
            row=row, column=0, sticky="ew", padx=4, pady=2)
        tk.Label(dialog, text=song["key"], padx=10, anchor="center").grid(
            row=row, column=1, sticky="ew", padx=4, pady=2)
        var = tk.StringVar(value=song["key"])
        vars_[song["id"]] = var
        tk.OptionMenu(dialog, var, *KEYS).grid(row=row, column=2, padx=8, pady=2)

    result = {}

    def on_apply():
        for song_id, var in vars_.items():
            result[song_id] = var.get()
        dialog.destroy()

    def on_cancel():
        dialog.destroy()

    btn = tk.Frame(dialog)
    btn.grid(row=len(keyed)+1, column=0, columnspan=3, pady=12)
    tk.Button(btn, text="Apply",  command=on_apply,  width=12).pack(side="left", padx=6)
    tk.Button(btn, text="Cancel", command=on_cancel, width=12).pack(side="left", padx=6)

    dialog.wait_window()
    return result

def prompt_for_unknown_categories(unknown_names):
    if not unknown_names:
        return {}
    results   = {}
    option_keys = list(OUTPUT_OPTIONS.keys())
    dialog    = tk.Toplevel()
    dialog.title("Unknown Categories Found")
    dialog.resizable(False, False)
    dialog.grab_set()
    tk.Label(dialog,
             text="The following category names have no routing rule.\n"
                  "Choose an output for each, or skip to leave untouched.",
             justify="left", padx=12, pady=8
             ).grid(row=0, column=0, columnspan=2, sticky="w")
    vars_ = {}
    for i, name in enumerate(unknown_names):
        tk.Label(dialog, text=name, font=("", 10, "bold"), padx=12).grid(
            row=i+1, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=option_keys[-1])
        vars_[name] = var
        tk.OptionMenu(dialog, var, *option_keys).grid(
            row=i+1, column=1, sticky="w", pady=3, padx=6)
    save_var = tk.BooleanVar(value=True)
    tk.Checkbutton(dialog, text="Save new entries to routing library",
                   variable=save_var).grid(
        row=len(unknown_names)+1, column=0, columnspan=2,
        sticky="w", padx=12, pady=(10, 4))
    def on_ok():
        for name, var in vars_.items():
            results[name] = OUTPUT_OPTIONS[var.get()]
        results["__save__"] = save_var.get()
        dialog.destroy()
    tk.Button(dialog, text="Apply", command=on_ok, width=14).grid(
        row=len(unknown_names)+2, column=0, columnspan=2, pady=10)
    dialog.wait_window()
    return results

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def collect_unknowns(files, category_rules):
    unknowns = set()
    for input_path in files:
        try:
            with gzip.open(input_path, "rb") as f:
                root = ET.fromstring(f.read())
            te = root.find(".//Tracks")
            if te is None:
                continue
            idx, ch = build_track_index(te)
            for song in identify_songs(idx, ch):
                if song["ignored"]:
                    continue
                for cat_id in song["category_ids"]:
                    cat_name = idx[cat_id]["name"]
                    if not any(k.upper() == cat_name.upper() for k in category_rules):
                        unknowns.add(cat_name)
        except Exception:
            pass
    return unknowns

def collect_songs_for_transpose(files):
    all_songs, seen = [], set()
    for input_path in files:
        try:
            with gzip.open(input_path, "rb") as f:
                root = ET.fromstring(f.read())
            te = root.find(".//Tracks")
            if te is None:
                continue
            idx, ch = build_track_index(te)
            for song in identify_songs(idx, ch):
                key = (song["raw_name"], input_path)
                if key not in seen:
                    song["_file"] = input_path
                    all_songs.append(song)
                    seen.add(key)
        except Exception:
            pass
    return all_songs

# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def select_and_process():
    root_tk = tk.Tk()
    root_tk.withdraw()

    files = filedialog.askopenfilenames(
        title="Select Ableton Live (.als) Files",
        filetypes=[("Ableton Live Projects", "*.als")],
    )
    if not files:
        messagebox.showinfo("No Files Selected", "No files were selected. Exiting.")
        return

    # 1. Which campuses?
    campus_choices = show_campus_dialog()
    if not any(campus_choices.values()):
        messagebox.showinfo("Nothing Selected", "No outputs selected. Exiting.")
        return

    # 2. Unknown categories
    category_rules = load_library()
    unknowns = collect_unknowns(files, category_rules)
    if unknowns:
        user_choices = prompt_for_unknown_categories(sorted(unknowns))
        should_save  = user_choices.pop("__save__", False)
        for name, rule in user_choices.items():
            if rule is not None:
                category_rules[name] = rule
        if should_save:
            save_library(category_rules)

    # 3. Two transpose dialogs — English and Español may have different keys
    all_songs = collect_songs_for_transpose(files)

    eng_transpose_maps = {f: {} for f in files}
    esp_transpose_maps = {f: {} for f in files}

    if campus_choices.get("eng"):
        print("\nShowing English transpose dialog...")
        raw_eng = show_transpose_dialog(all_songs, label="English")
        for song in all_songs:
            nk = raw_eng.get(song["id"])
            if nk:
                eng_transpose_maps[song["_file"]][song["id"]] = nk

    if campus_choices.get("esp"):
        print("\nShowing Español transpose dialog...")
        raw_esp = show_transpose_dialog(all_songs, label="Español")
        for song in all_songs:
            nk = raw_esp.get(song["id"])
            if nk:
                esp_transpose_maps[song["_file"]][song["id"]] = nk

    # 4. Generate up to 4 output files per input file:
    #      _Eng.als          — routed + adjusted for English
    #      _Eng_Practice.als — tuned for English, all tracks to Master, all unmuted
    #      _Esp.als          — routed + adjusted for Español (BGV muted)
    #      _Esp_Practice.als — tuned for Español, all tracks to Master, all unmuted
    all_warnings, generated, errors = [], [], []

    for input_path in files:
        eng_map = eng_transpose_maps.get(input_path, {})
        esp_map = esp_transpose_maps.get(input_path, {})
        base    = os.path.basename(input_path)

        # --- English routed ---
        if campus_choices.get("eng"):
            try:
                warnings = []
                def make_eng(root, index, children_of, songs,
                             _w=warnings, _cr=category_rules, _tm=eng_map):
                    route_standard(root, index, children_of, songs,
                                   _cr, MIXER_ADJUSTMENTS_ENG, _tm, _w)
                path = build_output(input_path, "Eng", make_eng)
                generated.append(f"English:          {os.path.basename(path)}")
                all_warnings.extend(warnings)
                print(f"\nSaved: {path}")
            except Exception as e:
                errors.append(f"{base} (Eng): {e}")

        # --- English practice (tuned for Eng, all to Master, all unmuted) ---
        if campus_choices.get("eng"):
            try:
                warnings = []
                def make_eng_practice(root, index, children_of, songs,
                                      _w=warnings, _tm=eng_map):
                    for song in songs:
                        if song["ignored"]:
                            continue
                        nk = _tm.get(song["id"])
                        if nk and nk != song["key"]:
                            transpose_song(song, nk, index, children_of, _w)
                    route_practice(root, index, children_of, songs, _w)
                path = build_output(input_path, "Eng_Practice", make_eng_practice)
                generated.append(f"English Practice: {os.path.basename(path)}")
                all_warnings.extend(warnings)
                print(f"\nSaved: {path}")
            except Exception as e:
                errors.append(f"{base} (Eng_Practice): {e}")

        # --- Español routed ---
        if campus_choices.get("esp"):
            try:
                warnings = []
                def make_esp(root, index, children_of, songs,
                             _w=warnings, _cr=category_rules, _tm=esp_map):
                    route_standard(root, index, children_of, songs,
                                   _cr, MIXER_ADJUSTMENTS_ESP, _tm, _w)
                path = build_output(input_path, "Esp", make_esp)
                generated.append(f"Español:          {os.path.basename(path)}")
                all_warnings.extend(warnings)
                print(f"\nSaved: {path}")
            except Exception as e:
                errors.append(f"{base} (Esp): {e}")

        # --- Español practice (tuned for Esp, all to Master, all unmuted) ---
        if campus_choices.get("esp"):
            try:
                warnings = []
                def make_esp_practice(root, index, children_of, songs,
                                      _w=warnings, _tm=esp_map):
                    for song in songs:
                        if song["ignored"]:
                            continue
                        nk = _tm.get(song["id"])
                        if nk and nk != song["key"]:
                            transpose_song(song, nk, index, children_of, _w)
                    route_practice(root, index, children_of, songs, _w)
                path = build_output(input_path, "Esp_Practice", make_esp_practice)
                generated.append(f"Español Practice: {os.path.basename(path)}")
                all_warnings.extend(warnings)
                print(f"\nSaved: {path}")
            except Exception as e:
                errors.append(f"{base} (Esp_Practice): {e}")

    # 5. Summary
    summary = "Files generated:\n" + "\n".join(f"  • {g}" for g in generated)
    summary += "\n\nℹ️  Open each Practice file in Ableton and use Export Audio to generate MP3s."
    if all_warnings:
        summary += f"\n\nWarnings ({len(all_warnings)}):\n" + "\n".join(all_warnings)
    if errors:
        summary += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors)

    print("\n" + summary)
    messagebox.showinfo("Done", summary)

if __name__ == "__main__":
    try:
        select_and_process()
    except Exception as e:
        messagebox.showerror("Fatal Error", str(e))
