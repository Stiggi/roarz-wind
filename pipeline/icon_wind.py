#!/usr/bin/env python3
"""ICON-EU wind and weather data for roarz (DWD open data → PNG textures).

Runs in GitHub Actions (.github/workflows/wind.yml); .github/scripts/ftp-wind.sh uploads the result. The roarz app
(public/js/wind/ in the app repository) decodes it – keep both sides in step when the format changes.

Source: https://opendata.dwd.de/weather/nwp/icon-eu/grib/{HH}/{var}/
        icon-eu_europe_regular-lat-lon_single-level_{YYYYMMDDHH}_{step:03d}_{VAR}.grib2.bz2
        (regular 0.0625° grid, 1377 × 657 points, lon −23.5…62.5, lat 29.5…70.5; CCSDS-packed GRIB2)

Output (one folder per model run):
  {out}/{run}/map/step_000.png … step_048.png     map texture, downsampled: R = u, G = v, B = gust
  {out}/{run}/tiles/{tileId}/wind_{step}.png       full resolution, 128 × 128 points: R = u, G = v, B = gust
  {out}/{run}/tiles/{tileId}/weather_{step}.png    R = t_2m, G = precipitation (mm/h), B = cloud cover, A = pmsl
  {out}/{run}/meta.json                            run, steps, grid, value ranges, tile index, attribution
  {out}/latest.json                                points to the run folder – written last, only on success

Byte b of a channel stands for  min + b / 255 · (max − min)  (ranges in ENCODING, also in meta.json). Row 0 is
the northernmost row. Tiles without any land point (DWD land fraction FR_LAND) are left out.

Rules: only a run whose files for ALL variables and ALL steps exist is processed; a newer run is published,
otherwise nothing happens (the published data stays as it is). Any download or decoding error aborts the run with
a non-zero exit code before latest.json is written.

Usage: python pipeline/icon_wind.py --out build/wind [--published latest.json] [--run YYYYMMDDHH]
"""

from __future__ import annotations

import argparse
import bz2
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

BASE_URL = 'https://opendata.dwd.de/weather/nwp/icon-eu/grib'
FILE_TEMPLATE = 'icon-eu_europe_regular-lat-lon_single-level_{run}_{step:03d}_{var}.grib2.bz2'
LAND_TEMPLATE = 'icon-eu_europe_regular-lat-lon_time-invariant_{run}_FR_LAND.grib2.bz2'

# DWD directory name → our name. Map + stands: u, v, gust. Stands only: the rest.
VARIABLES = {
    'u_10m': 'u',
    'v_10m': 'v',
    'vmax_10m': 'gust',
    't_2m': 't2m',
    'tot_prec': 'tot_prec',
    'clct': 'clct',
    'pmsl': 'pmsl',
}

MAX_STEP = 48  # hourly steps 0…48
RUN_INTERVAL_H = 3  # ICON-EU runs at 00, 03, …, 21 UTC
LOOKBACK_RUNS = 8  # DWD keeps about one day of runs
DOWNSAMPLE = 2  # map texture: every 2nd grid point
TILE_SIZE = 128  # stand tiles: grid points per side

# Fixed value ranges of the 8-bit encoding: name → (min, max, unit).
ENCODING = {
    'u': (-30.0, 30.0, 'm/s'),
    'v': (-30.0, 30.0, 'm/s'),
    'gust': (0.0, 40.0, 'm/s'),
    't2m': (-40.0, 45.0, '°C'),
    'precip': (0.0, 20.0, 'mm/h'),
    'clct': (0.0, 100.0, '%'),
    'pmsl': (950.0, 1050.0, 'hPa'),
}
WIND_CHANNELS = ('u', 'v', 'gust')  # R, G, B
WEATHER_CHANNELS = ('t2m', 'precip', 'clct', 'pmsl')  # R, G, B, A

ATTRIBUTION = {
    'en': 'Based on data from Deutscher Wetterdienst (DWD), ICON-EU, gridded data reproduced graphically. Licence: CC BY 4.0',
    'de': 'Datenbasis: Deutscher Wetterdienst (DWD), ICON-EU, Rasterdaten bildlich wiedergegeben. Lizenz: CC BY 4.0',
}

HTTP_TIMEOUT = 60
HTTP_RETRIES = 4
DOWNLOAD_WORKERS = 8
USER_AGENT = 'roarz.net wind pipeline (https://github.com/Stiggi/roarz-wind)'


class PipelineError(RuntimeError):
    """Data missing or not decodable – the run must not be published."""


# ---------------------------------------------------------------------------------------------- encoding


