"""
credit_utils.py — helper functions for building a CREDIT layer for any country.

CREDIT (Custom Residential Electricity Demand Indicative Target) combines a
poverty map and a GDP map into a 1-5 index, then converts that index into a
residential electricity demand target (kWh/capita/year) using MTF tiers.
Method: Korkovelos et al. (2019), Energies 12(7):1395.

Sections
--------
1. Country lookup          resolve_iso3, normalize_country, get_country_boundary
2. Poverty data (HDX/OPHI) extract_country_tabular, merge_poverty_to_admin
3. GDP data (Kummu v4)     download_file, extract_country_gdp
4. Raster helpers          rasterize_vector, reproj, raster_to_point_shp,
                           reclassify_raster, clip_raster
5. Step 3 — combine        combine_rasters (Option 1, raster arrays)
                           extract_raster_values_to_points (Option 2, points/CSV)
6. Step 4 — CREDIT         credit_from_index

Data sources
------------
- Poverty: OPHI subnational MPI, via the Humanitarian Data Exchange (HDX)
  https://data.humdata.org — one dataset per country, e.g. "niger-mpi".
- Admin boundaries: geoBoundaries API, https://www.geoboundaries.org
- GDP: Kummu, M., Kosonen, M. & Masoumzadeh Sayyar, S. 2025. Downscaled gridded
  global dataset for GDP per capita PPP over 1990-2022 (updated to 2024).
  Scientific Data 12: 178. https://doi.org/10.1038/s41597-025-04487-x
  Zenodo record: https://zenodo.org/records/18429133
"""

import difflib
import math
import os
import unicodedata
import zipfile
from pathlib import Path

import affine
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.mask
import requests
import rioxarray  # noqa: F401  (registers the .rio accessor on xarray objects)
import scipy.interpolate
import xarray as xr
from osgeo import gdal, ogr, osr
from rasterio import features
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import from_bounds
from shapely.geometry import box

try:
    import pycountry
    HAS_PYCOUNTRY = True
except ImportError:
    pycountry = None
    HAS_PYCOUNTRY = False

gdal.UseExceptions()
ogr.UseExceptions()


# =============================================================================
# 1. Country lookup
# =============================================================================

# Country names that differ between common usage and pycountry/HDX naming.
# Extend as you meet new mismatches.
COUNTRY_ALIASES = {
    "ivory coast": "cote d'ivoire",
    "cape verde": "cabo verde",
    "swaziland": "eswatini",
    "drc": "congo, the democratic republic of the",
    "congo-kinshasa": "congo, the democratic republic of the",
    "congo-brazzaville": "congo",
}


def _exact_country_match(key: str):
    """Exact, case-insensitive match on a pycountry name / official name / common name.

    Must run before any fuzzy search: search_fuzzy("niger") can return Nigeria.
    """
    for c in pycountry.countries:
        for attr in ("name", "official_name", "common_name"):
            val = getattr(c, attr, None)
            if val and val.lower() == key:
                return c
    return None


def _lookup_country(country: str):
    """Return the pycountry Country for a name or ISO3 code, or None."""
    if not HAS_PYCOUNTRY:
        return None
    by_code = pycountry.countries.get(alpha_3=country.strip().upper())
    if by_code:
        return by_code
    key = country.strip().lower()
    key = COUNTRY_ALIASES.get(key, key)
    exact = _exact_country_match(key)
    if exact:
        return exact
    try:
        return pycountry.countries.search_fuzzy(key)[0]
    except LookupError:
        return None


def normalize_country(country: str) -> str:
    """Country name or ISO3 code -> official short name (falls back to the input)."""
    match = _lookup_country(country)
    return match.name if match else country.strip()


def resolve_iso3(country: str) -> str:
    """Country name ('Niger', 'DRC') or ISO3 code ('COD') -> upper-case ISO3 code."""
    country = country.strip()
    if len(country) == 3 and country.isalpha() and country.isupper():
        return country
    match = _lookup_country(country)
    if match is None:
        raise ValueError(
            f"Could not resolve '{country}' to an ISO3 code. "
            "Install pycountry (pip install pycountry) or pass the ISO3 code directly."
        )
    return match.alpha_3


_boundary_cache = {}


def get_country_boundary(iso3: str, adm_level: str = "ADM0") -> gpd.GeoDataFrame:
    """Download a country boundary from the geoBoundaries API (cached per iso3 + level).

    adm_level: "ADM0" (country outline), "ADM1" (provinces/regions), "ADM2", ...
    """
    cache_key = (iso3, adm_level)
    if cache_key in _boundary_cache:
        return _boundary_cache[cache_key]

    api_url = f"https://www.geoboundaries.org/api/current/gbOpen/{iso3}/{adm_level}/"
    meta = requests.get(api_url, timeout=30).json()
    gdf = gpd.read_file(meta["gjDownloadURL"])
    print(f"Loaded {iso3} {adm_level} boundary: {len(gdf)} feature(s), CRS={gdf.crs}")
    _boundary_cache[cache_key] = gdf
    return gdf


