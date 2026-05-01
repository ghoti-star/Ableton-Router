"""
app.py — Streamlit web UI for ALS stem routing and transposition.
"""

import io
import json

import streamlit as st

from processor import (
    KEYS,
    load_config, save_config,
    process_als, scan_songs, scan_unknown_categories,
)

st.set_page_config(page_title="Stem Router", page_icon="🎛️", layout="centered")

# ---------------------------------------------------------------------------
# SESSION STATE HELPERS
# ---------------------------------------------------------------------------

def init_state():
    if "cfg" not in st.session_state:
        st.session_state.cfg = load_config()
    if "als_bytes" not in st.session_state:
        st.session_state.als_bytes = None
    if "als_name" not in st.session_state:
        st.session_state.als_name = None
    if "songs" not in st.session_state:
        st.session_state.songs = []
    if "unknowns_resolved" not in st.session_state:
        st.session_state.unknowns_resolved = {}

init_state()
cfg = st.session_state.cfg

# ---------------------------------------------------------------------------
# TABS
# ---------------------------------------------------------------------------

tab_process, tab_settings = st.tabs(["🎛️ Process", "⚙️ Settings"])

# ===========================================================================
# PROCESS TAB
# ===========================================================================

with tab_process:
    st.title("🎛️ Stem Router")
    st.caption("Upload an Ableton .als file, configure keys, and download your campus files.")

    # --- File upload ---
    uploaded = st.file_uploader("Upload .als file", type=["als"])
    if uploaded and uploaded.name != st.session_state.als_name:
        st.session_state.als_bytes = uploaded.read()
        st.session_state.als_name  = uploaded.name
        st.session_state.songs     = scan_songs(st.session_state.als_bytes, cfg)
        st.session_state.unknowns_resolved = {}

    if not st.session_state.als_bytes:
        st.info("Upload an .als file to get started.")
        st.stop()

    als_bytes = st.session_state.als_bytes
    als_name  = st.session_state.als_name
    songs     = st.session_state.songs
    base_name = als_name.rsplit(".", 1)[0]

    st.success(f"**{als_name}** — {len([s for s in songs if not s['ignored']])} songs detected")

    # --- Unknown categories ---
    unknowns = scan_unknown_categories(als_bytes, cfg)
    if unknowns:
        st.warning(f"**Unknown categories found:** {', '.join(sorted(unknowns))}")
        st.caption("Assign them a routing output below, or leave as 'Skip'.")
        output_keys = list(cfg["output_options"].keys()) + ["Skip (leave untouched)"]
        for name in sorted(unknowns):
            col1, col2 = st.columns([2, 3])
            with col1:
                st.markdown(f"**{name}**")
            with col2:
                choice = st.selectbox(
                    f"Route '{name}' to",
                    output_keys,
                    index=len(output_keys) - 1,
                    key=f"unknown_{name}",
                    label_visibility="collapsed",
                )
                if choice != "Skip (leave untouched)":
                    st.session_state.unknowns_resolved[name] = cfg["output_options"][choice]

        if st.button("Save new categories to config"):
            for name, rule in st.session_state.unknowns_resolved.items():
                cfg["category_routing"][name] = rule
            save_config(cfg)
            st.success("Saved to config.json.")
            st.rerun()

    st.divider()

    # --- Campus selection ---
    st.subheader("Campuses")
    campus_keys = list(cfg["campuses"].keys())
    selected_campuses = []
    cols = st.columns(len(campus_keys))
    for i, key in enumerate(campus_keys):
        with cols[i]:
            label = cfg["campuses"][key]["label"]
            if st.checkbox(f"{label}", value=True, key=f"campus_{key}"):
                selected_campuses.append(key)

    if not selected_campuses:
        st.warning("Select at least one campus.")
        st.stop()

    st.divider()

    # --- Transpose dialogs per campus ---
    transpose_maps = {}  # {campus_key: {song_id: new_key}}

    keyed_songs = [s for s in songs if not s["ignored"] and s["key"] is not None]

    if keyed_songs:
        st.subheader("Keys")
        st.caption("Set the target key for each song per campus. Leave unchanged to skip transposition.")

        # Header row
        header_cols = st.columns([3] + [2] * len(selected_campuses))
        header_cols[0].markdown("**Song**")
        for i, key in enumerate(selected_campuses):
            header_cols[i + 1].markdown(f"**{cfg['campuses'][key]['label']}**")

        for song in keyed_songs:
            row_cols = st.columns([3] + [2] * len(selected_campuses))
            row_cols[0].markdown(f"{song['base_name']}  \n`{song['key']}`")
            for i, campus_key in enumerate(selected_campuses):
                current_idx = KEYS.index(song["key"]) if song["key"] in KEYS else 0
                new_key = row_cols[i + 1].selectbox(
                    f"{song['base_name']} {campus_key}",
                    KEYS,
                    index=current_idx,
                    key=f"key_{song['id']}_{campus_key}",
                    label_visibility="collapsed",
                )
                if campus_key not in transpose_maps:
                    transpose_maps[campus_key] = {}
                transpose_maps[campus_key][song["id"]] = new_key
    else:
        st.info("No songs with keys found in this file.")
        for key in selected_campuses:
            transpose_maps[key] = {}

    st.divider()

    # --- Generate ---
    if st.button("🎛️ Generate Files", type="primary", use_container_width=True):
        all_warnings = []
        generated    = []  # list of (fname, bytes)

        progress    = st.progress(0)
        total_steps = len(selected_campuses) * 2
        step        = 0

        for campus_key in selected_campuses:
            label = cfg["campuses"][campus_key]["label"]
            t_map = transpose_maps.get(campus_key, {})

            # Routed file
            with st.spinner(f"Building {label} routed file…"):
                try:
                    out_bytes, warns = process_als(
                        als_bytes, campus_key, t_map, cfg, practice=False)
                    generated.append((f"{base_name}_{campus_key}.als", out_bytes))
                    all_warnings.extend(warns)
                except Exception as e:
                    st.error(f"{label} routing failed: {e}")
            step += 1
            progress.progress(step / total_steps)

            # Practice file
            with st.spinner(f"Building {label} practice file…"):
                try:
                    out_bytes, warns = process_als(
                        als_bytes, campus_key, t_map, cfg, practice=True)
                    generated.append((f"{base_name}_{campus_key}_Practice.als", out_bytes))
                    all_warnings.extend(warns)
                except Exception as e:
                    st.error(f"{label} practice failed: {e}")
            step += 1
            progress.progress(step / total_steps)

        st.success(f"Done! {len(generated)} file(s) ready.")

        if all_warnings:
            with st.expander(f"⚠️ {len(all_warnings)} warning(s)"):
                for w in all_warnings:
                    st.markdown(f"- {w}")

        st.markdown("**Download files:**")
        for fname, data in generated:
            st.download_button(
                label=f"⬇️ {fname}",
                data=data,
                file_name=fname,
                mime="application/octet-stream",
                use_container_width=True,
                key=f"dl_{fname}",
            )

