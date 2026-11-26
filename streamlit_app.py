"""
Service Area Checker - drive time / miles from a base location to many ZIP codes.

- Origin: any address or ZIP (default: the office address).
- Add many ZIP codes at once; each is drawn on the map and colored:
    green = within the max drive time, red = outside it.
- Every other ZIP area around the origin is shown in gray and is clickable:
  click it to see its ZIP, drive time and miles, then add it to your selection.
- Click a selected ZIP to see its details or remove it.

Data sources (all free, no API keys):
- Map tiles: OpenStreetMap
- ZIP centroids: GeoNames via pgeocode
- ZIP boundaries: OpenDataDE State-zip-code-GeoJSON (Texas) - falls back to a circle if missing
- Origin geocoding: OpenStreetMap Nominatim
- Driving time / distance: OSRM public demo server (no live traffic)
"""

import math
import re

import folium
import pandas as pd
import pgeocode
import requests
import streamlit as st
from streamlit_folium import st_folium

DEFAULT_ORIGIN = "6815 Manhattan Blvd, Fort Worth, TX 76120"
DEFAULT_MAX_MINUTES = 30
GREEN = "#2e9e4f"
RED = "#d93025"
GRAY = "#8a8f98"
CLICKABLE_RADIUS_KM = 60  # gray clickable ZIP areas are shown within this distance of the origin
HEADERS = {"User-Agent": "service-area-checker/1.0 (internal roofing tool)"}
TX_ZIP_GEOJSON = (
    "https://raw.githubusercontent.com/OpenDataDE/State-zip-code-GeoJSON/"
    "master/tx_texas_zip_codes_geo.min.json"
)
OSRM_URL = "https://router.project-osrm.org/table/v1/driving/"
METERS_PER_MILE = 1609.344

st.set_page_config(page_title="Service Area Checker", page_icon="📍", layout="wide")


# ----------------------------------------------------------------------------
# Data helpers
# ----------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_nomi():
    return pgeocode.Nominatim("us")


def _iter_points(coords):
    """Yield (lon, lat) from any nested GeoJSON coordinate list."""
    if coords and isinstance(coords[0], (int, float)):
        yield coords[0], coords[1]
    else:
        for c in coords:
            yield from _iter_points(c)


def simplify_geom(geom, step=0.0006):
    """Round to ~10 m and drop points closer than ~60 m to the last kept one (smaller, faster map)."""

    def ring(r):
        out = [r[0]]
        for p in r[1:-1]:
            if abs(p[0] - out[-1][0]) > step or abs(p[1] - out[-1][1]) > step:
                out.append(p)
        out.append(r[0])
        return [[round(x, 4), round(y, 4)] for x, y in out] if len(out) >= 4 else r

    t, c = geom.get("type"), geom.get("coordinates", [])
    if t == "Polygon":
        return {"type": t, "coordinates": [ring(r) for r in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[ring(r) for r in poly] for poly in c]}
    return geom


@st.cache_resource(show_spinner="Loading ZIP boundaries (first time only)...")
def load_zip_polygons():
    """Return {zip: {"geom", "lat", "lon"}} for Texas ZIPs. Empty dict if the download fails."""
    try:
        r = requests.get(TX_ZIP_GEOJSON, headers=HEADERS, timeout=90)
        r.raise_for_status()
        out = {}
        for feat in r.json().get("features", []):
            props = feat.get("properties", {}) or {}
            z = next(
                (str(v) for v in props.values() if re.fullmatch(r"\d{5}", str(v))),
                None,
            )
            geom = feat.get("geometry")
            if not (z and geom):
                continue
            pts = list(_iter_points(geom.get("coordinates", [])))
            if not pts:
                continue
            lons = [p[0] for p in pts]
            lats = [p[1] for p in pts]
            out[z] = {
                "geom": simplify_geom(geom),
                "lat": (min(lats) + max(lats)) / 2,
                "lon": (min(lons) + max(lons)) / 2,
            }
        return out
    except Exception:
        return {}


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def lookup_zip(z):
    r = get_nomi().query_postal_code(z)
    if pd.isna(r.latitude) or pd.isna(r.longitude):
        return None
    place = f"{r.place_name}, {r.state_code}" if isinstance(r.place_name, str) else z
    return {"lat": float(r.latitude), "lon": float(r.longitude), "place": place}


