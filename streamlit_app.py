"""
Service Area Checker - drive time / miles from a base location to many ZIP codes.

- Origin: any address or ZIP (default: the office address).
- Add many ZIP codes at once; each is drawn on the map and colored:
    green = within the max drive time, red = outside it.
- Click a ZIP on the map to see drive time + miles, and remove it from the selection.

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


@st.cache_resource(show_spinner="Loading ZIP boundaries (first time only)...")
def load_zip_polygons():
    """Return {zip: geometry} for Texas ZIPs. Empty dict if the download fails."""
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
            if z and feat.get("geometry"):
                out[z] = feat["geometry"]
        return out
    except Exception:
        return {}


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
ss.setdefault("selected", None)
ss.setdefault("origin_text", DEFAULT_ORIGIN)


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
st.caption(f"Origin: **{ss.origin['label']}** · limit: **{max_minutes} min**")

if not ss.zips:
    st.info("Add one or more ZIP codes from the sidebar.")
    st.map(pd.DataFrame([{"lat": ss.origin["lat"], "lon": ss.origin["lon"]}]), zoom=9)
    st.stop()

polygons = load_zip_polygons()

features = []
for z, d in ss.zips.items():
    within = d["minutes"] <= max_minutes
    geom = polygons.get(z) or circle_geometry(d["lat"], d["lon"])
    features.append(
        {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "zip": z,
                "color": GREEN if within else RED,
                "selected": z == ss.selected,
                "label": f"{z} · {d['minutes']:.0f} min · {d['miles']:.1f} mi",
            },
        }
    )

lats = [d["lat"] for d in ss.zips.values()] + [ss.origin["lat"]]
lons = [d["lon"] for d in ss.zips.values()] + [ss.origin["lon"]]

m = folium.Map(location=[ss.origin["lat"], ss.origin["lon"]], zoom_start=10, tiles="OpenStreetMap")
m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(30, 30))
folium.GeoJson(
    {"type": "FeatureCollection", "features": features},
    style_function=lambda f: {
        "color": "#111" if f["properties"]["selected"] else f["properties"]["color"],
        "weight": 4 if f["properties"]["selected"] else 1.5,
        "fillColor": f["properties"]["color"],
        "fillOpacity": 0.5,
    },
    tooltip=folium.GeoJsonTooltip(fields=["label"], labels=False),
).add_to(m)
folium.Marker(
    [ss.origin["lat"], ss.origin["lon"]],
    tooltip="Origin",
    icon=folium.Icon(color="blue", icon="home"),
).add_to(m)

left, right = st.columns([3, 1.3])
with left:
    out = st_folium(
        m,
        height=580,
        use_container_width=True,
        returned_objects=["last_active_drawing"],
        key="map",
    )
    st.markdown(
        f"<span style='color:{GREEN}'>■</span> within {max_minutes} min &nbsp;&nbsp; "
        f"<span style='color:{RED}'>■</span> over {max_minutes} min",
        unsafe_allow_html=True,
    )

clicked = ((out or {}).get("last_active_drawing") or {}).get("properties", {}).get("zip")
if clicked and clicked in ss.zips and clicked != ss.selected:
    ss.selected = clicked
    st.rerun()

with right:
    st.subheader("Selected ZIP")
    sel = ss.selected
    if sel and sel in ss.zips:
        d = ss.zips[sel]
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
        if st.button("🗑 Remove this ZIP", use_container_width=True):
            del ss.zips[sel]
            ss.selected = None
            st.rerun()
    else:
        st.write("Click a ZIP on the map to see its details.")

# ----------------------------------------------------------------------------
# Table + export
# ----------------------------------------------------------------------------
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