def encode(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Physical values → bytes (linear, rounded, clipped to the range)."""
    scaled = (np.asarray(values, dtype=np.float64) - lo) / (hi - lo) * 255.0
    return np.clip(np.rint(scaled), 0, 255).astype(np.uint8)


def decode(byte_values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Bytes → physical values (inverse of encode, as done in the browser)."""
    return lo + np.asarray(byte_values, dtype=np.float64) / 255.0 * (hi - lo)


def encode_channels(fields: dict[str, np.ndarray], names: Iterable[str]) -> np.ndarray:
    """Stack the encoded fields as image channels (H × W × len(names))."""
    return np.dstack([encode(fields[n], *ENCODING[n][:2]) for n in names])


def png_bytes(pixels: np.ndarray) -> bytes:
    from PIL import Image  # imported lazily: the tests of the pure functions need no Pillow import time

    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(pixels)).save(buf, 'PNG', compress_level=9)
    return buf.getvalue()


def deaccumulate(accum: np.ndarray, prev_accum: np.ndarray | None) -> np.ndarray:
    """tot_prec is accumulated since the run start (kg/m² = mm): amount of the last hour, never negative."""
    if prev_accum is None:
        return np.zeros_like(accum, dtype=np.float64)
    return np.maximum(np.asarray(accum, dtype=np.float64) - prev_accum, 0.0)


# ---------------------------------------------------------------------------------------------- grid & tiles


@dataclass(frozen=True)
class Grid:
    west: float
    north: float
    dlon: float
    dlat: float
    width: int
    height: int

    @property
    def east(self) -> float:
        return self.west + (self.width - 1) * self.dlon

    @property
    def south(self) -> float:
        return self.north - (self.height - 1) * self.dlat

    def bbox(self, x0: int, y0: int, w: int, h: int) -> dict:
        """Bounds of grid points [x0, x0+w) × [y0, y0+h) (point centres), rounded for JSON."""
        r = lambda v: round(v, 6)  # noqa: E731
        return {
            'west': r(self.west + x0 * self.dlon),
            'east': r(self.west + (x0 + w - 1) * self.dlon),
            'north': r(self.north - y0 * self.dlat),
            'south': r(self.north - (y0 + h - 1) * self.dlat),
        }

    def as_json(self) -> dict:
        return {'width': self.width, 'height': self.height, 'dlon': self.dlon, 'dlat': self.dlat, **self.bbox(0, 0, self.width, self.height)}