def geocode_origin(text):
    """Address or 5-digit ZIP -> {lat, lon, label} or None."""
    text = text.strip()
    if re.fullmatch(r"\d{5}", text):
        info = lookup_zip(text)
        return {"lat": info["lat"], "lon": info["lon"], "label": f"ZIP {text} center"} if info else None
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": text, "format": "json", "limit": 1, "countrycodes": "us"},
            headers=HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]), "label": text}
    except Exception:
        pass
    # Fallback: a ZIP inside the address text
    m = re.search(r"\b(\d{5})\b", text)
    if m:
        info = lookup_zip(m.group(1))
        if info:
            return {"lat": info["lat"], "lon": info["lon"], "label": f"{text} (ZIP {m.group(1)} center - address not found)"}
    return None


def route_many(origin, dests):
    """OSRM table call in chunks. Returns list of (minutes, miles) or (None, None)."""
    results = []
    for i in range(0, len(dests), 50):
        chunk = dests[i : i + 50]
        coords = [f"{origin['lon']},{origin['lat']}"] + [f"{d['lon']},{d['lat']}" for d in chunk]
        url = OSRM_URL + ";".join(coords)
        params = {
            "sources": "0",
            "destinations": ";".join(str(n) for n in range(1, len(chunk) + 1)),
            "annotations": "duration,distance",
        }
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        durs, dists = data["durations"][0], data["distances"][0]
        for s, m in zip(durs, dists):
            if s is None or m is None:
                results.append((None, None))
            else:
                results.append((s / 60.0, m / METERS_PER_MILE))
    return results


def parse_zips(text):
    tokens = [t for t in re.split(r"[,\s;]+", text.strip()) if t]
    good, bad = [], []
    for t in tokens:
        if re.fullmatch(r"\d{5}", t):
            if t not in good:
                good.append(t)
        else:
            bad.append(t)
    return good, bad


def circle_geometry(lat, lon, radius_km=1.6, n=28):
    pts = []
    for k in range(n + 1):
        a = 2 * math.pi * k / n
        dlat = (radius_km / 111.0) * math.sin(a)
        dlon = (radius_km / (111.0 * math.cos(math.radians(lat)))) * math.cos(a)
        pts.append([lon + dlon, lat + dlat])
    return {"type": "Polygon", "coordinates": [pts]}


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
ss = st.session_state
ss.setdefault("origin", None)
ss.setdefault("zips", {})  # zip -> {lat, lon, place, minutes, miles}
ss.setdefault("preview", {})  # zip -> same fields, for clicked-but-not-added ZIPs
ss.setdefault("selected", None)
ss.setdefault("last_clicked", None)
ss.setdefault("origin_text", DEFAULT_ORIGIN)


def calc_zip(z):
    """Return {lat, lon, place, minutes, miles} for one ZIP, or None (shows a message on failure)."""
    info = lookup_zip(z)
    if not info:
        st.warning(f"Could not find ZIP {z}.")
        return None
    try:
        (mins, miles), = route_many(ss.origin, [info])
    except Exception as e:
        st.error(f"Routing service failed: {e}. Try again in a moment.")
        return None
    if mins is None:
        st.warning(f"No driving route found for {z}.")
        return None
    return {**info, "minutes": mins, "miles": miles}


def add_zips(zip_list):
    found, missing = [], []
    for z in zip_list:
        info = lookup_zip(z)
        (found if info else missing).append((z, info))
    if found:
        try:
            res = route_many(ss.origin, [i for _, i in found])
        except Exception as e:
            st.error(f"Routing service failed: {e}. Nothing was added - try again in a moment.")
            return
        for (z, info), (mins, miles) in zip(found, res):
            if mins is None:
                missing.append((z, None))
            else:
                ss.zips[z] = {**info, "minutes": mins, "miles": miles}
    if missing:
        st.warning("Could not find/route: " + ", ".join(z for z, _ in missing))