def read_vector(path) -> gpd.GeoDataFrame:
    """gpd.read_file with a fallback to the fiona engine.

    pyogrio can fail on date columns with pandas 3 (UnboundLocalError in
    _try_parse_datetime); HDX COD-AB shapefiles have such columns (valid_on, valid_to).
    """
    try:
        return gpd.read_file(path)
    except UnboundLocalError:
        return gpd.read_file(path, engine="fiona")


def _adm_level_number(adm_level) -> int:
    """'ADM1' / 'adm1' / 1 -> 1"""
    return int(str(adm_level).upper().replace("ADM", ""))


# =============================================================================
# 2. Poverty data (OPHI MPI via HDX)
# =============================================================================

HDX_CKAN_API = "https://data.humdata.org/api/3/action"

# HDX slugs that do not follow the usual "<country-name>-mpi" pattern.
HDX_SLUG_OVERRIDES = {
    "cod": "democratic-republic-of-the-congo-mpi",
    "congo, democratic republic of the": "democratic-republic-of-the-congo-mpi",
    "congo, the democratic republic of the": "democratic-republic-of-the-congo-mpi",
    "democratic republic of the congo": "democratic-republic-of-the-congo-mpi",
    "drc": "democratic-republic-of-the-congo-mpi",
}


def country_dataset_slug(country: str) -> str:
    """HDX slug of a country's subnational MPI dataset, e.g. 'Burkina Faso' -> 'burkina-faso-mpi'."""
    key = country.strip().lower()
    if key in HDX_SLUG_OVERRIDES:
        return HDX_SLUG_OVERRIDES[key]
    official_name = normalize_country(country)
    if official_name.lower() in HDX_SLUG_OVERRIDES:
        return HDX_SLUG_OVERRIDES[official_name.lower()]
    slug_base = official_name.lower().strip().replace("'", "").replace(",", "")
    return "-".join(slug_base.split()) + "-mpi"


def get_dataset_resources(dataset_slug: str) -> list:
    """Resource list of an HDX dataset."""
    resp = requests.get(f"{HDX_CKAN_API}/package_show", params={"id": dataset_slug}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"HDX API request failed for dataset '{dataset_slug}': {data}")
    return data["result"]["resources"]


def get_country_mpi_resources(country: str) -> list:
    """Find the country's HDX MPI dataset (tries the official name, then the raw input)."""
    official_name = normalize_country(country)
    tried = []
    for name_variant in dict.fromkeys([official_name, country.strip()]):
        slug = country_dataset_slug(name_variant)
        tried.append(slug)
        try:
            return get_dataset_resources(slug)
        except (requests.HTTPError, RuntimeError):
            continue
    raise ValueError(
        f"Could not find an HDX MPI dataset for country='{country}' "
        f"(resolved as '{official_name}'). Tried slugs: {tried}. "
        "Check the exact dataset slug on https://data.humdata.org."
    )


def _warn_if_country_mismatch(resource: dict, country: str) -> None:
    """Warn if the resource name does not mention the requested country."""
    name_lower = resource["name"].lower()
    tokens = {normalize_country(country).lower(), country.strip().lower()}
    try:
        tokens.add(resolve_iso3(country).lower())
    except ValueError:
        pass
    if not any(tok in name_lower for tok in tokens if tok):
        print(f"[WARNING] Resource '{resource['name']}' does not appear to mention "
              f"'{country}' — check this is the right country's data.")


def find_resource_by_kind(resources: list, label: str, kind: str) -> dict:
    """Pick the resource whose name contains `kind` (fuzzy fallback)."""
    matches = [r for r in resources if kind.lower() in r["name"].lower()]
    if not matches:
        close = difflib.get_close_matches(kind, [r["name"] for r in resources], n=1, cutoff=0.4)
        matches = [r for r in resources if close and r["name"] == close[0]]
    if not matches:
        raise ValueError(f"No resource matching kind='{kind}' found for '{label}'. "
                         f"Available resources: {[r['name'] for r in resources]}")
    return matches[0]


