# roarz-wind – Leitfaden für Agenten

> **Synchronität:** Diese Datei existiert zweimal mit identischem Inhalt: `AGENTS.md` und `CLAUDE.md`.
> Wer eine der beiden ändert, ändert die andere im selben Commit genauso (`cp AGENTS.md CLAUDE.md`).
> Die CI (`.github/workflows/ci.yml`) schlägt fehl, sobald sich die Dateien unterscheiden.

## Zweck

Winddaten (DWD ICON-EU) für die Windebene der App roarz (eigenes, privates Repository `Stiggi/roarz`). Dieses Repo ist
**öffentlich**, damit die GitHub-Runner keine Actions-Minuten kosten. Die Daten liegen in `public/wind/` des
Live-Webspace; www.roarz.net liest `wind/`, dev.roarz.net `https://www.roarz.net/wind/` per CORS. Hochgeladen wird mit
einem eigenen FTP-Zugang, der nur diesen Ordner erreicht (bei Webgo an die Subdomain `wind.roarz.net` gebunden, die
dorthin zeigt; die App nutzt die Subdomain nicht).

## Sicherheit (öffentliches Repo)

- Zugangsdaten nur als Secrets im Environment `live` (nur Branch `main`). **Niemals** Zugangsdaten, Hostnamen oder
  Serverpfade committen, loggen oder ausgeben – die Logs der Actions sind öffentlich.
- Workflows mit Secrets laufen nur per `schedule`/`workflow_dispatch`, **nie** bei `pull_request` oder
  `pull_request_target`. `ci.yml` kommt ohne Secrets aus.
- Keine Self-hosted Runner (fremde Pull Requests könnten Code darauf ausführen).

## Branches & Ablauf

1. Nie direkt auf `main` committen: eigener Branch (`feature/…`, `fix/…`, `claude/…`) → Pull Request nach `main`.
2. `main` ist live: Der stündliche Lauf nutzt den Stand von `main`. **Vor jedem Merge nach `main` den Nutzer
   ausdrücklich fragen**, ob er sich sicher ist.
3. Kein Force-Push auf `main`.

## Pipeline

- `.github/workflows/wind.yml` (Cron stündlich + manuell; ohne neueren Lauf endet der Job nach `latest.json`) lädt in
  den Windordner (`<FTP_PATH>/<WIND_DIR>/`, Standard `/` und `.`). Ein Keep-alive-Schritt
  verhindert, dass GitHub den Cron nach 60 Tagen ohne Aktivität abschaltet.
- `pipeline/icon_wind.py` verarbeitet nur einen Lauf, dessen Dateien für **alle** Variablen und Schritte 000–048
  vorhanden sind, und nur wenn er neuer ist als der veröffentlichte. Fehler → Abbruch vor `latest.json`.
- `.github/scripts/ftp-wind.sh`: Laufordner hochladen → Vollständigkeit prüfen → `latest.json` zuletzt umschalten →
  alle Läufe außer dem neuen und dem vorherigen löschen. Das Löschen erledigt der Server (`POST` auf den Endpunkt der
  App, `WIND_CLEANUP_URL`/Secret `WIND_CLEANUP_TOKEN`, Header `X-Wind-Token`; den Token nie ausgeben); nur wenn das
  fehlschlägt, wird per FTP gelöscht. Die `.htaccess` des Ordners (CORS, Cache-Header) gehört der
  App und wird nie angefasst. Verbindung/Settings in `ftp-common.sh` (Kopie aus roarz).
- Kodierung (Wertebereiche, Kacheln, `meta.json`) siehe Kopf von `pipeline/icon_wind.py` und README. Die App dekodiert
  sie in `public/js/wind/grid.js` (roarz) – Formatänderungen nur abgestimmt mit der App und rückwärtskompatibel
  (`meta.json` trägt die Wertebereiche).
- Quellenvermerk DWD (`ATTRIBUTION`, CC BY 4.0) nicht entfernen.

## Vor dem Commit

`python -m unittest discover -s tests -p 'test_*.py'` (braucht `pip install -r pipeline/requirements.txt`) und
`bash tests/ftp_wind.test.sh`.
