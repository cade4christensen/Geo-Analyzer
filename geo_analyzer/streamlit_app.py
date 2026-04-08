import io
import time
import streamlit as st
from analyzer import analyze

st.set_page_config(
    page_title="Coordinate Analyzer",
    page_icon="🗺️",
    layout="centered",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_dist(value, round_to: str):
    if not isinstance(value, (int, float)):
        return value
    if round_to == "whole":
        return int(round(value, 0))
    return round(value, 2)


def fmt_dist(value, round_to: str) -> str:
    if value is None:
        return "none within 2 km"
    v = _round_dist(value, round_to)
    if round_to == "whole":
        return f"{v} m"
    return f"{v:.2f} m"


FEATURE_KEYS = ("Road", "Trail", "Railroad", "Utility Line", "Water Feature")

POPULATION_COLOR = {
    "Wilderness": "#4ade80",
    "Rural":      "#facc15",
    "Suburban":   "#fb923c",
    "Urban":      "#f87171",
}

SURFACE_COLOR = {
    "Structure": "#94a3b8",
    "Road":      "#64748b",
    "Linear":    "#a78bfa",
    "Drainage":  "#38bdf8",
    "Water":     "#0ea5e9",
    "Woods":     "#22c55e",
    "Scrub":     "#84cc16",
    "Field":     "#eab308",
    "Brush":     "#a3e635",
    "Rock":      "#d1d5db",
    "Unknown":   "#6b7280",
}


def badge(label: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:#000;padding:3px 10px;'
        f'border-radius:12px;font-weight:600;font-size:0.85rem;">{label}</span>'
    )


def display_results(result: dict, round_to: str):
    lf = result["linear_features"]
    surface = result["surface"]
    population = result["population"]

    # --- Summary badges ---
    sc = SURFACE_COLOR.get(surface, "#6b7280")
    pc = POPULATION_COLOR.get(population, "#6b7280")
    st.markdown(
        f"**Surface:** {badge(surface, sc)}&nbsp;&nbsp;"
        f"**Population:** {badge(population, pc)}",
        unsafe_allow_html=True,
    )
    st.markdown("")

    # --- Nearest feature summary ---
    nearest_dist = None
    nearest_type = None
    for key in FEATURE_KEYS:
        d = lf.get(key, {}).get("distance_m")
        if d is not None and (nearest_dist is None or d < nearest_dist):
            nearest_dist = d
            nearest_type = key

    if nearest_type:
        st.info(f"**Nearest feature:** {nearest_type} — {fmt_dist(nearest_dist, round_to)}")

    # --- Linear feature table ---
    st.markdown("#### Linear Features")
    rows = []
    for key in FEATURE_KEYS:
        info = lf.get(key, {})
        dist = info.get("distance_m")
        name = info.get("name") or ""
        rows.append({
            "Feature":    key,
            "Distance":   fmt_dist(dist, round_to),
            "Name / Ref": name,
        })

    st.table(rows)


# ---------------------------------------------------------------------------
# Excel processing (reused from cli.py logic, adapted for Streamlit)
# ---------------------------------------------------------------------------

def process_excel_bytes(file_bytes: bytes, round_to: str) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    SUMMARY_COLUMNS = ["Nearest Feature (m)", "Nearest Feature Type"]
    DETAIL_COLUMNS = []
    for fk in FEATURE_KEYS:
        DETAIL_COLUMNS.append(f"Nearest {fk} (m)")
        DETAIL_COLUMNS.append(f"Nearest {fk} Name")
    TAIL_COLUMNS = ["Surface", "Population"]

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
    ws = wb.active

    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    lower = [str(h).strip().lower() if h else "" for h in headers]

    def find_col(*candidates):
        for c in candidates:
            if c.lower() in lower:
                return lower.index(c.lower())
        return None

    lat_col = find_col("lat", "latitude")
    lon_col = find_col("lon", "long", "longitude")

    if lat_col is None or lon_col is None:
        st.error("Could not find lat/lon columns. Expected 'lat'/'latitude' and 'lon'/'longitude'.")
        return None

    summary_start = ws.max_column + 1
    detail_start  = summary_start + len(SUMMARY_COLUMNS)
    tail_start    = detail_start + len(DETAIL_COLUMNS)

    summary_fill = PatternFill("solid", fgColor="1F4E79")
    detail_fill  = PatternFill("solid", fgColor="2E75B6")
    hdr_font     = Font(bold=True, color="FFFFFF")
    center       = Alignment(horizontal="center")

    for i, name in enumerate(SUMMARY_COLUMNS):
        c = ws.cell(row=1, column=summary_start + i, value=name)
        c.fill = summary_fill; c.font = hdr_font; c.alignment = center

    for i, name in enumerate(DETAIL_COLUMNS):
        c = ws.cell(row=1, column=detail_start + i, value=name)
        c.fill = detail_fill; c.font = hdr_font; c.alignment = center

    for i, name in enumerate(TAIL_COLUMNS):
        c = ws.cell(row=1, column=tail_start + i, value=name)
        c.fill = summary_fill; c.font = hdr_font; c.alignment = center

    total = ws.max_row - 1
    progress = st.progress(0, text="Analyzing rows...")

    for row_idx, row in enumerate(ws.iter_rows(min_row=2), start=1):
        progress.progress(row_idx / total, text=f"Analyzing row {row_idx} of {total}...")
        lat_val = row[lat_col].value
        lon_val = row[lon_col].value

        def write_error():
            for i in range(len(SUMMARY_COLUMNS) + len(DETAIL_COLUMNS) + len(TAIL_COLUMNS)):
                ws.cell(row=row_idx + 1, column=summary_start + i, value="ERROR")

        if lat_val is None or lon_val is None:
            write_error(); continue
        try:
            lat_f, lon_f = float(lat_val), float(lon_val)
        except (TypeError, ValueError):
            write_error(); continue

        try:
            if row_idx > 1:
                time.sleep(1)
            result = analyze(lat_f, lon_f)
        except Exception as e:
            write_error(); continue

        lf = result["linear_features"]

        nearest_dist = None
        nearest_type = None
        for key in FEATURE_KEYS:
            d = lf.get(key, {}).get("distance_m")
            if d is not None and (nearest_dist is None or d < nearest_dist):
                nearest_dist = d; nearest_type = key

        nd = _round_dist(nearest_dist, round_to) if nearest_dist is not None else "none within 2km"
        ws.cell(row=row_idx + 1, column=summary_start,     value=nd)
        ws.cell(row=row_idx + 1, column=summary_start + 1, value=nearest_type or "none within 2km")

        col = detail_start
        for key in FEATURE_KEYS:
            info = lf.get(key, {})
            dist = info.get("distance_m")
            name = info.get("name")
            ws.cell(row=row_idx + 1, column=col,     value=_round_dist(dist, round_to) if dist is not None else "none within 2km")
            ws.cell(row=row_idx + 1, column=col + 1, value=name or "")
            col += 2

        ws.cell(row=row_idx + 1, column=tail_start,     value=result["surface"])
        ws.cell(row=row_idx + 1, column=tail_start + 1, value=result["population"])

    # Auto-size & group detail columns
    all_cols = SUMMARY_COLUMNS + DETAIL_COLUMNS + TAIL_COLUMNS
    for i, name in enumerate(all_cols):
        letter = openpyxl.utils.get_column_letter(summary_start + i)
        ws.column_dimensions[letter].width = max(len(name) + 2, 16)

    for c in range(detail_start, tail_start):
        letter = openpyxl.utils.get_column_letter(c)
        ws.column_dimensions[letter].outlineLevel = 1
        ws.column_dimensions[letter].hidden = True
    ws.sheet_view.showOutlineSymbols = True

    progress.empty()

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Coordinate Analyzer")
st.caption("Identifies linear features, surface type, and population density for any US coordinate.")

round_to = st.radio(
    "Distance rounding",
    options=["hundredth", "whole"],
    format_func=lambda x: "Nearest 0.01 m" if x == "hundredth" else "Nearest whole meter",
    horizontal=True,
)

st.markdown("---")
tab_single, tab_excel = st.tabs(["Single Coordinate", "Excel Batch"])

# ---- Single coordinate tab ----
with tab_single:
    col1, col2 = st.columns(2)
    with col1:
        lat = st.number_input("Latitude", value=None, format="%.6f", placeholder="e.g. 40.712800")
    with col2:
        lon = st.number_input("Longitude", value=None, format="%.6f", placeholder="e.g. -74.006000")

    if st.button("Analyze", type="primary", use_container_width=True):
        if lat is None or lon is None:
            st.warning("Enter both a latitude and longitude.")
        else:
            with st.spinner("Querying OpenStreetMap and NLCD..."):
                try:
                    result = analyze(float(lat), float(lon))
                    st.success(f"Results for **{lat}, {lon}**")
                    display_results(result, round_to)
                except Exception as e:
                    st.error(f"Analysis failed: {e}")

# ---- Excel batch tab ----
with tab_excel:
    st.markdown(
        "Upload an `.xlsx` file with **`lat`** and **`lon`** columns. "
        "Results are appended as new columns and returned as a download."
    )
    uploaded = st.file_uploader("Choose an Excel file", type=["xlsx"])

    if uploaded is not None:
        if st.button("Run Analysis", type="primary", use_container_width=True):
            file_bytes = uploaded.read()
            result_bytes = process_excel_bytes(file_bytes, round_to)
            if result_bytes:
                st.success("Done!")
                st.download_button(
                    label="Download analyzed.xlsx",
                    data=result_bytes,
                    file_name=uploaded.name.replace(".xlsx", "_analyzed.xlsx"),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