def download_resource_if_missing(resource: dict, out_dir: Path, country: str,
                                 extension: str = None) -> Path:
    """Download an HDX resource into out_dir unless it is already on disk."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{country.strip().replace(' ', '_')}_"
    safe_name = resource["name"].replace(" ", "_").replace("/", "-")
    url_suffix = Path(resource["url"].split("?")[0]).suffix.lstrip(".").lower()
    ext = extension or url_suffix or resource.get("format", "").lower() or "dat"
    if safe_name.lower().endswith(f".{ext}"):
        safe_name = safe_name[: -(len(ext) + 1)]
    out_path = out_dir / f"{prefix}{safe_name}.{ext}"

    if out_path.exists():
        print(f"[SKIP] {out_path.name} already exists, not re-downloading.")
        return out_path

    resp = requests.get(resource["url"], timeout=120)
    resp.raise_for_status()
    out_path.write_bytes(resp.content)

    # HDX's declared format is unreliable: rename zips saved without .zip
    if zipfile.is_zipfile(out_path) and out_path.suffix.lower() != ".zip":
        fixed_path = out_path.with_suffix(out_path.suffix + ".zip")
        out_path.rename(fixed_path)
        out_path = fixed_path

    print(f"[OK] Downloaded {out_path.name}")
    return out_path


def extract_country_tabular(country: str, out_dir, kind: str = "MPI and Partial Indices",
                            drop_hxl_row: bool = True) -> pd.DataFrame:
    """Download (if missing) the country's admin-1 MPI CSV into <out_dir>/tabular/ and load it.

    kind: "MPI and Partial Indices" (snapshot) or "MPI Trends Over Time".
    drop_hxl_row: HDX CSVs have a second header row of HXL tags (#adm1+name, ...);
                  drop it so the numeric columns parse cleanly.
    """
    tabular_dir = Path(out_dir) / "tabular"
    resource = find_resource_by_kind(get_country_mpi_resources(country), country, kind)
    _warn_if_country_mismatch(resource, country)
    local_path = download_resource_if_missing(resource, tabular_dir, country, extension="csv")
    print(f"CSV ready: {local_path.name}")
    df = pd.read_csv(local_path)
    if drop_hxl_row:
        df = df.iloc[1:].reset_index(drop=True)
    return df


# --- Admin boundaries --------------------------------------------------------
#
# Preferred source: HDX COD-AB ("Common Operational Datasets - Administrative
# Boundaries", dataset slug "cod-ab-<iso3>"). They carry the same PCodes as the
# OPHI poverty table, so the two join on a code instead of a name.
# Fallback: geoBoundaries (names only), joined by name.

def find_admin_shapefile_resource(resources: list) -> dict:
    """Pick the zipped-shapefile resource of an HDX COD-AB dataset."""
    for r in resources:
        name, fmt = r["name"].lower(), r.get("format", "").lower()
        if fmt == "shp" or ".shp" in name or "_shp" in name:
            return r
    raise ValueError(f"No shapefile resource found. Available: {[r['name'] for r in resources]}")


def _find_admin_shp_file(search_dir: Path, adm_level: int) -> Path:
    """Find the admin-<level> .shp inside an extracted COD-AB archive.

    Handles both naming schemes: new ('cod_admin1.shp', plus '*_em' variants that
    are skipped) and old ('..._admbnda_adm1_....shp').
    """
    all_shp = sorted(search_dir.rglob("*.shp"))
    if not all_shp:
        raise ValueError(f"No .shp files found under {search_dir}.")
    tokens = [f"admin{adm_level}", f"adm{adm_level}", f"admbnda_adm{adm_level}", f"adm_{adm_level}"]
    for token in tokens:
        matches = [p for p in all_shp
                   if token in p.stem.lower() and not p.stem.lower().endswith("_em")]
        if matches:
            return matches[0]
    raise ValueError(f"No admin-{adm_level} shapefile found among {[p.name for p in all_shp]}.")


def extract_country_shapefile(country: str, out_dir, adm_level=1) -> Path:
    """Download (if missing) and unzip the country's HDX COD-AB boundaries.

    Returns the path of the admin-<level> .shp file.
    """
    adm_level = _adm_level_number(adm_level)
    out_dir = Path(out_dir)
    slug = f"cod-ab-{resolve_iso3(country).lower()}"
    resource = find_admin_shapefile_resource(get_dataset_resources(slug))
    local_path = download_resource_if_missing(resource, out_dir, country)

    if zipfile.is_zipfile(local_path):
        extract_dir = local_path.parent / local_path.name.split(".")[0]   # drop .shp.zip
        if not extract_dir.exists():
            with zipfile.ZipFile(local_path, "r") as zf:
                zf.extractall(extract_dir)
            print(f"[OK] Extracted to {extract_dir}")
        search_root = extract_dir
    else:
        search_root = out_dir
    return _find_admin_shp_file(search_root, adm_level)


def _detect_column(gdf, candidates) -> str:
    """First column of gdf whose lower-case name is in candidates (or None)."""
    lower = {c.lower(): c for c in gdf.columns}
    return next((lower[c] for c in candidates if c in lower), None)


def load_admin_boundary(country: str, adm_level, out_dir, source: str = "auto"):
    """Load the admin boundaries the poverty table refers to.

    source: "hdx"           HDX COD-AB (has PCodes -> exact join with the OPHI table)
            "geoboundaries" geoBoundaries API (names only)
            "auto"          HDX first, geoBoundaries if the country has no COD-AB data
    Returns (admin_gdf, info) where info = {"source", "code_col", "name_col"}.
    """
    level = _adm_level_number(adm_level)
    iso3 = resolve_iso3(country)

    if source in ("auto", "hdx"):
        try:
            shp = extract_country_shapefile(country, out_dir, adm_level=level)
            gdf = read_vector(shp)
            info = {
                "source": "hdx",
                "code_col": _detect_column(gdf, [f"adm{level}_pcode", f"admin{level}pcode"]),
                "name_col": _detect_column(gdf, [f"adm{level}_name", f"adm{level}_en",
                                                 f"adm{level}_fr", f"admin{level}name"]),
            }
            print(f"Admin boundaries: HDX COD-AB, {len(gdf)} units ({shp.name}), "
                  f"code column '{info['code_col']}', name column '{info['name_col']}'")
            return gdf, info
        except Exception as exc:  # no COD-AB dataset, or unexpected layout
            if source == "hdx":
                raise
            print(f"[INFO] HDX COD-AB boundaries not available for {country} ({exc}); "
                  "falling back to geoBoundaries.")

    out_path = Path(out_dir) / f"{iso3}_geoBoundaries_ADM{level}.shp"
    if not out_path.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        get_country_boundary(iso3, adm_level=f"ADM{level}").to_file(out_path)
    gdf = read_vector(out_path)
    info = {"source": "geoboundaries", "code_col": None, "name_col": "shapeName"}
    print(f"Admin boundaries: geoBoundaries, {len(gdf)} units ({out_path.name})")
    return gdf, info


# --- Joining poverty to boundaries ---------------------------------------------

def normalize_name(s) -> str:
    """Normalize an admin-unit name: strip accents, lowercase, collapse '-', '_' and spaces."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace("-", " ").replace("_", " ").split())


def merge_poverty_to_admin(admin_gdf: gpd.GeoDataFrame, poverty_df: pd.DataFrame,
                           poverty_value_col: str, admin_name_col: str, poverty_name_col: str,
                           admin_code_col: str = None, poverty_code_col: str = None,
                           name_aliases: dict = None, fuzzy_cutoff: float = 0.8,
                           out_col: str = "pov_hcr") -> gpd.GeoDataFrame:
    """Attach a poverty value to every admin unit, trying in order:

    1. code   — admin PCode == poverty PCode (exact; needs HDX COD-AB boundaries)
    2. alias  — name_aliases {admin name: poverty-table name}, a manual override
    3. name   — same name after removing accents, case, '-' and '_'
    4. fuzzy  — closest remaining poverty name (difflib ratio >= fuzzy_cutoff)

    The method used for each unit is stored in the 'match_method' column and
    every alias/fuzzy match is printed so it can be checked.
    """
    pov = poverty_df.copy()
    pov[out_col] = pd.to_numeric(pov[poverty_value_col], errors="coerce")
    pov["_name"] = pov[poverty_name_col].apply(normalize_name)
    by_code = (pov.set_index(pov[poverty_code_col].astype(str).str.strip()).index
               if poverty_code_col and poverty_code_col in pov else None)
    aliases = {normalize_name(k): normalize_name(v) for k, v in (name_aliases or {}).items()}

    use_code = admin_code_col is not None and by_code is not None
    matched_row, method = [], []
    used = set()
    for _, unit in admin_gdf.iterrows():
        row, how = None, None
        if use_code:
            hits = np.flatnonzero(by_code == str(unit[admin_code_col]).strip())
            if len(hits):
                row, how = hits[0], "code"
        if row is None:
            key = normalize_name(unit[admin_name_col])
            target = aliases.get(key, key)
            hits = np.flatnonzero(pov["_name"].to_numpy() == target)
            if len(hits):
                row, how = hits[0], ("alias" if key in aliases else "name")
        matched_row.append(row)
        method.append(how)
        if row is not None:
            used.add(row)

    # Fuzzy pass over what is left, one-to-one
    for i, row in enumerate(matched_row):
        if row is not None:
            continue
        key = normalize_name(admin_gdf.iloc[i][admin_name_col])
        free = {pov["_name"].iloc[j]: j for j in range(len(pov)) if j not in used}
        close = difflib.get_close_matches(key, list(free), n=1, cutoff=fuzzy_cutoff)
        if close:
            matched_row[i], method[i] = free[close[0]], "fuzzy"
            used.add(free[close[0]])

    pov_cols = [c for c in pov.columns if c != "_name" and c not in admin_gdf.columns]
    joined = pd.DataFrame([pov.iloc[r][pov_cols] if r is not None else pd.Series(index=pov_cols, dtype=object)
                           for r in matched_row]).reset_index(drop=True)
    merged = pd.concat([admin_gdf.reset_index(drop=True), joined], axis=1)
    merged[out_col] = pd.to_numeric(merged[out_col], errors="coerce")
    merged["match_method"] = method
    merged = gpd.GeoDataFrame(merged, geometry=admin_gdf.geometry.name, crs=admin_gdf.crs)

    # Report
    counts = pd.Series([m or "unmatched" for m in method]).value_counts().to_dict()
    print(f"Matched {sum(m is not None for m in method)}/{len(method)} admin units: {counts}")
    for i, m in enumerate(method):
        if m in ("alias", "fuzzy"):
            print(f"  {m:5s}: '{admin_gdf.iloc[i][admin_name_col]}' -> "
                  f"'{pov.iloc[matched_row[i]][poverty_name_col]}'  (check this)")
    unmatched = [admin_gdf.iloc[i][admin_name_col] for i, m in enumerate(method) if m is None]
    if unmatched:
        print(f"WARNING: {len(unmatched)} admin unit(s) without poverty data: {unmatched}")
        left = [pov[poverty_name_col].iloc[j] for j in range(len(pov)) if j not in used]
        print(f"  Unused poverty-table names: {left}")
        print("  -> add {boundary name: poverty-table name} pairs to NAME_ALIASES.")
    return merged


# =============================================================================
# 3. GDP data (Kummu v4)
# =============================================================================

KUMMU_V4_FILES = {
    "rast_gdpTot_1990_2024_30arcsec.tif":
        "https://zenodo.org/records/18429133/files/rast_gdpTot_1990_2024_30arcsec.tif?download=1",
    "rast_gdpTot_1990_2024_5arcmin.tif":
        "https://zenodo.org/records/18429133/files/rast_gdpTot_1990_2024_5arcmin.tif?download=1",
    "rast_gdpTot_1990_2024_30arcmin.tif":
        "https://zenodo.org/records/18429133/files/rast_gdpTot_1990_2024_30arcmin.tif?download=1",
    "rast_adm2_gdp_perCapita_1990_2024.tif":
        "https://zenodo.org/records/18429133/files/rast_adm2_gdp_perCapita_1990_2024.tif?download=1",
    "rast_adm1_gdp_perCapita_1990_2024.tif":
        "https://zenodo.org/records/18429133/files/rast_adm1_gdp_perCapita_1990_2024.tif?download=1",
    "rast_adm0_gdp_perCapita_1990_2024.tif":
        "https://zenodo.org/records/18429133/files/rast_adm0_gdp_perCapita_1990_2024.tif?download=1",
}

# Source file for each GDP variant
_VARIANT_FILES = {
    "total":     "rast_gdpTot_1990_2024_30arcsec.tif",
    "percapita": "rast_adm0_gdp_perCapita_1990_2024.tif",
}


def download_file(url: str, out_dir: str, filename: str = None,
                  chunk_size: int = 1 << 20, overwrite: bool = False) -> str:
    """Stream-download a (large) file; skip if already on disk. Returns the local path."""
    if filename is None:
        filename = url.split("/")[-1].split("?")[0]
    out_path = os.path.join(out_dir, filename)

    if os.path.exists(out_path) and not overwrite:
        print(f"Already exists, skipping download: {out_path}")
        return out_path

    os.makedirs(out_dir, exist_ok=True)
    print(f"Downloading {url}\n  -> {out_path}")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded/1e6:,.0f} MB / {total/1e6:,.0f} MB "
                          f"({downloaded / total * 100:5.1f}%)", end="", flush=True)
    print("\nDownload complete.")
    return out_path