def recompute_all():
    existing = list(ss.zips.keys())
    ss.zips = {}
    ss.preview = {}
    ss.selected = None
    if existing:
        add_zips(existing)


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    origin_text = st.text_input("Origin (address or ZIP)", key="origin_text")
    max_minutes = st.slider("Max drive time (minutes)", 5, 90, DEFAULT_MAX_MINUTES)

    if st.button("Set origin", use_container_width=True) or ss.origin is None:
        with st.spinner("Locating origin..."):
            o = geocode_origin(origin_text)
        if o is None:
            st.error("Could not locate that origin.")
        else:
            changed = ss.origin is not None
            ss.origin = o
            if changed:
                with st.spinner("Recalculating all ZIPs from the new origin..."):
                    recompute_all()

    st.divider()
    with st.form("add_form", clear_on_submit=True):
        zip_text = st.text_area(
            "ZIP codes (comma, space or new line separated)",
            placeholder="76120, 76021, 75001\n76244",
            height=120,
        )
        submitted = st.form_submit_button("Add ZIP codes", use_container_width=True)
    if submitted and ss.origin:
        good, bad = parse_zips(zip_text)
        if bad:
            st.warning("Ignored (not 5-digit ZIPs): " + ", ".join(bad))
        new = [z for z in good if z not in ss.zips]
        if new:
            with st.spinner(f"Calculating {len(new)} ZIP(s)..."):
                add_zips(new)

    if ss.zips and st.button("Clear all ZIPs", use_container_width=True):
        ss.zips = {}
        ss.selected = None
        st.rerun()

    st.caption(
        "Drive times come from OSRM (typical driving, no live traffic) to the center of each ZIP. "
        "Treat them as estimates."
    )

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
st.title("📍 Service Area Checker")
if not ss.origin:
    st.stop()
st.caption(
    f"Origin: **{ss.origin['label']}** · limit: **{max_minutes} min** · "
    "click any area on the map to check it"
)

polygons = load_zip_polygons()


@st.cache_resource(show_spinner="Drawing map (first time only)...")
def build_base_map(olat, olon, radius_km):
    """Static map: tiles + origin + clickable gray ZIP areas. Built once per origin, never redrawn."""
    feats = []
    for z, p in polygons.items():
        if haversine_km(olat, olon, p["lat"], p["lon"]) <= radius_km:
            feats.append(
                {
                    "type": "Feature",
                    "geometry": p["geom"],
                    "properties": {"zip": z, "label": f"{z} (click to check)"},
                }
            )
    bm = folium.Map(location=[olat, olon], zoom_start=10, tiles="OpenStreetMap")
    folium.GeoJson(
        {"type": "FeatureCollection", "features": feats},
        style_function=lambda f: {"color": GRAY, "weight": 0.8, "fillColor": GRAY, "fillOpacity": 0.12},
        tooltip=folium.GeoJsonTooltip(fields=["label"], labels=False),
    ).add_to(bm)
    folium.Marker(
        [olat, olon], tooltip="Origin", icon=folium.Icon(color="blue", icon="home")
    ).add_to(bm)
    return bm


base_map = build_base_map(
    round(ss.origin["lat"], 5), round(ss.origin["lon"], 5), CLICKABLE_RADIUS_KM
)

# Dynamic layer: only the selected ZIPs. This is the only thing re-sent to the browser on add/remove.
sel_features = []
for z, d in ss.zips.items():
    within = d["minutes"] <= max_minutes
    poly = polygons.get(z)
    geom = poly["geom"] if poly else circle_geometry(d["lat"], d["lon"])
    sel_features.append(
        {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "zip": z,
                "color": GREEN if within else RED,
                "label": f"{z} · {d['minutes']:.0f} min · {d['miles']:.1f} mi",
            },
        }
    )
