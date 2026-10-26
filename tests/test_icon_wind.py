"""Unit tests for pipeline/icon_wind.py (no network, no ecCodes). Run: python -m unittest discover -s tests -p 'test_*.py'"""

import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'pipeline'))
import icon_wind as iw  # noqa: E402

RUN = datetime(2026, 10, 12, 0, tzinfo=timezone.utc)
NAME = '2026101200'


def png_roundtrip(pixels: np.ndarray) -> np.ndarray:
    """Encode to PNG and read back unchanged (no premultiplied alpha), as the browser must."""
    return np.asarray(Image.open(io.BytesIO(iw.png_bytes(pixels))))


class Encoding(unittest.TestCase):
    LIMITS = {'u': 0.3, 'v': 0.3, 'gust': 0.3, 't2m': 0.5, 'precip': 0.1, 'clct': 0.5, 'pmsl': 0.5}

    def test_png_roundtrip_all_variables(self):
        rng = np.random.default_rng(1)
        fields = {k: rng.uniform(lo, hi, (37, 53)) for k, (lo, hi, _) in iw.ENCODING.items()}
        for names in (iw.WIND_CHANNELS, iw.WEATHER_CHANNELS):
            back = png_roundtrip(iw.encode_channels(fields, names))
            self.assertEqual(back.shape, (37, 53, len(names)))
            for i, name in enumerate(names):
                lo, hi, _ = iw.ENCODING[name]
                err = np.abs(iw.decode(back[..., i], lo, hi) - fields[name]).max()
                self.assertLess(err, self.LIMITS[name], name)

    def test_alpha_channel_survives_even_when_transparent(self):
        # pmsl at the bottom of its range gives alpha 0 – the other channels must not be lost.
        fields = {'t2m': np.full((2, 2), 20.0), 'precip': np.full((2, 2), 3.0), 'clct': np.full((2, 2), 50.0), 'pmsl': np.full((2, 2), 940.0)}
        back = png_roundtrip(iw.encode_channels(fields, iw.WEATHER_CHANNELS))
        self.assertEqual(back[0, 0, 3], 0)
        self.assertAlmostEqual(iw.decode(back[0, 0, 0], -40, 45), 20.0, delta=0.5)
        self.assertAlmostEqual(iw.decode(back[0, 0, 1], 0, 20), 3.0, delta=0.1)

    def test_values_outside_the_range_are_clipped(self):
        self.assertEqual(list(iw.encode(np.array([-99.0, 0.0, 99.0]), -30, 30)), [0, 128, 255])
        self.assertEqual(iw.encode(np.array([45.0]), 0, 40)[0], 255)


class Precipitation(unittest.TestCase):
    def test_deaccumulation(self):
        accum = [np.array([0.0, 0.0]), np.array([1.2, 0.0]), np.array([1.2, 0.5]), np.array([3.0, 0.5]), np.array([2.99, 0.6])]
        prev, out = None, []
        for a in accum:
            out.append(iw.deaccumulate(a, prev))
            prev = a
        np.testing.assert_allclose(out, [[0, 0], [1.2, 0], [0, 0.5], [1.8, 0], [0, 0.1]], atol=1e-9)
        self.assertTrue(all((o >= 0).all() for o in out), 'never negative (packing noise)')

    def test_first_step_is_zero(self):
        np.testing.assert_array_equal(iw.deaccumulate(np.array([0.3, 2.0]), None), [0, 0])


# ---------------------------------------------------------------------------------------------- fakes


def listing_for(runs: dict[datetime, int], missing: set[tuple[str, int]] = frozenset()):
    """Fake DWD listing: each run has steps 0..max_step for all variables, minus (var, step) in `missing`."""

    def listing(url: str) -> set[str]:
        hh, directory = re.search(r'/(\d\d)/(\w+)/$', url).groups()
        names = set()
        for run, last in runs.items():
            if f'{run.hour:02d}' != hh:
                continue
            for s in range(last + 1):
                if (directory, s) not in missing:
                    names.add(iw.FILE_TEMPLATE.format(run=iw.run_name(run), step=s, var=directory.upper()))
        return names

    return listing


