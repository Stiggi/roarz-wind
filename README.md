# roarz-wind

Wind and weather data for the map layer of [roarz.net](https://roarz.net): the 10 m wind of the DWD model ICON-EU
(Europe, about 7 km grid), edged with ICON global (about 13 km) where ICON-EU ends (all of Iceland, the North Cape) as PNG textures, uploaded to `public/wind/` of the roarz live webspace. The app (a separate,
private repository) reads them from `https://www.roarz.net/wind/` (dev.roarz.net too, via CORS); the browser never
talks to the DWD.

## Pipeline

`.github/workflows/wind.yml` runs every hour (cron `20 * * * *`; DWD publishes a run every 3 hours, the hourly check
picks it up soon after it is complete and covers scheduled runs that GitHub drops) and on demand, on `main`, with the
FTP secrets of the environment `live`:

1. `.github/scripts/ftp-wind.sh fetch` reads the published `latest.json`.
2. `pipeline/icon_wind.py` looks for the newest run whose files for **all** variables (u_10m, v_10m, vmax_10m, t_2m,
   tot_prec, clct, pmsl) and **all** steps 000–048 exist on opendata.dwd.de. Only if it is newer than the published
   one, it downloads it (≈ 340 MB) and the same variables of the matching ICON global run (≈ 1.1 GB, triangular
   grid; the same run or an earlier one, steps shifted), decodes the GRIB2 files (ecCodes), turns tot_prec into mm/h,
   puts both on the 0.0625° points of the area lon −30…62.5, lat 29.5…75 (ICON-EU on its own points, outside it ICON
   global interpolated from the 4 nearest cells, faded into ICON-EU over 1° at its west and north edge) and writes
   - `<run>/map/step_000.png … step_048.png`: map texture, every 2nd grid point (741 × 365), R = u, G = v
     (−30…+30 m/s), B = gust (0…40 m/s), row 0 = north;
   - `<run>/tiles/<ty>_<tx>/wind_NNN.png` (same encoding) and `weather_NNN.png` (R = t_2m −40…+45 °C,
     G = precipitation 0…20 mm/h, B = cloud cover 0…100 %, A = pmsl 950…1050 hPa): full resolution, 128 × 128 grid
     points per tile; tiles without land (DWD land fraction) are left out. Decode them without premultiplied alpha.
   - `<run>/meta.json` (run, steps with valid times, grid, value ranges, tile index, attribution, `sources`) and, last,
     `latest.json`.
3. `.github/scripts/ftp-wind.sh publish` uploads the run folder (lftp mirror, 8 parallel connections), checks that
   every file arrived, then switches `latest.json` and deletes all run folders but the new and the previous one. The
   folder's `.htaccess` (CORS, cache headers) is deployed by the app and never touched here.

Any error (missing file, decoding, upload) fails the job and leaves the published data untouched. Without a complete
ICON global run the ICON-EU area alone is published (a warning in the job). Per run: about 6,300 files and 120 MB; on the server at most three runs at a time. The job summary logs the volume, file count and
duration. Byte b of a channel stands for `min + b / 255 · (max − min)`; the details are in the header of
`pipeline/icon_wind.py` and in `meta.json`.

## Setup

Environment `live` (Settings → Environments), deployment branches: `main` only.

| Name | Kind | Value |
| --- | --- | --- |
| `FTP_HOST`, `FTP_USER`, `FTP_PASSWORD` | secrets | FTP account that reaches only `public/wind/` (at webgo bound to the subdomain wind.roarz.net, which points there) |
| `FTP_PATH` | variable, optional | folder the account starts in (default `/`) |
| `WIND_DIR` | variable, optional | wind folder relative to `FTP_PATH` (default `.`) |

The workflow never runs for pull requests, so forks never see the secrets. GitHub disables scheduled workflows of
public repositories after 60 days without activity; the workflow re-enables itself on every scheduled run.

**First run:** Actions → "Wind data" → "Run workflow" (optionally a run `YYYYMMDDHH`).

## Develop locally

```sh
pip install -r pipeline/requirements.txt
python pipeline/icon_wind.py --out build/wind          # full run: ≈ 1.5 GB download
python -m unittest discover -s tests -p 'test_*.py'     # pipeline (no network, no ecCodes)
bash tests/ftp_wind.test.sh                             # upload script (dry run, no server)
```

Copy `build/wind/*` to `public/wind/` of the app to try it there (without a configured URL the app reads `wind/`).

## Data source

Deutscher Wetterdienst (DWD), ICON-EU and ICON, [opendata.dwd.de](https://opendata.dwd.de/weather/nwp/),
licence [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The app shows the attribution from `meta.json`.