# ===========================================================================
# SETTINGS TAB
# ===========================================================================

with tab_settings:
    st.title("⚙️ Settings")

    # --- Category Routing ---
    st.subheader("Category Routing")
    st.caption("Maps instrument category names to output channels.")

    output_option_labels = list(cfg["output_options"].keys())
    output_option_values = list(cfg["output_options"].values())

    def rule_to_label(rule):
        for label, r in cfg["output_options"].items():
            if r == rule:
                return label
        return output_option_labels[0]

    routing_changes = {}
    cat_to_delete = []

    for cat_name, rule in list(cfg["category_routing"].items()):
        col1, col2, col3 = st.columns([3, 3, 1])
        with col1:
            new_name = st.text_input("Category", value=cat_name,
                                     key=f"catname_{cat_name}", label_visibility="collapsed")
        with col2:
            current_label = rule_to_label(rule)
            idx = output_option_labels.index(current_label) if current_label in output_option_labels else 0
            new_label = st.selectbox("Output", output_option_labels, index=idx,
                                     key=f"catout_{cat_name}", label_visibility="collapsed")
        with col3:
            if st.button("✕", key=f"catdel_{cat_name}", help="Remove"):
                cat_to_delete.append(cat_name)
        routing_changes[cat_name] = (new_name, cfg["output_options"][new_label])

    col_add1, col_add2, col_add3 = st.columns([3, 3, 1])
    with col_add1:
        new_cat_name = st.text_input("New category name", key="new_cat_name",
                                     placeholder="e.g. HORNS", label_visibility="collapsed")
    with col_add2:
        new_cat_out = st.selectbox("Output for new category", output_option_labels,
                                   key="new_cat_out", label_visibility="collapsed")

    if st.button("Save Category Routing"):
        new_routing = {}
        for old_name, (new_name, rule) in routing_changes.items():
            if old_name not in cat_to_delete:
                new_routing[new_name.strip() or old_name] = rule
        if new_cat_name.strip():
            new_routing[new_cat_name.strip()] = cfg["output_options"][new_cat_out]
        cfg["category_routing"] = new_routing
        save_config(cfg)
        st.session_state.cfg = cfg
        st.success("Category routing saved.")
        st.rerun()

    st.divider()

    # --- Atonal Categories ---
    st.subheader("Atonal Categories")
    st.caption("Tracks in these categories are never pitch-shifted during transposition.")
    atonal_str = st.text_input(
        "Comma-separated list",
        value=", ".join(cfg.get("atonal_categories", [])),
        key="atonal_input",
    )
    if st.button("Save Atonal Categories"):
        cfg["atonal_categories"] = [x.strip() for x in atonal_str.split(",") if x.strip()]
        save_config(cfg)
        st.session_state.cfg = cfg
        st.success("Saved.")

    st.divider()

    # --- Campuses ---
    st.subheader("Campuses")
    st.caption("Each campus generates a routed file and a practice file.")

    for campus_key, campus_data in cfg["campuses"].items():
        with st.expander(f"{campus_data['label']} ({campus_key})"):
            new_label = st.text_input("Display name", value=campus_data["label"],
                                      key=f"campus_label_{campus_key}")
            st.markdown("**Mixer Adjustments**")
            st.caption("Edit raw JSON for this campus's mixer adjustments.")
            adj_json = st.text_area(
                "Adjustments JSON",
                value=json.dumps(campus_data.get("mixer_adjustments", {}), indent=2),
                height=200,
                key=f"campus_adj_{campus_key}",
                label_visibility="collapsed",
            )
            if st.button(f"Save {campus_data['label']}", key=f"save_campus_{campus_key}"):
                try:
                    parsed = json.loads(adj_json)
                    cfg["campuses"][campus_key]["label"] = new_label
                    cfg["campuses"][campus_key]["mixer_adjustments"] = parsed
                    save_config(cfg)
                    st.session_state.cfg = cfg
                    st.success("Saved.")
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON: {e}")

    st.divider()
    st.subheader("Add Campus")
    col1, col2 = st.columns(2)
    with col1:
        new_campus_key   = st.text_input("Campus key (short, no spaces)", placeholder="e.g. North")
    with col2:
        new_campus_label = st.text_input("Display name", placeholder="e.g. North Campus")
    if st.button("Add Campus"):
        if new_campus_key.strip():
            cfg["campuses"][new_campus_key.strip()] = {
                "label": new_campus_label.strip() or new_campus_key.strip(),
                "mixer_adjustments": {}
            }
            save_config(cfg)
            st.session_state.cfg = cfg
            st.success(f"Campus '{new_campus_key}' added. Configure its mixer adjustments above.")
            st.rerun()

    st.divider()

    # --- Raw JSON ---
    st.subheader("Raw config.json")
    with st.expander("View / edit full config (advanced)"):
        raw = st.text_area("config.json", value=json.dumps(cfg, indent=2), height=400)
        if st.button("Save Raw JSON"):
            try:
                new_cfg = json.loads(raw)
                save_config(new_cfg)
                st.session_state.cfg = new_cfg
                st.success("config.json saved.")
                st.rerun()
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")