GRID = iw.Grid(west=0.0, north=10.0, dlon=0.5, dlat=0.5, width=300, height=200)  # tiles 3 × 2 at size 128


def fake_fetch(url: str) -> bytes:
    m = re.search(r'_(\d{10})_(\d{3})_(\w+)\.grib2\.bz2$', url)
    if m:
        return json.dumps({'run': m[1], 'step': int(m[2]), 'var': m[3]}).encode()
    if 'FR_LAND' in url:
        return json.dumps({'land': True}).encode()
    raise iw.PipelineError(f'unexpected url {url}')


def fake_decoder(fail: tuple[str, int] | None = None, wrong_step: tuple[str, int] | None = None):
    """Fields with known values: u = 3, v = −4, gust = 7, t = 15 °C, precipitation 0.5 mm per hour, …"""

    def decode(blob: bytes) -> iw.Field:
        d = json.loads(blob)
        if d.get('land'):
            land = np.zeros((GRID.height, GRID.width))
            land[150:, 0:100] = 1  # land only in the lower-left tile 1_0 …
            land[10, 200] = 0.2  # … and one island point in tile 0_1
            return iw.Field(land, GRID, NAME, 0)
        var, step = d['var'].lower(), d['step']
        if fail == (var, step):
            raise RuntimeError('ECCODES ERROR: corrupt message')
        values = {
            'u_10m': 3.0,
            'v_10m': -4.0,
            'vmax_10m': 7.0,
            't_2m': 288.15,
            'tot_prec': 0.5 * step,
            'clct': 40.0,
            'pmsl': 101300.0,
        }[var]
        reported = step + 1 if wrong_step == (var, step) else step
        return iw.Field(np.full((GRID.height, GRID.width), values), GRID, d['run'], reported)

    return decode


# ---------------------------------------------------------------------------------------------- publishing


class RunSelection(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 7, 40, tzinfo=timezone.utc)

    def test_candidates_are_the_last_runs_newest_first(self):
        runs = iw.candidate_runs(self.NOW, 3)
        self.assertEqual([iw.run_name(r) for r in runs], ['2026101206', '2026101203', '2026101200'])

    def test_partial_newest_run_is_skipped(self):
        r06, r03 = iw.candidate_runs(self.NOW, 2)
        listing = listing_for({r06: 48, r03: 48}, missing={('vmax_10m', 48)})  # missing in both hours' dirs …
        listing_ok03 = listing_for({r06: 48, r03: 48}, missing=set())

        def mixed(url):  # … but only the 06 run is really incomplete
            return listing(url) if '/06/' in url else listing_ok03(url)

        self.assertEqual(iw.select_run([r06, r03], mixed, None), r03)

    def test_run_with_too_few_steps_is_skipped(self):
        r06, r03 = iw.candidate_runs(self.NOW, 2)
        self.assertEqual(iw.select_run([r06, r03], listing_for({r06: 30, r03: 51}), None), r03)

    def test_nothing_complete_publishes_nothing(self):
        r06, r03 = iw.candidate_runs(self.NOW, 2)
        self.assertIsNone(iw.select_run([r06, r03], listing_for({r06: 47, r03: 20}), None))

    def test_already_published_run_is_not_rebuilt(self):
        r06, r03 = iw.candidate_runs(self.NOW, 2)
        listing = listing_for({r06: 40, r03: 48})
        self.assertIsNone(iw.select_run([r06, r03], listing, '2026101203'))
        self.assertEqual(iw.select_run([r06, r03], listing, '2026101200'), r03)

    def test_published_runs_reads_latest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'latest.json'
            self.assertEqual(iw.published_runs(p), (None, None))
            p.write_text('{"run": "2026101203", "previous": "2026101200"}')
            self.assertEqual(iw.published_runs(p), ('2026101203', '2026101200'))
            p.write_text('{"run": "../../etc"}')
            self.assertEqual(iw.published_runs(p), (None, None))
            p.write_text('not json')
            self.assertEqual(iw.published_runs(p), (None, None))