def extract_country_gdp(country: str, year: int, gdp_path: str, out_path: str = None,
                        tif_path: str = None, adm_level: str = "ADM0",
                        band_years=(1990, 1995, 2000, 2005, 2010, 2015, 2020, 2024),
                        pad_deg: float = 0.05, variant: str = "total",
                        mask_zero: bool = True, zero_value: float = 0):
    """Clip one year of the Kummu v4 GDP raster to a country and save it.

    Reads only the country's bounding-box window from disk, so it works on the
    1.4 GB global file with little memory.

    country:  name or ISO3 code
    year:     must be one of band_years (or of the years stored in the band tags)
    variant:  "total" (GDP per pixel) or "percapita" (GDP per capita per pixel)
    Returns the clipped xarray DataArray.
    """
    if variant not in _VARIANT_FILES:
        raise ValueError(f"variant must be one of {list(_VARIANT_FILES)}, got '{variant}'")
    if tif_path is None:
        tif_path = os.path.join(gdp_path, _VARIANT_FILES[variant])

    iso3 = resolve_iso3(country)
    boundary_gdf = get_country_boundary(iso3, adm_level=adm_level)

    if out_path is None:
        suffix = "PerCapita" if variant == "percapita" else "PPP"
        out_path = os.path.join(gdp_path, f"GDP_{suffix}_{year}_{iso3}.tif")

    with rasterio.open(tif_path) as src:
        raster_crs = src.crs
    if boundary_gdf.crs != raster_crs:
        boundary_gdf = boundary_gdf.to_crs(raster_crs)

    minx, miny, maxx, maxy = boundary_gdf.total_bounds
    minx, miny, maxx, maxy = minx - pad_deg, miny - pad_deg, maxx + pad_deg, maxy + pad_deg

    with rasterio.open(tif_path) as src:
        n_bands = src.count
        tag_years = []
        for b in range(1, n_bands + 1):
            tags = src.tags(b)
            y = tags.get("year") or tags.get("YEAR") or tags.get("DATE") or tags.get("DESCRIPTION")
            tag_years.append(int(y) if y and str(y).strip().isdigit() else None)
        if all(y is not None for y in tag_years) and len(tag_years) == n_bands:
            band_years = tag_years
        band_years = list(band_years)
        if len(band_years) != n_bands:
            raise ValueError(f"band_years has {len(band_years)} entries but file has {n_bands} bands.")
        if year not in band_years:
            raise ValueError(f"Year {year} not available. Bands correspond to: {band_years}")

        band_idx = band_years.index(year) + 1
        window = from_bounds(minx, miny, maxx, maxy, transform=src.transform)
        window = window.round_offsets().round_lengths()
        data = src.read(band_idx, window=window)
        window_transform = src.window_transform(window)
        nodata = src.nodata

    data = data.astype("float32")
    if mask_zero:
        data[data == zero_value] = np.nan
    nodata_out = np.nan if (mask_zero or nodata is None) else nodata

    height, width = data.shape
    xs = window_transform.c + window_transform.a * (np.arange(width) + 0.5)
    ys = window_transform.f + window_transform.e * (np.arange(height) + 0.5)

    da_year = xr.DataArray(data, dims=("y", "x"), coords={"y": ys, "x": xs}, name="gdp")
    da_year = da_year.rio.write_crs(raster_crs, inplace=True)
    da_year = da_year.rio.write_nodata(nodata_out, inplace=True)
    da_clipped = da_year.rio.clip(boundary_gdf.geometry, boundary_gdf.crs, drop=True)

    vals = da_clipped.values
    n_valid = int(np.isfinite(vals).sum())
    if variant == "percapita":
        print(f"{country} ({iso3}) {year}: {n_valid} valid pixels, "
              f"GDP/capita mean={float(np.nanmean(vals)):,.0f}, median={float(np.nanmedian(vals)):,.0f}")
    else:
        print(f"{country} ({iso3}) {year}: {n_valid} valid pixels, total GDP={float(np.nansum(vals)):,.0f}")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    da_clipped.rio.to_raster(out_path)
    print("Saved:", out_path)
    return da_clipped