fg = folium.FeatureGroup(name="selected")
if sel_features:
    folium.GeoJson(
        {"type": "FeatureCollection", "features": sel_features},
        style_function=lambda f: {
            "color": f["properties"]["color"],
            "weight": 1.5,
            "fillColor": f["properties"]["color"],
            "fillOpacity": 0.5,
        },
        tooltip=folium.GeoJsonTooltip(fields=["label"], labels=False),
    ).add_to(fg)

left, right = st.columns([3, 1.3])
with left:
    out = st_folium(
        base_map,
        feature_group_to_add=fg,
        height=580,
        use_container_width=True,
        returned_objects=["last_active_drawing"],
        key="map",
    )
    st.markdown(
        f"<span style='color:{GREEN}'>■</span> selected, within {max_minutes} min &nbsp;&nbsp; "
        f"<span style='color:{RED}'>■</span> selected, over {max_minutes} min &nbsp;&nbsp; "
        f"<span style='color:{GRAY}'>■</span> not selected (click to check)",
        unsafe_allow_html=True,
    )

# Handle a click in this same run (no extra rerun)
click = (out or {}).get("last_active_drawing")
new_zip = ((click or {}).get("properties") or {}).get("zip")
if new_zip and new_zip != ss.last_clicked:
    ss.last_clicked = new_zip
    ss.selected = new_zip
    if new_zip not in ss.zips and new_zip not in ss.preview:
        with st.spinner(f"Calculating {new_zip}..."):
            data = calc_zip(new_zip)
        if data:
            ss.preview[new_zip] = data

with right:
    st.subheader("Selected ZIP")
    sel = ss.selected
    d = (ss.zips.get(sel) or ss.preview.get(sel)) if sel else None
    if sel and d:
        added = sel in ss.zips
        within = d["minutes"] <= max_minutes
        st.markdown(f"### {sel}")
        st.caption(d["place"])
        c1, c2 = st.columns(2)
        c1.metric("Drive time", f"{d['minutes']:.0f} min")
        c2.metric("Distance", f"{d['miles']:.1f} mi")
        if within:
            st.success(f"Within {max_minutes} min")
        else:
            st.error(f"Over {max_minutes} min")
        if added:
            if st.button("🗑 Remove from selection", use_container_width=True):
                del ss.zips[sel]
                st.rerun()
        else:
            if st.button("➕ Add to selected ZIPs", use_container_width=True, type="primary"):
                ss.zips[sel] = d
                st.rerun()
    else:
        st.write("Click any area on the map to see its ZIP, drive time and miles.")

# ----------------------------------------------------------------------------
# Table + export
# ----------------------------------------------------------------------------
if ss.zips:
    st.divider()
    rows = [
        {
            "ZIP": z,
            "Area": d["place"],
            "Drive time (min)": round(d["minutes"]),
            "Miles": round(d["miles"], 1),
            f"Within {max_minutes} min": d["minutes"] <= max_minutes,
        }
        for z, d in ss.zips.items()
    ]
    df = pd.DataFrame(rows).sort_values("Drive time (min)").reset_index(drop=True)
    st.dataframe(df, use_container_width=True, hide_index=True)

    to_remove = st.multiselect("Remove several ZIPs", options=list(ss.zips.keys()))
    if to_remove and st.button("Remove selected"):
        for z in to_remove:
            ss.zips.pop(z, None)
        if ss.selected in to_remove:
            ss.selected = None
        st.rerun()

    inside = df[df[f"Within {max_minutes} min"]]["ZIP"].tolist()
    st.markdown(f"**ZIPs within {max_minutes} min ({len(inside)}):**")
    st.code(", ".join(inside) if inside else "(none)", language=None)
    st.download_button(
        "Download table (CSV)",
        df.to_csv(index=False).encode("utf-8"),
        file_name="zip_drive_times.csv",
        mime="text/csv",
    )