class Build(unittest.TestCase):
    def build(self, out: Path, **kw):
        return iw.build_run(RUN, out, previous='2026101121', fetch=fake_fetch, max_step=3, workers=2, **kw)

    def test_failed_decoding_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'latest.json').write_text('{"run": "2026101121"}')
            with self.assertRaises(iw.PipelineError):
                self.build(out, decoder=fake_decoder(fail=('pmsl', 2)))
            self.assertEqual(json.loads((out / 'latest.json').read_text())['run'], '2026101121', 'latest.json untouched')
            self.assertEqual(sorted(p.name for p in out.iterdir()), ['latest.json'], 'no partial run folder left')

    def test_file_of_another_step_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaisesRegex(iw.PipelineError, 'contains run'):
                self.build(out, decoder=fake_decoder(wrong_step=('u_10m', 1)))
            self.assertFalse((out / 'latest.json').exists())

    def test_missing_file_never_publishes(self):
        def fetch(url):
            if url.endswith('_002_CLCT.grib2.bz2'):
                raise iw.PipelineError(f'not found: {url}')
            return fake_fetch(url)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(iw.PipelineError):
                iw.build_run(RUN, out, fetch=fetch, decoder=fake_decoder(), max_step=3, workers=2)
            self.assertEqual(list(out.iterdir()), [])

    def test_successful_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            stats = self.build(out, decoder=fake_decoder())
            latest = json.loads((out / 'latest.json').read_text())
            self.assertEqual(latest, {**latest, 'run': NAME, 'meta': f'{NAME}/meta.json', 'previous': '2026101121', 'runTime': '2026-10-12T00:00:00Z'})
            meta = json.loads((out / NAME / 'meta.json').read_text())
            self.assertEqual(sorted(meta['tiles']['index']), ['0_1', '1_0'], 'tiles without land are skipped')
            self.assertEqual(meta['tiles']['index']['1_0'], {'x0': 0, 'y0': 128, 'width': 128, 'height': 72, 'west': 0.0, 'east': 63.5, 'north': -54.0, 'south': -89.5})
            self.assertEqual((meta['map']['width'], meta['map']['height']), (150, 100))
            self.assertEqual([s['time'] for s in meta['steps']], ['2026-10-12T00:00:00Z', '2026-10-12T01:00:00Z', '2026-10-12T02:00:00Z', '2026-10-12T03:00:00Z'])
            self.assertEqual(stats.written_files, 4 * (1 + 2 * 2) + 1)

            m = np.asarray(Image.open(out / NAME / 'map' / 'step_002.png'))
            self.assertEqual(m.shape, (100, 150, 3))
            r = meta['encoding']['ranges']
            self.assertAlmostEqual(iw.decode(m[5, 5, 0], r['u']['min'], r['u']['max']), 3.0, delta=0.3)
            self.assertAlmostEqual(iw.decode(m[5, 5, 1], r['v']['min'], r['v']['max']), -4.0, delta=0.3)
            self.assertAlmostEqual(iw.decode(m[5, 5, 2], r['gust']['min'], r['gust']['max']), 7.0, delta=0.3)

            w0 = np.asarray(Image.open(out / NAME / 'tiles' / '1_0' / 'weather_000.png'))
            w2 = np.asarray(Image.open(out / NAME / 'tiles' / '1_0' / 'weather_002.png'))
            self.assertEqual(w2.shape, (72, 128, 4))
            self.assertAlmostEqual(iw.decode(w2[0, 0, 0], -40, 45), 15.0, delta=0.5)
            self.assertAlmostEqual(iw.decode(w0[0, 0, 1], 0, 20), 0.0, delta=0.1)
            self.assertAlmostEqual(iw.decode(w2[0, 0, 1], 0, 20), 0.5, delta=0.1, msg='mm/h, not accumulated')
            self.assertAlmostEqual(iw.decode(w2[0, 0, 3], 950, 1050), 1013.0, delta=0.5)


# ---------------------------------------------------------------------------------------------- ICON global edge

GLOBAL_RUN = (RUN, 0)
AREA = iw.Grid(west=-10.0, north=20.0, dlon=0.5, dlat=0.5, width=320, height=220)  # GRID from x0 = y0 = 20
LAT, LON = (a.ravel() for a in np.meshgrid(np.arange(-90.0, 90.1, 1.0), np.arange(-180.0, 180.0, 1.0), indexing='ij'))