def get_country_gdp(country: str, year: int, gdp_path: str, out_path: str,
                    keep_global: bool = True,
                    global_file: str = "rast_gdpTot_1990_2024_30arcsec.tif"):
    """Return the country's GDP raster for `year`, doing as little work as possible.

    - If out_path already exists, it is loaded and nothing is downloaded.
    - Otherwise the global Kummu v4 raster (1.4 GB) is downloaded if missing,
      the country is clipped out and saved to out_path.
    - keep_global=False deletes the global raster after clipping, keeping only the
      country file. Keep it (True) if you will run other countries or years,
      or it will have to be downloaded again.
    """
    global_path = os.path.join(gdp_path, global_file)

    if os.path.exists(out_path):
        print(f"[SKIP] Country GDP raster already exists, no download needed: {out_path}")
        da = rioxarray.open_rasterio(out_path, masked=True).squeeze("band", drop=True)
    else:
        download_file(url=KUMMU_V4_FILES[global_file], out_dir=gdp_path)
        da = extract_country_gdp(country, year, gdp_path=gdp_path, out_path=out_path,
                                 tif_path=global_path)

    if not keep_global and os.path.exists(global_path):
        os.remove(global_path)
        print(f"[OK] Deleted the global GDP raster to free 1.4 GB: {global_path}")
    return da


