# roarz-wind

Wind and weather data for the map layer of [roarz.net](https://roarz.net): the 10 m wind of the DWD model ICON-EU
(Europe, about 7 km grid) as PNG textures, published on **https://wind.roarz.net/** (own subdomain and folder on the
webspace, own FTP account). The app (a separate, private repository) reads them from there, on www.roarz.net and
dev.roarz.net alike; the browser never talks to the DWD.

## Pipeline

`.github/workflows/wind.yml` runs every hour (cron `20 * * * *`; DWD publishes a run every 3 hours, the hourly check
picks it up soon after it is complete and covers scheduled runs that GitHub drops) and on demand, on `main`, with the
FTP secrets of the environment `live`:

1. `.github/scripts/ftp-wind.sh fetch` reads the published `latest.json`.
2. `pipeline/icon_wind.py` looks for the newest run whose files for **all** variables (u_10m, v_10m, vmax_10m, t_2m,
   tot_prec, clct, pmsl) and **all** steps 000–048 exist on opendata.dwd.de. Only if it is newer than the published
   one, it downloads (≈ 340 MB), decodes the GRIB2 files (ecCodes), turns tot_prec into mm/h and writes
   - `<run>/map/step_000.png … step_048.png`: map texture, every 2nd grid point (689 × 329), R = u, G = v
     (−30…+30 m/s), B = gust (0…40 m/s), row 0 = north;
   - `<run>/tiles/<ty>_<tx>/wind_NNN.png` (same encoding) and `weather_NNN.png` (R = t_2m −40…+45 °C,
     G = precipitation 0…20 mm/h, B = cloud cover 0…100 %, A = pmsl 950…1050 hPa): full resolution, 128 × 128 grid
     points per tile; tiles without land (DWD land fraction) are left out. Decode them without premultiplied alpha.
   - `<run>/meta.json` (run, steps with valid times, grid, value ranges, tile index, attribution) and, last,
     `latest.json`.
3. `.github/scripts/ftp-wind.sh publish` uploads `web/.htaccess` (CORS for the app, cache headers: JSON 5 min, PNG
   1 year, immutable) and the run folder (lftp mirror, 8 parallel connections), checks that every file arrived, then
   switches `latest.json` and deletes all run folders but the new and the previous one.

Any error (missing file, decoding, upload) fails the job and leaves the published data untouched. Per run: about
6,000 files and 110 MB; on the server at most three runs at a time. The job summary logs the volume, file count and
duration. Byte b of a channel stands for `min + b / 255 · (max − min)`; the details are in the header of
`pipeline/icon_wind.py` and in `meta.json`.

## Setup

Environment `live` (Settings → Environments), deployment branches: `main` only.

| Name | Kind | Value |
| --- | --- | --- |
| `FTP_HOST`, `FTP_USER`, `FTP_PASSWORD` | secrets | FTP account of the subdomain wind.roarz.net (reaches only its folder) |
| `FTP_PATH` | variable, optional | folder the account starts in (default `/`) |
| `WIND_DIR` | variable, optional | web folder of wind.roarz.net relative to `FTP_PATH` (default `.`) |

The subdomain needs HTTPS (Let's Encrypt) – the app only loads `https://wind.roarz.net/`.

The workflow never runs for pull requests, so forks never see the secrets. GitHub disables scheduled workflows of
public repositories after 60 days without activity; the workflow re-enables itself on every scheduled run.

**First run:** Actions → "Wind data" → "Run workflow" (optionally a run `YYYYMMDDHH`).

## Develop locally

```sh
pip install -r pipeline/requirements.txt
python pipeline/icon_wind.py --out build/wind          # full run: ≈ 340 MB download
python -m unittest discover -s tests -p 'test_*.py'     # pipeline (no network, no ecCodes)
bash tests/ftp_wind.test.sh                             # upload script (dry run, no server)
```

Copy `build/wind/*` to `public/wind/` of the app to try it there (without a configured URL the app reads `wind/`).

## Data source

Deutscher Wetterdienst (DWD), ICON-EU, [opendata.dwd.de](https://opendata.dwd.de/weather/nwp/icon-eu/grib/),
licence [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The app shows the attribution from `meta.json`.