def global_fetch(url: str) -> bytes:
    m = re.search(r'icon_global_icosahedral_time-invariant_(\d{10})_(\w+)\.grib2\.bz2$', url)
    if m:
        return json.dumps({'global': True, 'run': m[1], 'const': m[2]}).encode()
    m = re.search(r'icon_global_icosahedral_single-level_(\d{10})_(\d{3})_(\w+)\.grib2\.bz2$', url)
    if m:
        return json.dumps({'global': True, 'run': m[1], 'step': int(m[2]), 'var': m[3]}).encode()
    return fake_fetch(url)


def global_decoder(blob: bytes) -> iw.Field:
    """ICON-EU as fake_decoder; ICON global on a 1° lattice: u = 5, v = 2, gust = 9, t = 5 °C, 1 mm per hour, …"""
    d = json.loads(blob)
    if not d.get('global'):
        return fake_decoder()(blob)
    if 'const' in d:
        values = {'CLAT': LAT, 'CLON': LON, 'FR_LAND': (LAT > 15).astype(float)}[d['const']]
        return iw.Field(values, None, d['run'], 0)
    var, step = d['var'].lower(), d['step']
    if var == 'vmax_10m' and step == 0:
        raise AssertionError('ICON global has no gust at step 0')
    value = {'u_10m': 5.0, 'v_10m': 2.0, 'vmax_10m': 9.0, 't_2m': 278.15, 'tot_prec': 1.0 * step, 'clct': 80.0, 'pmsl': 100000.0}[var]
    return iw.Field(np.full(LAT.shape, value), None, d['run'], step)