# =============================================================================
# 4. Raster helpers
# =============================================================================

def rasterize_vector(inD, outFile, field, res=0.1, dtype="float32"):
    """Burn a GeoDataFrame column into a GeoTIFF.

    inD:   GeoDataFrame (polygons or points), in a CRS whose units match `res`
    field: column to rasterize
    res:   pixel size in inD's CRS units (metres for a projected CRS)
    """
    bounds = inD.total_bounds
    width = math.ceil((bounds[2] - bounds[0]) / res)
    height = math.ceil((bounds[3] - bounds[1]) / res)

    cAffine = affine.Affine(res, 0, bounds[0], 0, res * -1, bounds[3])
    cMeta = {"count": 1, "crs": inD.crs, "dtype": dtype, "affine": cAffine,
             "driver": "GTiff", "transform": cAffine,
             "height": height, "width": width, "nodata": 0}
    inD = inD.sort_values(by=[field], ascending=False)
    shapes = ((row.geometry, row[field]) for idx, row in inD.iterrows())
    with rasterio.open(outFile, "w", **cMeta) as out:
        burned = features.rasterize(shapes=shapes, fill=0, all_touched=True,
                                    out_shape=(cMeta["height"], cMeta["width"]),
                                    transform=out.transform,
                                    merge_alg=rasterio.enums.MergeAlg.replace)
        out.write_band(1, burned.astype(cMeta["dtype"]))