def downsampled(grid: Grid, factor: int) -> Grid:
    return Grid(grid.west, grid.north, grid.dlon * factor, grid.dlat * factor, (grid.width - 1) // factor + 1, (grid.height - 1) // factor + 1)


def tile_id(ty: int, tx: int) -> str:
    return f'{ty}_{tx}'


def tile_layout(grid: Grid, size: int, land: np.ndarray | None) -> dict[str, dict]:
    """Tile index: id → {x0, y0, width, height, west, east, north, south}. Tiles without land are skipped."""
    index = {}
    for ty in range(math.ceil(grid.height / size)):
        for tx in range(math.ceil(grid.width / size)):
            x0, y0 = tx * size, ty * size
            w, h = min(size, grid.width - x0), min(size, grid.height - y0)
            if land is not None and not np.any(land[y0 : y0 + h, x0 : x0 + w] > 0):
                continue
            index[tile_id(ty, tx)] = {'x0': x0, 'y0': y0, 'width': w, 'height': h, **grid.bbox(x0, y0, w, h)}
    return index


def tile_lookup(meta: dict, lat: float, lon: float) -> tuple[str, int, int] | None:
    """Tile id and pixel (x, y) of the grid point nearest to lat/lon, or None (outside / sea tile)."""
    g = meta['grid']
    gx = round((lon - g['west']) / g['dlon'])
    gy = round((g['north'] - lat) / g['dlat'])
    if not (0 <= gx < g['width'] and 0 <= gy < g['height']):
        return None
    size = meta['tiles']['size']
    tid = tile_id(gy // size, gx // size)
    if tid not in meta['tiles']['index']:
        return None
    return tid, gx % size, gy % size


# ---------------------------------------------------------------------------------------------- run selection


def run_name(run: datetime) -> str:
    return run.strftime('%Y%m%d%H')


def parse_run(name: str) -> datetime:
    return datetime.strptime(name, '%Y%m%d%H').replace(tzinfo=timezone.utc)


def candidate_runs(now: datetime, count: int = LOOKBACK_RUNS) -> list[datetime]:
    """The last `count` run times up to `now`, newest first."""
    now = now.astimezone(timezone.utc)
    latest = now.replace(minute=0, second=0, microsecond=0, hour=now.hour - now.hour % RUN_INTERVAL_H)
    return [latest - timedelta(hours=RUN_INTERVAL_H * i) for i in range(count)]


def required_files(run: datetime, max_step: int = MAX_STEP) -> dict[str, list[str]]:
    """DWD directory → file names that must all exist for the run."""
    name = run_name(run)
    return {d: [FILE_TEMPLATE.format(run=name, step=s, var=d.upper()) for s in range(max_step + 1)] for d in VARIABLES}


def run_complete(run: datetime, listing: Callable[[str], set[str]], max_step: int = MAX_STEP) -> bool:
    """True if every variable has every step. `listing(dir_url)` returns the file names in a DWD directory."""
    hh = f'{run.hour:02d}'
    for directory, files in required_files(run, max_step).items():
        available = listing(f'{BASE_URL}/{hh}/{directory}/')
        missing = [f for f in files if f not in available]
        if missing:
            print(f'  {run_name(run)}: {directory} incomplete ({len(missing)} of {len(files)} files missing)')
            return False
    return True


def select_run(candidates: list[datetime], listing: Callable[[str], set[str]], published: str | None, max_step: int = MAX_STEP) -> datetime | None:
    """Newest complete run, or None if there is none or it is not newer than the published one."""
    for run in candidates:
        if published and run_name(run) <= published:
            print(f'No complete run newer than the published {published}.')
            return None
        if run_complete(run, listing, max_step):
            return run
    print('No complete run found.')
    return None


# ---------------------------------------------------------------------------------------------- network


def http_get(url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
                return res.read()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise PipelineError(f'not found: {url}') from err
            last = err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last = err
        time.sleep(2**attempt)
    raise PipelineError(f'download failed: {url}: {last}')


_listing_cache: dict[str, set[str]] = {}


def dwd_listing(url: str) -> set[str]:
    """File names of an Apache-style directory listing (cached per run of the script)."""
    if url not in _listing_cache:
        try:
            html = http_get(url).decode('utf-8', 'replace')
        except PipelineError:
            html = ''
        _listing_cache[url] = set(re.findall(r'href="([^"/?]+\.grib2\.bz2)"', html))
    return _listing_cache[url]


# ---------------------------------------------------------------------------------------------- GRIB


@dataclass
class Field:
    values: np.ndarray  # north-up (row 0 = north), float64
    grid: Grid
    run: str  # YYYYMMDDHH from dataDate/dataTime
    step: int  # end step in hours


def decode_grib(data: bytes) -> Field:
    """Decode one bz2-compressed GRIB2 message (regular lat/lon) with ecCodes."""
    import eccodes  # lazily: the unit tests do not need ecCodes

    try:
        raw = bz2.decompress(data)
    except (OSError, ValueError) as err:
        raise PipelineError(f'bz2: {err}') from err
    h = eccodes.codes_new_from_message(raw)
    try:
        if eccodes.codes_get(h, 'gridType') != 'regular_ll':
            raise PipelineError('unexpected grid type')
        ni, nj = eccodes.codes_get(h, 'Ni'), eccodes.codes_get(h, 'Nj')
        lat1 = eccodes.codes_get_double(h, 'latitudeOfFirstGridPointInDegrees')
        lat2 = eccodes.codes_get_double(h, 'latitudeOfLastGridPointInDegrees')
        lon1 = eccodes.codes_get_double(h, 'longitudeOfFirstGridPointInDegrees')
        dlon = eccodes.codes_get_double(h, 'iDirectionIncrementInDegrees')
        dlat = eccodes.codes_get_double(h, 'jDirectionIncrementInDegrees')
        north_first = eccodes.codes_get(h, 'jScansPositively') == 0
        values = eccodes.codes_get_values(h).astype(np.float64)
        if eccodes.codes_get(h, 'bitmapPresent'):
            values[values == eccodes.codes_get_double(h, 'missingValue')] = np.nan
        run = f"{eccodes.codes_get(h, 'dataDate'):08d}{eccodes.codes_get(h, 'dataTime') // 100:02d}"
        eccodes.codes_set(h, 'stepUnits', 'h')
        step = eccodes.codes_get_long(h, 'endStep')
    except PipelineError:
        raise
    except Exception as err:  # ecCodes raises its own exception types
        raise PipelineError(f'GRIB: {err}') from err
    finally:
        eccodes.codes_release(h)
    if values.size != ni * nj:
        raise PipelineError(f'value count {values.size} != {ni}×{nj}')
    if not np.all(np.isfinite(values)):
        raise PipelineError('missing values in field')
    values = values.reshape(nj, ni)
    if not north_first:
        values = values[::-1]
    west = lon1 - 360 if lon1 > 180 else lon1
    return Field(values, Grid(west, max(lat1, lat2), dlon, dlat, ni, nj), run, step)


# ---------------------------------------------------------------------------------------------- build


@dataclass
class Stats:
    downloaded_bytes: int = 0
    downloaded_files: int = 0
    written_bytes: int = 0
    written_files: int = 0

    def write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.written_bytes += len(data)
        self.written_files += 1


def build_run(
    run: datetime,
    out: Path,
    *,
    previous: str | None = None,
    fetch: Callable[[str], bytes] = http_get,
    decoder: Callable[[bytes], Field] = decode_grib,
    max_step: int = MAX_STEP,
    factor: int = DOWNSAMPLE,
    tile_size: int = TILE_SIZE,
    workers: int = DOWNLOAD_WORKERS,
) -> Stats:
    """Download, decode and encode one run into {out}/{run}/, then write {out}/latest.json.

    The run is assembled in a temporary folder and renamed when complete; on any error nothing of it is left
    in {out} and latest.json is not touched.
    """
    name = run_name(run)
    hh = f'{run.hour:02d}'
    stats = Stats()
    tmp = out / f'.tmp-{name}'
    final = out / name
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        land = None
        try:
            land_field = decoder(fetch(f'{BASE_URL}/{hh}/fr_land/{LAND_TEMPLATE.format(run=name)}'))
            land = land_field.values
        except PipelineError as err:
            print(f'  Land mask not available ({err}) – keeping all tiles.')

        grid: Grid | None = None
        meta_tiles: dict[str, dict] = {}
        prev_accum = None
        steps = []

        def load(step: int) -> dict[str, Field]:
            urls = {
                key: f'{BASE_URL}/{hh}/{d}/{FILE_TEMPLATE.format(run=name, step=step, var=d.upper())}' for d, key in VARIABLES.items()
            }
            with ThreadPoolExecutor(max_workers=workers) as pool:
                blobs = dict(zip(urls, pool.map(fetch, urls.values())))
            stats.downloaded_files += len(blobs)
            stats.downloaded_bytes += sum(len(b) for b in blobs.values())
            fields = {}
            for key, blob in blobs.items():
                try:
                    f = decoder(blob)
                except Exception as err:
                    raise PipelineError(f'{key} step {step}: decoding failed: {err}') from err
                if f.run != name or f.step != step:
                    raise PipelineError(f'{key} step {step}: file contains run {f.run} step {f.step}')
                fields[key] = f
            return fields

        # Download the next step while the current one is encoded.
        with ThreadPoolExecutor(max_workers=1) as prefetch:
            pending = prefetch.submit(load, 0)
            for step in range(max_step + 1):
                fields = pending.result()
                if step < max_step:
                    pending = prefetch.submit(load, step + 1)

                if grid is None:
                    grid = fields['u'].grid
                    if land is not None and land.shape != (grid.height, grid.width):
                        print('  Land mask has a different grid – keeping all tiles.')
                        land = None
                    meta_tiles = tile_layout(grid, tile_size, land)
                for key, f in fields.items():
                    if f.grid != grid:
                        raise PipelineError(f'{key} step {step}: grid differs from u_10m')

                values = {
                    'u': fields['u'].values,
                    'v': fields['v'].values,
                    'gust': fields['gust'].values,
                    't2m': fields['t2m'].values - 273.15,
                    'precip': deaccumulate(fields['tot_prec'].values, prev_accum),
                    'clct': fields['clct'].values,
                    'pmsl': fields['pmsl'].values / 100.0,
                }
                prev_accum = fields['tot_prec'].values

                wind = encode_channels(values, WIND_CHANNELS)
                weather = encode_channels(values, WEATHER_CHANNELS)
                images = {tmp / 'map' / f'step_{step:03d}.png': wind[::factor, ::factor]}
                for tid, t in meta_tiles.items():
                    sl = (slice(t['y0'], t['y0'] + t['height']), slice(t['x0'], t['x0'] + t['width']))
                    images[tmp / 'tiles' / tid / f'wind_{step:03d}.png'] = wind[sl]
                    images[tmp / 'tiles' / tid / f'weather_{step:03d}.png'] = weather[sl]
                # zlib releases the GIL: encode the PNGs of a step in parallel.
                with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
                    for path, data in zip(images, pool.map(png_bytes, images.values())):
                        stats.write(path, data)
                steps.append({'step': step, 'time': iso(run + timedelta(hours=step))})

        assert grid is not None
        small = downsampled(grid, factor)
        meta = {
            'version': 1,
            'model': 'ICON-EU',
            'source': 'Deutscher Wetterdienst (DWD), opendata.dwd.de',
            'license': 'CC BY 4.0',
            'attribution': ATTRIBUTION,
            'run': name,
            'runTime': iso(run),
            'generated': iso(datetime.now(timezone.utc)),
            'steps': steps,
            'grid': grid.as_json(),
            'map': {'factor': factor, 'path': 'map/step_{step}.png', **small.as_json()},
            'tiles': {
                'size': tile_size,
                'path': 'tiles/{id}/{kind}_{step}.png',
                'kinds': {'wind': list(WIND_CHANNELS), 'weather': list(WEATHER_CHANNELS)},
                'landOnly': land is not None,
                'index': meta_tiles,
            },
            'encoding': {
                'formula': 'value = min + byte / 255 * (max - min); row 0 = north; {step} = 3 digits (000)',
                'decode': 'weather PNGs carry pmsl in alpha: decode without premultiplied alpha (WebGL / createImageBitmap premultiplyAlpha: none)',
                'ranges': {k: {'min': lo, 'max': hi, 'unit': unit} for k, (lo, hi, unit) in ENCODING.items()},
                'map': list(WIND_CHANNELS),
            },
        }
        stats.write(tmp / 'meta.json', json.dumps(meta, separators=(',', ':')).encode())
        shutil.rmtree(final, ignore_errors=True)
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    latest = {'run': name, 'runTime': iso(run), 'generated': meta['generated'], 'meta': f'{name}/meta.json', 'previous': previous}
    (out / 'latest.json').write_text(json.dumps(latest, indent=1) + '\n')
    return stats


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def published_runs(path: Path | None) -> tuple[str | None, str | None]:
    """(run, previous) from the currently published latest.json; missing or unreadable → None."""
    if not path or not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None
    valid = lambda v: v if isinstance(v, str) and re.fullmatch(r'\d{10}', v) else None  # noqa: E731
    return (valid(data.get('run')), valid(data.get('previous'))) if isinstance(data, dict) else (None, None)


def summary(lines: list[str]) -> None:
    text = '\n'.join(lines)
    print(text)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(text + '\n')


def set_output(key: str, value: str) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(f'{key}={value}\n')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', type=Path, required=True, help='output folder (gets {run}/ and latest.json)')
    ap.add_argument('--published', type=Path, help='latest.json currently on the server (to skip runs already published)')
    ap.add_argument('--run', help='process this run (YYYYMMDDHH) instead of the newest complete one')
    ap.add_argument('--factor', type=int, default=DOWNSAMPLE, help='downsampling of the map texture')
    ap.add_argument('--steps', type=int, default=MAX_STEP, help='last forecast step (hours)')
    args = ap.parse_args(argv)

    started = time.monotonic()
    published, published_previous = published_runs(args.published)
    print(f'Published run: {published or "none"}')
    if args.run:
        run = parse_run(args.run)
        if not run_complete(run, dwd_listing, args.steps):
            raise PipelineError(f'run {args.run} is incomplete')
    else:
        run = select_run(candidate_runs(datetime.now(timezone.utc)), dwd_listing, published, args.steps)
    if run is None:
        summary(['### Wind data', f'Nothing to publish – the published run ({published or "none"}) stays.'])
        set_output('run', '')
        return 0

    print(f'Processing run {run_name(run)} …')
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'latest.json').unlink(missing_ok=True)
    # Rebuilding the published run (manual --run) keeps its predecessor as "previous".
    previous = published_previous if published == run_name(run) else published
    stats = build_run(run, args.out, previous=previous, max_step=args.steps, factor=args.factor)
    duration = time.monotonic() - started
    summary([
        '### Wind data',
        f'- Run: **{run_name(run)}** (previous: {previous or "none"})',
        f'- Downloaded: {stats.downloaded_files} files, {stats.downloaded_bytes / 1e6:.1f} MB',
        f'- Written: {stats.written_files} files, {stats.written_bytes / 1e6:.1f} MB',
        f'- Duration (build): {duration:.0f} s',
    ])
    set_output('run', run_name(run))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PipelineError as err:
        print(f'::error::Wind pipeline failed: {err}', file=sys.stderr)
        sys.exit(1)