class GlobalEdge(unittest.TestCase):
    def test_area_holds_icon_eu_on_its_points(self):
        eu = iw.Grid(-23.5, 70.5, 0.0625, 0.0625, 1377, 657)
        self.assertEqual((iw.AREA.east, iw.AREA.south), (62.5, 29.5))
        self.assertLessEqual(iw.AREA.west, -24.6, 'all of Iceland')
        self.assertGreaterEqual(iw.AREA.north, 71.2, 'North Cape')
        self.assertEqual(((eu.west - iw.AREA.west) / 0.0625, (iw.AREA.north - eu.north) / 0.0625), (104.0, 72.0))

    def test_eu_weights_fade_only_towards_the_ring(self):
        area = iw.Grid(0, 10, 1, 1, 12, 10)
        w = iw.eu_weights(area, 2, 3, 10, 7, 4)  # ICON-EU reaches the area's east and south edge
        self.assertEqual(w[:3].max(), 0)
        self.assertEqual(w[:, :2].max(), 0)
        np.testing.assert_allclose(w[9, 2:7], [0, 0.25, 0.5, 0.75, 1])  # from the west edge inwards
        np.testing.assert_allclose(w[3:8, 11], [0, 0.25, 0.5, 0.75, 1])  # from the north edge downwards
        self.assertEqual(w[9, 11], 1, 'no fading at the area edge')

    def test_composite_interpolates_and_blends(self):
        comp = iw.composite(AREA, GRID, LAT, LON, blend=4)
        self.assertEqual((comp.x0, comp.y0), (20, 20))
        ring = comp.interpolate(np.full(LAT.shape, 5.0))
        np.testing.assert_allclose(ring, 5.0)
        lon_field = comp.interpolate(LON)  # smooth field: the interpolation follows it
        ty, tx = np.divmod(comp.targets, AREA.width)
        mid = np.abs(AREA.north - ty * AREA.dlat) < 60  # away from the poles, where the longitude is degenerate
        self.assertLess(np.abs(lon_field - (AREA.west + tx * AREA.dlon))[mid].max(), 0.6)
        out = comp.compose(np.full((GRID.height, GRID.width), 3.0), ring)
        self.assertEqual(out.shape, (AREA.height, AREA.width))
        self.assertAlmostEqual(out[0, 0], 5.0)
        np.testing.assert_allclose(out[100, 18:26], [5, 5, 5, 4.5, 4, 3.5, 3, 3])
        self.assertEqual(out[219, 319], 3.0)

    def test_misaligned_icon_eu_is_rejected(self):
        with self.assertRaisesRegex(iw.PipelineError, 'does not lie'):
            iw.composite(AREA, iw.Grid(0.25, 10.0, 0.5, 0.5, 300, 200), LAT, LON)
        with self.assertRaisesRegex(iw.PipelineError, 'does not lie'):
            iw.composite(AREA, iw.Grid(0.0, 10.0, 0.5, 0.5, 400, 200), LAT, LON)

    def test_global_candidates_and_files(self):
        runs = iw.global_candidates(datetime(2026, 10, 12, 9, tzinfo=timezone.utc))
        self.assertEqual([(iw.run_name(g), o) for g, o in runs], [('2026101206', 3), ('2026101200', 9), ('2026101118', 15)])
        files = iw.global_files(RUN, 0, 3)
        self.assertEqual(files['vmax_10m'], [iw.GLOBAL_TEMPLATE.format(run=NAME, step=s, var='VMAX_10M') for s in (1, 2, 3)])
        self.assertEqual(len(files['u_10m']), 4)
        self.assertEqual(files['clat'], [f'icon_global_icosahedral_time-invariant_{NAME}_CLAT.grib2.bz2'])

    def test_select_global_takes_the_newest_complete_run(self):
        r06 = datetime(2026, 10, 12, 6, tzinfo=timezone.utc)
        r00 = datetime(2026, 10, 12, 0, tzinfo=timezone.utc)
        full = {r06: iw.global_files(r06, 0, 4), r00: iw.global_files(r00, 6, 4)}

        def listing(without: set[datetime]):
            def names(url: str) -> set[str]:
                hh, directory = re.search(r'/(\d\d)/(\w+)/$', url).groups()
                return {f for run, files in full.items() if run not in without and f'{run.hour:02d}' == hh for f in files.get(directory, [])}

            return names

        self.assertEqual(iw.select_global(r06, listing(set()), 4), (r06, 0))
        self.assertEqual(iw.select_global(r06, listing({r06}), 4), (r00, 6))
        self.assertIsNone(iw.select_global(r06, listing({r06, r00}), 4))

    def test_build_with_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            iw.build_run(RUN, out, global_run=GLOBAL_RUN, area=AREA, blend=4, fetch=global_fetch, decoder=global_decoder, max_step=3, workers=2)
            meta = json.loads((out / NAME / 'meta.json').read_text())
            self.assertEqual(meta['grid'], {**meta['grid'], 'width': 320, 'height': 220, 'west': -10.0, 'north': 20.0})
            self.assertEqual((meta['map']['width'], meta['map']['height']), (160, 110))
            self.assertEqual(meta['model'], 'ICON-EU + ICON')
            self.assertIn('ICON-EU und ICON', meta['attribution']['de'])
            self.assertEqual(meta['sources']['ICON'], {**meta['sources']['ICON'], 'run': NAME, 'stepOffset': 0, 'blend': 4})
            self.assertEqual(meta['sources']['ICON-EU']['west'], 0.0)
            # land: ICON-EU tiles moved by (20, 20) points, plus the ICON global land north of 15° N (rows 0…9)
            self.assertIn('0_0', meta['tiles']['index'])
            self.assertEqual(meta['tiles']['index']['0_0']['north'], 20.0)

            r = meta['encoding']['ranges']
            m = np.asarray(Image.open(out / NAME / 'map' / 'step_002.png'))
            self.assertEqual(m.shape, (110, 160, 3))
            u = lambda y, x: iw.decode(m[y, x, 0], r['u']['min'], r['u']['max'])  # noqa: E731
            self.assertAlmostEqual(u(2, 2), 5.0, delta=0.3, msg='ring: ICON global')
            self.assertAlmostEqual(u(60, 60), 3.0, delta=0.3, msg='inside: ICON-EU')
            self.assertAlmostEqual(u(60, 11), 4.0, delta=0.3, msg='half way into the fade (point 22 of the area)')

            g0 = np.asarray(Image.open(out / NAME / 'tiles' / '0_0' / 'wind_000.png'))
            self.assertAlmostEqual(iw.decode(g0[0, 0, 2], 0, 40), 9.0, delta=0.3, msg='gust of step 1 at step 0')
            w2 = np.asarray(Image.open(out / NAME / 'tiles' / '0_0' / 'weather_002.png'))
            self.assertAlmostEqual(iw.decode(w2[0, 0, 0], -40, 45), 5.0, delta=0.5)
            self.assertAlmostEqual(iw.decode(w2[0, 0, 1], 0, 20), 1.0, delta=0.1, msg='ICON global mm/h, not accumulated')
            self.assertAlmostEqual(iw.decode(w2[0, 0, 3], 950, 1050), 1000.0, delta=0.5)

    def test_global_file_of_another_run_is_rejected(self):
        def fetch(url):
            return global_fetch(url.replace(f'icosahedral_single-level_{NAME}_002', 'icosahedral_single-level_2026101118_002'))

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaisesRegex(iw.PipelineError, 'ICON .* file contains run 2026101118'):
                iw.build_run(RUN, out, global_run=GLOBAL_RUN, area=AREA, blend=4, fetch=fetch, decoder=global_decoder, max_step=3, workers=2)
            self.assertEqual(list(out.iterdir()), [])