def reproj(input_raster, output_raster, new_crs, factor=1):
    """Reproject a raster to new_crs (nearest neighbour). factor scales the pixel count."""
    with rasterio.open(input_raster) as src:
        transform, width, height = calculate_default_transform(
            src.crs, new_crs, src.width * factor, src.height * factor, *src.bounds)
        kwargs = src.meta.copy()
        kwargs.update({"crs": new_crs, "transform": transform, "width": width, "height": height})

        with rasterio.open(output_raster, "w", **kwargs) as dst:
            for i in range(1, src.count + 1):
                reproject(source=rasterio.band(src, i), destination=rasterio.band(dst, i),
                          src_transform=src.transform, src_crs=src.crs,
                          dst_transform=transform, dst_crs=new_crs,
                          resampling=Resampling.nearest)


def _pixel_offset_to_coord(raster, x_offset, y_offset):
    gt = raster.GetGeoTransform()
    return gt[0] + gt[1] * x_offset, gt[3] + gt[5] * y_offset


def raster_to_point_shp(rasterfn, outSHPfn, band_no=1):
    """Write every positive pixel of a raster as a point (field VALUE) to a shapefile.

    The points are used to compute classification bins (Step 2) and as the
    base point layer of Option 2 (Step 3). Note: VALUE is an integer field.
    """
    raster = gdal.Open(rasterfn)
    array = raster.GetRasterBand(band_no).ReadAsArray()

    srs = osr.SpatialReference()
    srs.ImportFromWkt(raster.GetProjection())

    shpDriver = ogr.GetDriverByName("ESRI Shapefile")
    if os.path.exists(outSHPfn):
        shpDriver.DeleteDataSource(outSHPfn)
    outDataSource = shpDriver.CreateDataSource(outSHPfn)
    outLayer = outDataSource.CreateLayer(outSHPfn, geom_type=ogr.wkbPoint, srs=srs)
    featureDefn = outLayer.GetLayerDefn()
    outLayer.CreateField(ogr.FieldDefn("VALUE", ogr.OFTInteger))

    point = ogr.Geometry(ogr.wkbPoint)
    row_count = array.shape[0]
    for ridx, row in enumerate(array):
        if ridx % 100 == 0:
            print(f"{ridx} of {row_count} rows processed")
        for cidx, value in enumerate(row):
            if value > 0:
                x, y = _pixel_offset_to_coord(raster, cidx, ridx)
                point.AddPoint(x, y)
                outFeature = ogr.Feature(featureDefn)
                outFeature.SetGeometry(point)
                outFeature.SetField("VALUE", float(value))
                outLayer.CreateFeature(outFeature)
                outFeature.Destroy()
    outDataSource = None  # flush to disk


def reclassify_raster(in_raster, out_raster, bins, lookup, mask_nan=False):
    """Re-classify a raster into classes using bin edges and a lookup table.

    Each pixel first gets the number of bin edges it is below (0..len(bins)),
    then `lookup` [(from, to), ...] remaps those counts to the final classes.
    mask_nan: if True, NaN pixels are written as 0 and 0 is set as nodata.
    """
    with rasterio.open(in_raster) as src:
        grey = np.mean(np.dstack(src.read()), axis=2)
        profile = src.profile.copy()

    classified = np.sum(np.dstack([(grey < b) for b in bins]), axis=2)

    lookup = np.asarray(lookup)
    replacer = np.arange(classified.max() + 1)
    replacer[lookup[:, 0]] = lookup[:, 1]
    classified = replacer[classified]

    if mask_nan:
        classified = np.where(~np.isnan(grey), classified, 0)
    classified = classified.reshape(1, grey.shape[0], grey.shape[1]).astype(np.int32)

    profile.update(count=1, nodata=0 if mask_nan else None, dtype=classified.dtype)
    with rasterio.open(out_raster, "w", **profile) as dst:
        dst.write(classified)


