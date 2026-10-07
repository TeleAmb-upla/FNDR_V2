"""
Pirámide de teselas XYZ (EPSG:3857, 256 px, WebP) para el explorador.

Las teselas del zoom máximo se leen directamente del GeoTIFF original con un WarpedVRT,
por bloques. Los zooms menores se componen a partir de las teselas hijas. El contorno de
cada capa se recorta con un campo de distancia derivado de la máscara suavizada, por lo
que el borde queda limpio y antialiasado a cualquier zoom.
"""
from __future__ import annotations

import base64
import json
import math
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from scipy import ndimage

WEB_CRS = "EPSG:3857"
ORIGIN = 20037508.342789244
TILE = 256
BLOCK = 8
PAD = 2
TILE_VERSION = 3


def tile_size_m(z):
    return 2 * ORIGIN / (1 << z)


def native_zoom(src, lat):
    web_res = abs(src.res[0]) / math.cos(math.radians(lat))
    return math.log2(2 * ORIGIN / TILE / web_res)


def tile_range(bounds_web, z):
    l, b, r, t = bounds_web
    s = tile_size_m(z)
    x0 = int(math.floor((l + ORIGIN) / s))
    x1 = int(math.floor((r + ORIGIN) / s - 1e-9))
    y0 = int(math.floor((ORIGIN - t) / s))
    y1 = int(math.floor((ORIGIN - b) / s - 1e-9))
    return x0, y0, x1, y1


def finest_level(path, samples=24):
    """Algunos GeoTIFF traen solo las vistas generales (overviews) con datos y la
    resolución completa vacía. Devuelve el nivel más fino que tenga datos reales."""
    with rasterio.open(path) as src:
        n_ovr = len(src.overviews(1))
        if not n_ovr:
            return None
    with rasterio.open(path, overview_level=n_ovr - 1) as coarse:
        valid = coarse.read_masks(1) > 0
        if coarse.count >= 4:
            valid &= coarse.read(coarse.count) > 0
        ys, xs = np.nonzero(valid)
        if not ys.size:
            return None
        pick = np.random.default_rng(0).choice(ys.size, min(samples, ys.size), replace=False)
        pts = [((xs[i] + 0.5) / coarse.width, (ys[i] + 0.5) / coarse.height) for i in pick]
    for level in [None] + list(range(n_ovr)):
        kw = {} if level is None else {"overview_level": level}
        with rasterio.open(path, **kw) as src:
            hits = 0
            for fx, fy in pts:
                c, r = int(fx * src.width), int(fy * src.height)
                hits += int(src.read(1, window=Window(c, r, 1, 1))[0, 0] != 0)
        if hits >= 0.5 * len(pts):
            return level
    return None


def signed_distance(mask, transform):
    sampling = (abs(transform.e), abs(transform.a))
    inside = ndimage.distance_transform_edt(mask, sampling=sampling)
    outside = ndimage.distance_transform_edt(~mask, sampling=sampling)
    return (inside - outside).astype(np.float32)


def sample_sdf(sdf, st, x0, y0, res, h, w):
    my, mx = -res / st.e, res / st.a
    oy = (y0 - 0.5 * res - st.f) / st.e - 0.5
    ox = (x0 + 0.5 * res - st.c) / st.a - 0.5
    return ndimage.affine_transform(sdf, [my, mx], offset=[oy, ox], output_shape=(h, w),
                                    order=1, cval=-1e4, mode="constant")


def encode_index(tiles):
    out = {}
    by_z = {}
    for z, x, y in tiles:
        by_z.setdefault(z, []).append((x, y))
    for z, xy in by_z.items():
        a = np.array(xy)
        x0, y0 = a.min(axis=0)
        w, h = a.max(axis=0) - (x0, y0) + 1
        bits = np.zeros(int(w * h), np.uint8)
        bits[(a[:, 1] - y0) * w + (a[:, 0] - x0)] = 1
        out[str(z)] = [int(x0), int(y0), int(w), int(h), base64.b64encode(np.packbits(bits).tobytes()).decode()]
    return out


def save_tile(rgba, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(rgba), "RGBA").save(path, "WEBP", quality=82, method=4)