class TileLookup(unittest.TestCase):
    META = {
        'grid': {'west': -23.5, 'north': 70.5, 'dlon': 0.0625, 'dlat': 0.0625, 'width': 1377, 'height': 657},
        'tiles': {'size': 128, 'index': {'2_5': {}, '5_10': {}}},
    }

    def test_lookup_in_tile_without_land_is_none(self):
        # Berlin 52.52 N 13.405 E → grid x 590, y 288 → tile 2_4, which is not in this index
        self.assertIsNone(iw.tile_lookup(self.META, 52.52, 13.405))
        self.assertEqual(iw.tile_lookup({**self.META, 'tiles': {'size': 128, 'index': {'2_4': {}}}}, 52.52, 13.405), ('2_4', 78, 32))

    def test_lookup_in_indexed_tile(self):
        # tile 2_5 covers x 640…767, y 256…383; point x = 700, y = 300 → lon −23.5 + 700·0.0625 = 20.25, lat 70.5 − 18.75 = 51.75
        self.assertEqual(iw.tile_lookup(self.META, 51.75, 20.25), ('2_5', 60, 44))
        # nearest grid point: a bit off still rounds to the same pixel
        self.assertEqual(iw.tile_lookup(self.META, 51.75 + 0.03, 20.25 - 0.03), ('2_5', 60, 44))
        # south-east corner of the grid → last (partial) tile 5_10, pixel (96, 16)
        self.assertEqual(iw.tile_lookup(self.META, 29.5, 62.5), ('5_10', 96, 16))

    def test_lookup_outside_grid(self):
        self.assertIsNone(iw.tile_lookup(self.META, 75.0, 10.0))
        self.assertIsNone(iw.tile_lookup(self.META, 50.0, -40.0))

    def test_layout_matches_lookup(self):
        grid = iw.Grid(-23.5, 70.5, 0.0625, 0.0625, 1377, 657)
        index = iw.tile_layout(grid, 128, None)
        self.assertEqual(len(index), 11 * 6)
        self.assertEqual(index['5_10'], {'x0': 1280, 'y0': 640, 'width': 97, 'height': 17, 'west': 56.5, 'east': 62.5, 'north': 30.5, 'south': 29.5})
        meta = {'grid': grid.as_json(), 'tiles': {'size': 128, 'index': index}}
        for lat, lon in [(52.52, 13.405), (47.0, 11.0), (64.1, -21.9), (35.0, 33.0)]:
            tid, px, py = iw.tile_lookup(meta, lat, lon)
            t = index[tid]
            self.assertLess(abs(t['west'] + px * 0.0625 - lon), 0.0625 / 2 + 1e-9)
            self.assertLess(abs(t['north'] - py * 0.0625 - lat), 0.0625 / 2 + 1e-9)


if __name__ == '__main__':
    unittest.main()