def clip_raster(in_raster, out_raster, shapes):
    """Clip a raster to the given geometries (list of GeoJSON-like dicts)."""
    with rasterio.open(in_raster) as src:
        out_image, out_transform = rasterio.mask.mask(src, shapes, crop=True)
        out_meta = src.meta
    out_meta.update({"driver": "GTiff", "height": out_image.shape[1],
                     "width": out_image.shape[2], "transform": out_transform})
    with rasterio.open(out_raster, "w", **out_meta) as dest:
        dest.write(out_image)


# =============================================================================
# 5. Step 3 — combine the classified layers
# =============================================================================

def combine_rasters(pov_raster, gdp_raster, out_raster, weight_pov=0.5, weight_gdp=0.5):
    """Option 1: weighted sum of two classified rasters on a common grid.

    The output grid uses the poverty raster's resolution and covers the overlap of
    the two rasters; the GDP raster is resampled onto it (nearest neighbour,
    since the values are discrete classes). Pixels where either layer is 0 are set to 0.
    """
    with rasterio.open(pov_raster) as raster1, rasterio.open(gdp_raster) as raster2:
        minx, miny, maxx, maxy = box(*raster1.bounds).intersection(box(*raster2.bounds)).bounds

        res = raster1.res[0]
        width = int((maxx - minx) / res)
        height = int((maxy - miny) / res)
        out_transform = affine.Affine(res, 0.0, minx, 0.0, -res, maxy)

        window1 = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=raster1.transform)
        arr1 = raster1.read(1, window=window1, out_shape=(height, width), resampling=Resampling.nearest)

        arr2 = np.zeros((height, width), dtype=raster2.dtypes[0])
        reproject(source=rasterio.band(raster2, 1), destination=arr2,
                  src_transform=raster2.transform, src_crs=raster2.crs,
                  dst_transform=out_transform, dst_crs=raster1.crs,
                  resampling=Resampling.nearest)

        valid_mask = (arr1 != 0) & (arr2 != 0)
        arr_sum = (arr1 * weight_pov) + (arr2 * weight_gdp)
        arr_sum = np.where(valid_mask, arr_sum, 0).astype("float32")

        out_profile = raster1.profile.copy()
        out_profile.update(height=height, width=width, transform=out_transform,
                           count=1, nodata=0, dtype="float32")
        with rasterio.open(out_raster, "w", **out_profile) as dst:
            dst.write(arr_sum, 1)


def extract_raster_values_to_points(points_gdf, raster_paths: dict, fill_value=None) -> pd.DataFrame:
    """Option 2: sample rasters at point locations.

    points_gdf:   GeoDataFrame of points (same CRS as the rasters)
    raster_paths: {column_name: raster_path}
    fill_value:   value for points that fall outside a raster (default NaN)
    Returns a DataFrame with one column per raster, aligned with points_gdf.
    """
    xs = points_gdf.geometry.x.to_numpy()
    ys = points_gdf.geometry.y.to_numpy()
    out = pd.DataFrame(index=points_gdf.index)
    for col, path in raster_paths.items():
        print(f"Extracting {os.path.basename(path)} values -> '{col}'...")
        ds = gdal.Open(path)
        gt = ds.GetGeoTransform()
        arr = ds.GetRasterBand(1).ReadAsArray()
        # map -> pixel coordinates (geotransforms without rotation)
        px = ((xs - gt[0]) / gt[1]).astype(int)
        py = ((ys - gt[3]) / gt[5]).astype(int)
        inside = (px >= 0) & (px < arr.shape[1]) & (py >= 0) & (py < arr.shape[0])
        vals = np.full(len(xs), np.nan if fill_value is None else fill_value, dtype="float64")
        vals[inside] = arr[py[inside], px[inside]]
        out[col] = vals
    return out


# =============================================================================
# 6. Step 4 — CREDIT layer
# =============================================================================

def credit_from_index(index_values, mtf_x, mtf_y):
    """Convert the 0-5 combined index into kWh/capita/year by linear interpolation
    between the MTF tier targets (mtf_x = index breakpoints, mtf_y = demand)."""
    return scipy.interpolate.interp1d(mtf_x, mtf_y)(index_values)


def utm_crs_for(gdf: gpd.GeoDataFrame) -> str:
    """Suggested projected CRS (UTM zone of the layer's centroid), e.g. 'EPSG:32734' for DRC."""
    return gdf.estimate_utm_crs().to_string()
