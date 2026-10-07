"""
Convierte los productos de dron (GeoTIFF / LAS) de data/drone en recursos livianos
para el explorador web (data_web/).

Uso:
    python scripts/preprocess.py            # procesa lo que falte (usa caché)
    python scripts/preprocess.py --force    # vuelve a leer todos los rásters
    python scripts/preprocess.py --skip-las # omite las nubes de puntos

Fase 1 lee cada ráster decimado, lo reproyecta a EPSG:3857 y lo guarda en .cache/.
Fase 2 calcula rangos globales por capa, colorea y escribe imágenes, grillas y manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from matplotlib import colormaps
from matplotlib.colors import LinearSegmentedColormap
from PIL import Image
from rasterio.enums import ColorInterp, Resampling
from rasterio.features import shapes
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject, transform as warp_transform
from rasterio.windows import Window, from_bounds

from tiles import build_layer_tiles

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DRONE = DATA / "drone"
OUT = ROOT / "data_web"
CACHE = ROOT / ".cache"
WEB_CRS = "EPSG:3857"

MAX_PX_RGB = 6144
MAX_PX_SCALAR = 4096
GRID_PX = 512
HIST_BINS = 48
LAS_TARGET_POINTS = 2_000_000
TILE_WORKERS = 6

Image.MAX_IMAGE_PIXELS = None


TERRAIN = LinearSegmentedColormap.from_list(
    "fndr_terrain", ["#2f6b3a", "#8fbf6a", "#e9dc9c", "#c49a6c", "#8a6248", "#f4f1ec"]
)

MISSIONS = {
    "multi": {"label": "Multiespectral", "icon": "bi-layers-half"},
    "termico": {"label": "Térmico", "icon": "bi-thermometer-half"},
    "lidar": {"label": "LiDAR", "icon": "bi-box"},
}

CATALOG = {
    "rgb": {
        "label": "RGB", "group": "color", "mission": "multi", "unit": "",
        "description": "Ortomosaico en color verdadero: la fotografía de alta resolución del vuelo, tal como se vería desde el aire. Sirve de referencia para ubicarse y comparar con los índices.",
    },
    "ndvi": {
        "label": "NDVI", "group": "indices", "mission": "multi", "unit": "", "cmap": "RdYlGn",
        "description": "Índice de vegetación de diferencia normalizada. Valores altos (verde) indican vegetación densa y vigorosa; valores bajos (rojo) suelo desnudo, superficies construidas o vegetación estresada.",
    },
    "ndwi": {
        "label": "NDWI", "group": "indices", "mission": "multi", "unit": "", "cmap": "RdYlBu",
        "description": "Índice de agua de diferencia normalizada. Resalta cuerpos de agua y superficies húmedas (azul) frente a suelos secos y vegetación (rojo-amarillo).",
    },
    "ndci": {
        "label": "NDCI", "group": "indices", "mission": "multi", "unit": "", "cmap": "viridis",
        "description": "Índice de clorofila de diferencia normalizada (borde rojo). Sensible al contenido de clorofila y pigmentos; útil para detectar cambios fisiológicos en la vegetación.",
    },
    "ndsi": {
        "label": "NDSI", "group": "indices", "mission": "multi", "unit": "", "cmap": "BrBG_r",
        "description": "Índice de diferencia normalizada orientado a resaltar suelo expuesto y superficies salinas frente a la cobertura vegetal.",
    },
    "rgb_t": {
        "label": "RGB", "group": "color", "mission": "termico", "unit": "",
        "description": "Ortomosaico RGB capturado junto con la cámara térmica, útil como referencia visual del vuelo térmico.",
    },
    "thermal": {
        "label": "Temperatura", "group": "termico", "mission": "termico", "unit": "°C", "cmap": "inferno",
        "description": "Temperatura de superficie estimada con la cámara térmica del dron. Permite ver contrastes entre agua, suelo, vegetación y estructuras.",
    },
    "dom": {
        "label": "RGB", "group": "color", "mission": "lidar", "unit": "",
        "description": "Ortomosaico RGB capturado durante el vuelo LiDAR. Sirve de referencia visual para el relieve y la nube de puntos.",
    },
    "dem": {
        "label": "Relieve (MDE)", "group": "relieve", "mission": "lidar", "unit": "m", "cmap": TERRAIN,
        "description": "Modelo digital de elevación con sombreado de relieve, derivado del vuelo LiDAR. Muestra la altitud del terreno en metros.",
    },
    "lidar": {
        "label": "Nube 3D", "group": "3d", "mission": "lidar", "unit": "m",
        "description": "Nube de puntos LiDAR en 3D. Puedes rotarla, acercarla y colorearla por color real, elevación, intensidad o clasificación.",
    },
}
LAYER_ORDER = ["rgb", "ndvi", "ndwi", "ndci", "ndsi", "rgb_t", "thermal", "dom", "dem", "lidar"]
RGB_LAYERS = {"rgb", "rgb_t", "dom"}
INDEX_LAYERS = {"ndvi", "ndwi", "ndci", "ndsi"}
VALID_RANGE = {
    **{k: (-1.0, 1.0) for k in INDEX_LAYERS},
    "thermal": (-40.0, 120.0),
    "dem": (-500.0, 7000.0),
}
PER_FLIGHT_RANGE = {"dem"}
ZCAP = {"rgb": 20, "dom": 20, "rgb_t": 20, "thermal": 19, "dem": 18, **{k: 19 for k in INDEX_LAYERS}}
EDGE_BUFFER_M = 2.0
EDGE_SMOOTH_M = 10.0
EDGE_HOLE_M = 50.0
SUFFIX_RE = re.compile(r"_(rgb_t|rgb|ndvi|ndwi|ndci|ndsi|thermal)\.tif$", re.I)
FOLDER_RE = re.compile(r"^(?P<sensor>[A-Z0-9]+)_(?P<site>[A-Z0-9]+)_(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})$")
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def discover():
    flights = []
    for d in sorted(p for p in DRONE.iterdir() if p.is_dir()):
        layers = {}
        for f in d.glob("*.tif"):
            m = SUFFIX_RE.search(f.name)
            if m:
                layers[m.group(1).lower()] = f
            elif f.name.lower() in ("dem.tif", "dom.tif"):
                layers[f.stem.lower()] = f
        las = sorted(d.glob("*.las"), key=lambda p: p.stat().st_size, reverse=True)
        flights.append({"id": d.name, "dir": d, "layers": layers, "las": las[0] if las else None})
    return flights


def decimated_shape(h, w, max_px):
    f = max(h, w) / max_px
    if f <= 1:
        return h, w
    return max(1, round(h / f)), max(1, round(w / f))


def crop_valid(arr, mask, transform):
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0:
        return arr, mask, transform
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    t = transform * transform.translation(c0, r0)
    return arr[..., r0:r1, c0:c1], mask[r0:r1, c0:c1], t


def read_window(src, ref_bounds):
    full = Window(0, 0, src.width, src.height)
    if ref_bounds is None:
        return full
    l, b, r, t = ref_bounds
    pad = 0.02 * max(r - l, t - b)
    w = from_bounds(l - pad, b - pad, r + pad, t + pad, transform=src.transform)
    w = w.round_offsets().round_lengths().intersection(full)
    return w if w.width * w.height < 0.8 * src.width * src.height else full


def to_web(arr, src_transform, src_crs, resampling, nodata):
    h, w = arr.shape[-2:]
    dst_t, dw, dh = calculate_default_transform(src_crs, WEB_CRS, w, h, *array_bounds(h, w, src_transform))
    bands = arr if arr.ndim == 3 else arr[None]
    out = np.full((bands.shape[0], dh, dw), nodata, dtype=bands.dtype)
    for i in range(bands.shape[0]):
        reproject(bands[i], out[i], src_transform=src_transform, src_crs=src_crs,
                  dst_transform=dst_t, dst_crs=WEB_CRS, resampling=resampling,
                  src_nodata=nodata, dst_nodata=nodata)
    return (out if arr.ndim == 3 else out[0]), dst_t


def source_sig(path):
    st = Path(path).stat()
    return np.array([st.st_size, int(st.st_mtime)], dtype=np.int64)


def cache_fresh(cache, path):
    if not cache.exists():
        return False
    with np.load(cache) as z:
        return "src" in z.files and np.array_equal(z["src"], source_sig(path))


def phase1_layer(flight_id, kind, path, ref_bounds, force):
    cache = CACHE / flight_id / f"{kind}.npz"
    if not force and cache_fresh(cache, path):
        return kind, "cache"
    cache.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with rasterio.open(path) as src:
        win = read_window(src, ref_bounds if kind in INDEX_LAYERS else None)
        max_px = MAX_PX_RGB if kind in RGB_LAYERS else MAX_PX_SCALAR
        oh, ow = decimated_shape(int(win.height), int(win.width), max_px)
        tr = src.window_transform(win) * rasterio.Affine.scale(win.width / ow, win.height / oh)
        px_area = abs(tr.a * tr.e)
        if kind in RGB_LAYERS:
            data = src.read([1, 2, 3], window=win, out_shape=(3, oh, ow), resampling=Resampling.average)
            alpha_idx = [i + 1 for i, (d, ci) in enumerate(zip(src.descriptions, src.colorinterp))
                         if i >= 3 and ((d or "").lower() == "alpha" or ci == ColorInterp.alpha)]
            if alpha_idx:
                valid = src.read(alpha_idx[0], window=win, out_shape=(oh, ow), resampling=Resampling.nearest) > 0
            else:
                masks = src.read_masks([1, 2, 3], window=win, out_shape=(3, oh, ow), resampling=Resampling.nearest)
                valid = masks.max(axis=0) > 0
            if data.dtype != np.uint8:
                rgb = np.zeros(data.shape, np.uint8)
                for i in range(3):
                    v = data[i][valid]
                    lo, hi = np.percentile(v, [1, 99]) if v.size else (0, 1)
                    rgb[i] = np.clip((data[i].astype(np.float32) - lo) / max(hi - lo, 1e-6) * 255, 0, 255)
                data = rgb
            else:
                valid &= data.max(axis=0) > 0
            rgba = np.concatenate([data, (valid * 255).astype(np.uint8)[None]], axis=0)
            rgba[:3, ~valid] = 0
            web, wt = to_web(rgba, tr, src.crs, Resampling.nearest, 0)
            wmask = web[3] > 0
            web, wmask, wt = crop_valid(web, wmask, wt)
            np.savez(cache, arr=web, transform=np.array(wt)[:6], area_ha=valid.sum() * px_area / 1e4,
                     utm_bounds=np.array(array_bounds(oh, ow, tr)), src=source_sig(path))
        else:
            a = src.read(1, window=win, out_shape=(oh, ow), resampling=Resampling.average, masked=True)
            arr = a.filled(np.nan).astype(np.float32)
            lo, hi = VALID_RANGE[kind]
            arr[(arr < lo) | (arr > hi)] = np.nan
            web, wt = to_web(arr, tr, src.crs, Resampling.bilinear, np.nan)
            wmask = np.isfinite(web)
            web, wmask, wt = crop_valid(web, wmask, wt)
            store = web if kind == "dem" else web.astype(np.float16)
            np.savez_compressed(cache, arr=store, transform=np.array(wt)[:6],
                                area_ha=np.isfinite(arr).sum() * px_area / 1e4, src=source_sig(path))
    log(f"  {flight_id}/{kind}: {ow}x{oh} leído en {time.time() - t0:.0f}s")
    return kind, "ok"


def phase1(flights, force, workers):
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fl in flights:
            ref = None
            if "rgb" in fl["layers"]:
                with rasterio.open(fl["layers"]["rgb"]) as s:
                    ref = tuple(s.bounds)
            for kind, path in fl["layers"].items():
                jobs.append(ex.submit(phase1_layer, fl["id"], kind, path, ref, force))
        for j in as_completed(jobs):
            j.result()


def load_cache(flight_id, kind):
    z = np.load(CACHE / flight_id / f"{kind}.npz")
    t = rasterio.Affine(*z["transform"])
    return z["arr"], t, float(z["area_ha"])


def remove_canvas(rgba):
    """Quita el lienzo blanco que rodea algunos ortomosaicos (blanco conectado al exterior).
    Solo aplica a fuentes uint8: en las de 16 bits el estiramiento satura las luces a 255."""
    from scipy import ndimage

    alpha = rgba[3] > 0
    white = alpha & (rgba[:3].min(axis=0) >= 250)
    if white.mean() < 0.02:
        return alpha
    lab, _ = ndimage.label(white | ~alpha)
    border = np.zeros_like(alpha)
    border[[0, -1], :] = border[:, [0, -1]] = True
    outside = np.unique(lab[(~alpha) | border])
    canvas = np.isin(lab, outside[outside > 0]) & white
    return alpha & ~canvas


def clean_edges(mask, transform):
    """Recorta EDGE_BUFFER_M desde el borde exterior, suaviza el contorno (radio
    ≈ EDGE_SMOOTH_M) y elimina manchones sueltos. Los huecos interiores no se modifican."""
    from scipy import ndimage

    if not mask.any():
        return mask
    h, w = mask.shape
    b = web_bounds_latlon(transform, h, w)
    k = math.cos(math.radians((b[0][0] + b[1][0]) / 2))
    sampling = (abs(transform.e) * k, abs(transform.a) * k)
    filled = ndimage.binary_fill_holes(mask)
    holes = filled & ~mask
    pad = int(math.ceil(3 * EDGE_SMOOTH_M / min(sampling) + EDGE_BUFFER_M / min(sampling))) + 2
    m = np.pad(filled, pad)
    dist = ndimage.distance_transform_edt(m, sampling=sampling)
    if holes.any():
        lab, _ = ndimage.label(np.pad(holes, pad))
        near = np.unique(lab[(lab > 0) & (dist < EDGE_HOLE_M)])
        if near.size:
            m &= ~np.isin(lab, near)
            dist = ndimage.distance_transform_edt(m, sampling=sampling)
    core = dist > EDGE_SMOOTH_M + EDGE_BUFFER_M
    opened = ndimage.distance_transform_edt(~core, sampling=sampling) <= EDGE_SMOOTH_M
    sigma = (0.3 * EDGE_SMOOTH_M / sampling[0], 0.3 * EDGE_SMOOTH_M / sampling[1])
    out = (ndimage.gaussian_filter(opened.astype(np.float32), sigma) > 0.5) & (dist > EDGE_BUFFER_M * 0.5)
    out = out[pad:-pad, pad:-pad]
    lab, n = ndimage.label(out)
    if n > 1:
        sizes = np.bincount(lab.ravel())[1:]
        keep = np.flatnonzero(sizes >= 0.01 * sizes.max()) + 1
        out = np.isin(lab, keep)
    return out & ~holes


def web_bounds_latlon(transform, h, w):
    l, b, r, t = array_bounds(h, w, transform)
    xs, ys = warp_transform(WEB_CRS, "EPSG:4326", [l, r], [b, t])
    return [[round(ys[0], 7), round(xs[0], 7)], [round(ys[1], 7), round(xs[1], 7)]]


def nice_range(lo, hi, kind):
    if kind in INDEX_LAYERS:
        return math.floor(lo * 20) / 20, math.ceil(hi * 20) / 20
    if kind in ("thermal", "dem"):
        return math.floor(lo), math.ceil(hi)
    return round(lo, 3), round(hi, 3)


def css_gradient(cmap, n=9):
    stops = []
    for i in range(n):
        r, g, b, _ = cmap(i / (n - 1))
        stops.append(f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)}) {round(i / (n - 1) * 100)}%")
    return "linear-gradient(to top, " + ", ".join(stops) + ")"


def get_cmap(kind):
    c = CATALOG[kind]["cmap"]
    return colormaps[c] if isinstance(c, str) else c


def hillshade(dem, transform, lat):
    dx = abs(transform.a) * math.cos(math.radians(lat))
    dy = abs(transform.e) * math.cos(math.radians(lat))
    z = np.where(np.isfinite(dem), dem, np.nanmean(dem))
    gy, gx = np.gradient(z, dy, dx)
    slope = np.arctan(np.hypot(gx, gy) * 1.5)
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(315), math.radians(45)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(hs, 0, 1)


def value_grid(arr, transform):
    h, w = arr.shape
    f = max(1, math.ceil(max(h, w) / GRID_PX))
    hh, ww = h // f, w // f
    if hh == 0 or ww == 0:
        f, hh, ww = 1, h, w
    blk = arr[: hh * f, : ww * f].astype(np.float32).reshape(hh, f, ww, f)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        g = np.nanmean(blk, axis=(1, 3))
    gt = transform * transform.scale(f, f)
    return g, gt


def footprint(mask, transform):
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as shp_transform, unary_union
    from pyproj import Transformer

    f = max(1, math.ceil(max(mask.shape) / 400))
    small = mask[::f, ::f].astype(np.uint8)
    st = transform * transform.scale(f, f)
    polys = [shape(g) for g, v in shapes(small, mask=small > 0, transform=st) if v == 1]
    if not polys:
        return None
    geom = unary_union(polys).buffer(abs(st.a) * 1.5).buffer(-abs(st.a) * 1.5).simplify(abs(st.a) * 1.5)
    tf = Transformer.from_crs(WEB_CRS, "EPSG:4326", always_xy=True).transform
    return mapping(shp_transform(tf, geom))


def process_las(flight_id, path, force):
    import laspy

    out = OUT / "flights" / flight_id / "pointcloud.bin"
    meta_p = CACHE / flight_id / "pointcloud.json"
    sig = [int(v) for v in source_sig(path)]
    if out.exists() and meta_p.exists() and not force:
        cached = json.loads(meta_p.read_text("utf-8"))
        if cached.get("src") == sig and cached.get("target") == LAS_TARGET_POINTS:
            return cached
    t0 = time.time()
    rng = np.random.default_rng(42)
    with laspy.open(path) as f:
        total = f.header.point_count
        p = min(1.0, LAS_TARGET_POINTS * 1.05 / total)
        dims = set(f.header.point_format.dimension_names)
        has_rgb = {"red", "green", "blue"} <= dims
        parts = {k: [] for k in ("x", "y", "z", "i", "c", "r", "g", "b")}
        for chunk in f.chunk_iterator(4_000_000):
            sel = rng.random(len(chunk)) < p
            if not sel.any():
                continue
            parts["x"].append(np.asarray(chunk.x)[sel])
            parts["y"].append(np.asarray(chunk.y)[sel])
            parts["z"].append(np.asarray(chunk.z)[sel])
            parts["i"].append(np.asarray(chunk.intensity)[sel])
            parts["c"].append(np.asarray(chunk.classification)[sel])
            if has_rgb:
                parts["r"].append(np.asarray(chunk.red)[sel])
                parts["g"].append(np.asarray(chunk.green)[sel])
                parts["b"].append(np.asarray(chunk.blue)[sel])
    a = {k: np.concatenate(v) if v else np.array([]) for k, v in parts.items()}
    n = min(len(a["x"]), LAS_TARGET_POINTS)
    idx = rng.permutation(len(a["x"]))[:n]
    x, y, z = a["x"][idx], a["y"][idx], a["z"][idx]
    zlo, zhi = np.percentile(z, [0.5, 99.5])
    keep = (z >= zlo - 5) & (z <= zhi + 5)
    idx, x, y, z = idx[keep], x[keep], y[keep], z[keep]
    n = len(x)
    mins = np.array([x.min(), y.min(), z.min()])
    maxs = np.array([x.max(), y.max(), z.max()])
    span = np.maximum(maxs - mins, 1e-6)
    q = np.round((np.stack([x, y, z], 1) - mins) / span * 65535).astype("<u2")
    inten = a["i"][idx].astype(np.float32)
    ilo, ihi = np.percentile(inten, [1, 99])
    inten8 = np.clip((inten - ilo) / max(ihi - ilo, 1) * 255, 0, 255).astype(np.uint8)
    if has_rgb:
        rgb = np.stack([a["r"][idx], a["g"][idx], a["b"][idx]], 1).astype(np.float32)
        if rgb.max() > 255:
            rgb /= 256.0
        rgb8 = np.clip(rgb, 0, 255).astype(np.uint8)
        has_rgb = bool(rgb8.max() > 0)
    else:
        rgb8 = np.zeros((n, 3), np.uint8)
    cls = a["c"][idx].astype(np.uint8)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(q.tobytes())
        fh.write(rgb8.tobytes())
        fh.write(inten8.tobytes())
        fh.write(cls.tobytes())
    xs, ys = warp_transform("EPSG:32719", "EPSG:4326", [float(x.mean())], [float(y.mean())])
    classes, counts = np.unique(cls, return_counts=True)
    meta = {
        "path": f"flights/{flight_id}/pointcloud.bin",
        "count": int(n),
        "total_points": int(total),
        "min": [round(float(v), 3) for v in mins],
        "max": [round(float(v), 3) for v in maxs],
        "has_rgb": has_rgb,
        "classes": {int(c): int(k) for c, k in zip(classes, counts)},
        "center_latlon": [round(ys[0], 6), round(xs[0], 6)],
        "layout": "u16 xyz[n*3] | u8 rgb[n*3] | u8 intensity[n] | u8 class[n]",
        "src": sig,
        "target": LAS_TARGET_POINTS,
    }
    meta_p.parent.mkdir(parents=True, exist_ok=True)
    meta_p.write_text(json.dumps(meta), "utf-8")
    log(f"  {flight_id}/LiDAR: {n:,} de {total:,} puntos en {time.time() - t0:.0f}s")
    return meta


def export_comunas(flight_bboxes):
    import geopandas as gpd
    from shapely.geometry import box

    shp = DATA / "vector" / "comunas" / "comunas.shp"
    if not shp.exists():
        return None
    g = gpd.read_file(shp).to_crs(4326)
    g = g[g["REGION"].str.contains("Valpara", na=False)]
    g = g[~g["PROV"].str.contains("Isla de Pascua", na=False)]
    hits = set()
    for b in flight_bboxes:
        hits |= set(g[g.intersects(box(*b))]["NOM_COM"])
    g = g.assign(con_vuelo=g["NOM_COM"].isin(hits))
    g["geometry"] = g.geometry.simplify(0.0008, preserve_topology=True)
    out = OUT / "vector" / "comunas.geojson"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(g[["NOM_COM", "PROV", "CODIGO", "con_vuelo", "geometry"]].to_json(drop_id=True), "utf-8")
    return "vector/comunas.geojson"


def make_colorizer(cmap, vmin, vmax):
    lut = (cmap(np.linspace(0, 1, 256)) * 255).astype(np.uint8)
    span = max(vmax - vmin, 1e-9)

    def colorize(v):
        idx = np.nan_to_num(np.clip((v - vmin) / span * 255, 0, 255)).astype(np.uint8)
        return lut[idx]

    return colorize


def mask_signature(mask, *extra):
    h = hashlib.md5(np.packbits(mask).tobytes())
    h.update(repr((mask.shape, EDGE_BUFFER_M, EDGE_SMOOTH_M, EDGE_HOLE_M) + extra).encode())
    return h.hexdigest()


def set_extent(rec, rgba, bounds, area_ha, mask, transform, base):
    th = Image.fromarray(np.ascontiguousarray(rgba), "RGBA")
    th.thumbnail((900, 900))
    bg = Image.new("RGB", th.size, (55, 66, 59))
    bg.paste(th, mask=th.split()[3])
    bg.save(base / "thumb.jpg", quality=85)
    rec["bounds"] = bounds
    rec["center"] = [round((bounds[0][0] + bounds[1][0]) / 2, 6), round((bounds[0][1] + bounds[1][1]) / 2, 6)]
    rec["area_ha"] = round(area_ha, 1)
    rec["thumb"] = f"flights/{rec['id']}/thumb.jpg"
    rec["footprint"] = footprint(mask, transform)
    rec["bbox"] = (bounds[0][1], bounds[0][0], bounds[1][1], bounds[1][0])


def phase2(flights, sites, las_meta):
    stats_raw = {}
    for fl in flights:
        for kind in fl["layers"]:
            if kind in RGB_LAYERS:
                continue
            arr, _, _ = load_cache(fl["id"], kind)
            v = arr[np.isfinite(arr)].astype(np.float32)
            if v.size:
                stats_raw[(fl["id"], kind)] = np.percentile(v, [2, 98])
    ranges = {}
    for kind in CATALOG:
        ps = [p for (fid, k), p in stats_raw.items() if k == kind]
        if ps:
            ranges[kind] = nice_range(min(p[0] for p in ps), max(p[1] for p in ps), kind)

    catalog = {}
    for kind in LAYER_ORDER:
        c = {k: v for k, v in CATALOG[kind].items() if k != "cmap"}
        if "cmap" in CATALOG[kind]:
            c["legend_gradient"] = css_gradient(get_cmap(kind))
        if kind in ranges:
            c["vmin"], c["vmax"] = ranges[kind]
        if kind in PER_FLIGHT_RANGE:
            c["per_flight_range"] = True
        catalog[kind] = c

    out_flights, bboxes = [], []
    for fl in flights:
        fid = fl["id"]
        m = FOLDER_RE.match(fid)
        date = f"{m['y']}-{m['m']}-{m['d']}" if m else None
        site = sites.get(fid, {})
        rec = {
            "id": fid,
            "sensor": m["sensor"] if m else "",
            "site_code": site.get("site_code", m["site"] if m else fid),
            "site_name": site.get("site_name", fid),
            "comuna": site.get("comuna", ""),
            "date": date,
            "date_label": f"{int(m['d'])} de {MONTHS[int(m['m']) - 1]} de {m['y']}" if m else "",
            "layers": {},
        }
        base = OUT / "flights" / fid
        if base.exists():
            for old in list(base.glob("*.webp")) + list(base.glob("*_grid.bin")):
                old.unlink()
        kinds_here = [k for k in LAYER_ORDER if k in fl["layers"]]
        if (base / "tiles").exists():
            for d in (base / "tiles").iterdir():
                if d.name not in kinds_here:
                    shutil.rmtree(d)
        for kind in kinds_here:
            arr, t, area_ha = load_cache(fid, kind)
            path = fl["layers"][kind]
            if kind in RGB_LAYERS:
                h, w = arr.shape[1:]
                raw = arr[3] > 0
                with rasterio.open(path) as src:
                    native8 = src.dtypes[0] == "uint8"
                valid = clean_edges(remove_canvas(arr) if native8 else raw, t)
                arr = arr.copy()
                arr[:, ~valid] = 0
                area_ha *= valid.sum() / max(raw.sum(), 1)
                rgba = np.moveaxis(arr, 0, -1)
                bounds = web_bounds_latlon(t, h, w)
                lat = (bounds[0][0] + bounds[1][0]) / 2
                tiles = build_layer_tiles(
                    path, base / "tiles" / kind, f"flights/{fid}/tiles/{kind}", source_sig(path),
                    mask_sig=mask_signature(valid), kind=kind, is_rgb=True, mask=valid, mask_t=t,
                    lat=lat, zcap=ZCAP[kind], workers=TILE_WORKERS)
                log(f"  {fid}/{kind}: {tiles['count']} teselas (z{tiles['minzoom']}–{tiles['maxzoom']})")
                rec["layers"][kind] = {"tiles": tiles, "bounds": bounds, "area_ha": round(area_ha, 1)}
                if "bounds" not in rec:
                    set_extent(rec, rgba, bounds, area_ha, valid, t, base)
                    bboxes.append(rec["bbox"])
                continue
            arr = arr.astype(np.float32)
            h, w = arr.shape
            raw = np.isfinite(arr)
            valid = clean_edges(raw, t)
            area_ha *= valid.sum() / max(raw.sum(), 1)
            arr[~valid] = np.nan
            v = arr[valid]
            if not v.size:
                log(f"  {fid}/{kind}: sin datos válidos, se omite")
                continue
            extra = {}
            if kind in PER_FLIGHT_RANGE:
                vmin, vmax = nice_range(*np.percentile(v, [2, 98]), kind)
                extra = {"vmin": vmin, "vmax": vmax}
            else:
                vmin, vmax = ranges[kind]
            bounds = web_bounds_latlon(t, h, w)
            lat = rec["center"][0] if "center" in rec else (bounds[0][0] + bounds[1][0]) / 2
            colorize = make_colorizer(get_cmap(kind), vmin, vmax)
            tiles = build_layer_tiles(
                path, base / "tiles" / kind, f"flights/{fid}/tiles/{kind}", source_sig(path),
                mask_sig=mask_signature(valid, vmin, vmax), kind=kind, is_rgb=False, mask=valid, mask_t=t,
                lat=lat, zcap=ZCAP[kind], colorize=colorize,
                hillshade=hillshade if kind == "dem" else None, valid_range=VALID_RANGE[kind],
                workers=TILE_WORKERS)
            log(f"  {fid}/{kind}: {tiles['count']} teselas (z{tiles['minzoom']}–{tiles['maxzoom']})")

            g, gt = value_grid(arr, t)
            gv = np.isfinite(g)
            glo = float(np.nanmin(g)) if gv.any() else 0.0
            ghi = float(np.nanmax(g)) if gv.any() else 1.0
            scale = (ghi - glo) / 65534 if ghi > glo else 1.0
            gq = np.where(gv, np.round((g - glo) / scale), 65535).astype("<u2")
            base.mkdir(parents=True, exist_ok=True)
            (base / f"{kind}_grid.bin").write_bytes(gq.tobytes())

            counts, edges = np.histogram(v, bins=HIST_BINS, range=(vmin, vmax))
            p2, p25, p50, p75, p98 = np.percentile(v, [2, 25, 50, 75, 98])
            rec["layers"][kind] = {
                "tiles": tiles,
                "bounds": bounds,
                "stats": {
                    "mean": round(float(v.mean()), 4), "std": round(float(v.std()), 4),
                    "min": round(float(v.min()), 4), "max": round(float(v.max()), 4),
                    "p2": round(float(p2), 4), "p25": round(float(p25), 4), "median": round(float(p50), 4),
                    "p75": round(float(p75), 4), "p98": round(float(p98), 4),
                    "area_ha": round(area_ha, 1),
                },
                "hist": {"edges": [round(float(e), 4) for e in edges],
                         "pct": [round(float(c) / max(v.size, 1) * 100, 3) for c in counts]},
                "grid": {"path": f"flights/{fid}/{kind}_grid.bin", "width": int(g.shape[1]),
                         "height": int(g.shape[0]), "offset": glo, "scale": scale,
                         "bounds": web_bounds_latlon(gt, g.shape[0], g.shape[1])},
                **extra,
            }
            if "bounds" not in rec:
                rgba = colorize(arr)
                rgba[..., 3] = np.where(valid, 255, 0)
                set_extent(rec, rgba, bounds, area_ha, valid, t, base)
                bboxes.append(rec["bbox"])
        if fid in las_meta:
            rec["layers"]["lidar"] = las_meta[fid]
        out_flights.append(rec)
        log(f"  {fid}: {len(rec['layers'])} capas exportadas")

    out_flights.sort(key=lambda r: (r["date"] or "", r["id"]))
    manifest = {
        "title": "Aconcagua Vivo · Explorador de datos de dron",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "layer_order": LAYER_ORDER,
        "missions": MISSIONS,
        "layers": catalog,
        "flights": out_flights,
        "comunas": export_comunas(bboxes),
    }
    text = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    (OUT / "manifest.json").write_text(text, "utf-8")
    (OUT / "manifest.js").write_text("window.FNDR_MANIFEST = " + text + ";\n", "utf-8")
    log(f"manifest.json escrito con {len(out_flights)} vuelos")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-las", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    flights = discover()
    if not flights:
        sys.exit(f"No se encontraron vuelos en {DRONE}")
    sites_p = Path(__file__).with_name("sites.json")
    sites = json.loads(sites_p.read_text("utf-8")) if sites_p.exists() else {}
    log(f"{len(flights)} vuelos: " + ", ".join(f["id"] for f in flights))

    log("Fase 1: lectura y reproyección de rásters")
    phase1(flights, args.force, args.workers)

    las_meta = {}
    if not args.skip_las:
        log("Nubes de puntos LiDAR")
        for fl in flights:
            if fl["las"]:
                las_meta[fl["id"]] = process_las(fl["id"], fl["las"], args.force)
    else:
        for fl in flights:
            p = CACHE / fl["id"] / "pointcloud.json"
            if p.exists():
                las_meta[fl["id"]] = json.loads(p.read_text("utf-8"))

    log("Fase 2: estilos, estadísticas y manifest")
    phase2(flights, sites, las_meta)
    log("Listo.")


if __name__ == "__main__":
    main()