class LayerTiler:
    def __init__(self, src_path, out_dir, kind, is_rgb, mask, mask_t, lat, zcap,
                 colorize=None, hillshade=None, valid_range=None, workers=6):
        self.src_path = str(src_path)
        self.out_dir = Path(out_dir)
        self.kind = kind
        self.is_rgb = is_rgb
        self.lat = lat
        self.colorize = colorize
        self.hillshade = hillshade
        self.valid_range = valid_range
        self.workers = workers
        self.sdf = signed_distance(mask, mask_t)
        self.sdf_t = mask_t
        h, w = mask.shape
        self.bounds_web = (mask_t.c, mask_t.f + mask_t.e * h, mask_t.c + mask_t.a * w, mask_t.f)
        self.level = finest_level(self.src_path)
        self.open_kw = {} if self.level is None else {"overview_level": self.level}
        with rasterio.open(self.src_path, **self.open_kw) as src:
            self.zmax = int(min(zcap, max(12, round(native_zoom(src, lat)))))
            self.bands, self.alpha_band, self.stretch = self._rgb_setup(src) if is_rgb else (None, None, None)
            self.src_nodata = src.nodata
        ext = max(self.bounds_web[2] - self.bounds_web[0], self.bounds_web[3] - self.bounds_web[1])
        self.zmin = max(10, min(self.zmax, int(math.floor(math.log2(2 * ORIGIN / ext)))))
        self.local = threading.local()

    def _rgb_setup(self, src):
        alpha = None
        for i, (d, ci) in enumerate(zip(src.descriptions, src.colorinterp)):
            if i >= 3 and ((d or "").lower() == "alpha" or ci == ColorInterp.alpha):
                alpha = i + 1
                break
        stretch = None
        if src.dtypes[0] != "uint8":
            f = max(src.width, src.height) / 2000
            shp = (max(1, int(src.height / f)), max(1, int(src.width / f)))
            data = src.read([1, 2, 3], out_shape=(3,) + shp, resampling=Resampling.nearest)
            valid = (src.read(alpha, out_shape=shp, resampling=Resampling.nearest) > 0) if alpha else data.max(axis=0) > 0
            stretch = [np.percentile(data[i][valid], [1, 99]) for i in range(3)]
        return [1, 2, 3] + ([alpha] if alpha else []), alpha, stretch

    def _vrt(self, z):
        cache = getattr(self.local, "vrt", None)
        if cache and cache[0] == z:
            return cache[1]
        if cache:
            cache[1].close()
            cache[2].close()
        x0, y0, x1, y1 = tile_range(self.bounds_web, z)
        s = tile_size_m(z)
        res = s / TILE
        gt = Affine(res, 0, -ORIGIN + x0 * s - PAD * res, 0, -res, ORIGIN - y0 * s + PAD * res)
        width = (x1 - x0 + 1) * TILE + 2 * PAD
        height = (y1 - y0 + 1) * TILE + 2 * PAD
        src = rasterio.open(self.src_path, **self.open_kw)
        opts = dict(crs=WEB_CRS, transform=gt, width=width, height=height,
                    resampling=Resampling.bilinear if self.kind == "dem" else Resampling.average,
                    warp_mem_limit=512)
        if self.is_rgb:
            if self.alpha_band is None and self.src_nodata is not None:
                opts.update(src_nodata=self.src_nodata, nodata=0)
        else:
            opts.update(src_nodata=self.src_nodata, nodata=np.nan, dtype="float32")
        vrt = WarpedVRT(src, **opts)
        self.local.vrt = (z, vrt, src)
        return vrt

    def _render_block(self, z, bx, by, x0, y0, x1, y1):
        s = tile_size_m(z)
        res = s / TILE
        tx0, ty0 = x0 + bx * BLOCK, y0 + by * BLOCK
        nx, ny = min(BLOCK, x1 - tx0 + 1), min(BLOCK, y1 - ty0 + 1)
        w, h = nx * TILE, ny * TILE
        wx0, wy0 = -ORIGIN + tx0 * s, ORIGIN - ty0 * s
        sdf = sample_sdf(self.sdf, self.sdf_t, wx0, wy0, res, h, w)
        if sdf.max() <= -res:
            return []
        alpha = np.clip(sdf / res + 0.5, 0, 1)
        vrt = self._vrt(z)
        win = Window(bx * BLOCK * TILE, by * BLOCK * TILE, w + 2 * PAD, h + 2 * PAD)
        if self.is_rgb:
            data = vrt.read(self.bands, window=win)[:, PAD:-PAD, PAD:-PAD]
            if self.alpha_band:
                valid = data[3] > 127
            else:
                valid = data[:3].max(axis=0) > 0
            rgb = data[:3]
            if self.stretch is not None:
                out = np.empty(rgb.shape, np.uint8)
                for i, (lo, hi) in enumerate(self.stretch):
                    out[i] = np.clip((rgb[i].astype(np.float32) - lo) / max(hi - lo, 1e-6) * 255, 0, 255)
                rgb = out
            rgba = np.empty((h, w, 4), np.uint8)
            rgba[..., :3] = np.moveaxis(rgb, 0, -1)
        else:
            v = vrt.read(1, window=win).astype(np.float32)
            if self.valid_range:
                lo, hi = self.valid_range
                v[(v < lo) | (v > hi)] = np.nan
            rgba = self.colorize(v)
            if self.hillshade is not None:
                bt = Affine(res, 0, wx0 - PAD * res, 0, -res, wy0 + PAD * res)
                hs = self.hillshade(v, bt, self.lat)
                rgba[..., :3] = (rgba[..., :3] * (0.35 + 0.65 * hs[..., None])).astype(np.uint8)
            valid = np.isfinite(v)[PAD:-PAD, PAD:-PAD]
            rgba = rgba[PAD:-PAD, PAD:-PAD]
        a = (alpha * 255).astype(np.uint8)
        a[~valid] = 0
        rgba[..., 3] = a
        rgba[a == 0, :3] = 0
        written = []
        for j in range(ny):
            for i in range(nx):
                sub = rgba[j * TILE:(j + 1) * TILE, i * TILE:(i + 1) * TILE]
                if sub[..., 3].max() == 0:
                    continue
                save_tile(sub, self.out_dir / str(z) / str(tx0 + i) / f"{ty0 + j}.webp")
                written.append((z, tx0 + i, ty0 + j))
        return written

    def _render_parent(self, z, x, y, children):
        canvas = Image.new("RGBA", (2 * TILE, 2 * TILE), (0, 0, 0, 0))
        for dx in (0, 1):
            for dy in (0, 1):
                c = (z + 1, 2 * x + dx, 2 * y + dy)
                if c in children:
                    with Image.open(self.out_dir / str(c[0]) / str(c[1]) / f"{c[2]}.webp") as im:
                        canvas.paste(im.convert("RGBA"), (dx * TILE, dy * TILE))
        small = canvas.resize((TILE, TILE), Image.BOX)
        arr = np.asarray(small)
        if arr[..., 3].max() == 0:
            return None
        save_tile(arr, self.out_dir / str(z) / str(x) / f"{y}.webp")
        return (z, x, y)

    def run(self):
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        z = self.zmax
        x0, y0, x1, y1 = tile_range(self.bounds_web, z)
        nbx = (x1 - x0) // BLOCK + 1
        nby = (y1 - y0) // BLOCK + 1
        tiles = set()
        with rasterio.Env(GDAL_CACHEMAX=512, GDAL_NUM_THREADS="ALL_CPUS"):
            with ThreadPoolExecutor(self.workers) as ex:
                jobs = [ex.submit(self._render_block, z, bx, by, x0, y0, x1, y1)
                        for by in range(nby) for bx in range(nbx)]
                for j in jobs:
                    tiles.update(j.result())
        level = {t for t in tiles}
        with ThreadPoolExecutor(self.workers) as ex:
            for z in range(self.zmax - 1, self.zmin - 1, -1):
                parents = {(z, x // 2, y // 2) for (_, x, y) in level}
                res = ex.map(lambda p: self._render_parent(p[0], p[1], p[2], level), parents)
                level = {r for r in res if r}
                tiles.update(level)
        return tiles


def build_layer_tiles(src_path, out_dir, rel_url, sig, **kw):
    meta_p = Path(out_dir) / "tiles.json"
    params = {"src": [int(v) for v in sig], "version": TILE_VERSION, "zcap": kw.get("zcap"),
              "mask": kw.pop("mask_sig")}
    if meta_p.exists():
        old = json.loads(meta_p.read_text("utf-8"))
        if old.get("params") == params:
            return old["meta"]
    tiler = LayerTiler(src_path, out_dir, **kw)
    tiles = tiler.run()
    meta = {"url": rel_url + "/{z}/{x}/{y}.webp", "minzoom": tiler.zmin, "maxzoom": tiler.zmax,
            "count": len(tiles), "index": encode_index(tiles)}
    if tiler.level is not None:
        meta["source_overview"] = tiler.level
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    meta_p.write_text(json.dumps({"params": params, "meta": meta}), "utf-8")
    return meta
