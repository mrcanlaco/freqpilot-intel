# FreqPilot Intel

An evidence-first research workspace for public Binance ecosystem material.
Store provenance, compare source statements and keep personal research in your
browser. **No trading or automatic publishing.**

[Hosted application](https://intel.freqpilot.dev) · [Architecture](docs/ARCHITECTURE.md)
· [Data policy](docs/DATA_POLICY.md) · [Operations](docs/OPERATIONS.md)

The interface is Vietnamese. The application separates three evidence pillars:
community engagement, explicitly qualified public performance observations and
official recognition. A content award is not audited trading performance; missing
evidence stays unknown. Deterministic keyword labels are not AI translations or
validated investment signals.

## v0.5.0: Compact Square-style posts

- Home, source-library and followed-source cards show the author, original
  publication time, a compact excerpt, original images and visible engagement.
- **Hiện thêm trên Binance** opens the full original post in a new tab.
  **Bản lưu & bằng chứng** opens the application's permitted excerpt and research
  audit. Full normalized evidence bodies remain private, not public HTTP fields.
- Views, likes, comments and shares are read-only observed snapshots, not live
  counters or interaction buttons. Unknown values display **Chưa rõ**; a recorded
  zero stays zero. The metric observation time is separate from publication time.

### Original Binance images and drafting

- Source cards and the reader display captured post images directly from the
  original Binance CDN. The server stores URLs only: no image hosting, image
  proxy, substitute images or automatic retries around a blocked CDN.
  **Hôm nay** suggests collected sources with images; the full source library
  continues to include text-only posts without inventing cover images.
- In the composer, select up to four images from cited sources. Selected links
  appear in the preview, local draft thumbnails and private backup. Existing v1
  text drafts remain readable and migrate on explicit save.
- Text-only drafts export `.txt`. Image drafts export `bti-square-post-v1` JSON
  containing post text, original image URLs and source attribution/revision.
  Clipboard still copies text only; neither format posts or embeds images into
  Square automatically. Private notes are excluded from post exports.
- Export rechecks both source permissions/revisions and image membership in the
  current captured source. Original remote image bytes may change or disappear;
  a captured URL is not an immutable copy or a license to republish.

- **Hôm nay** is the starting page. Pick a public source or choose **Tạo bài Square**.
  The five primary destinations separate sources, drafts, research and operations.
- Edit the title and body alongside source cards and the final-text preview.
  Mobile uses **Nguồn / Soạn bài / Xem trước** with persistent save/export actions.
- Drafts autosave in this browser and appear in **Bài viết**. Search, reopen,
  delete or export/import a private JSON backup. There is no cross-device sync.
  Storage failures preserve the saved draft and pending text with a recovery download.
- Copy or export only after rechecking public permission and exact source
  revisions. Authored text stays yours; private notes never enter the post.
- **Kho nguồn → Gợi ý bài viết** retains deterministic Vietnamese templates
  (not AI). Choose **Soạn từ gợi ý này** to create an independent editable draft;
  subsequent filter changes cannot overwrite it.
- Export the same `bti-intelligence-brief-v1` package through read-only
  `GET /api/brief` or `python -m bti export-brief`. The browser can download JSON
  for a separately operated publishing tool; this application does not publish.
- Personal research can also export Square text after rechecking source versions
  and public permissions. Private notes stay in the browser.


## What this repository contains

Application source, synthetic tests, empty configuration examples and generic
deployment tooling. The hosted application's corpus, trader snapshots, raw source
captures, operator configuration, databases and deployment history are **not**
distributed here. A fresh install starts empty. Optional published-report links
return 404 until an operator supplies a complete reviewed publication bundle.

## Run locally

Requirements: Python 3.11+. Node.js 22+ is needed only for JavaScript tests.
The application uses Python's standard library and needs no API key.

```sh
git clone https://github.com/mrcanlaco/freqpilot-intel.git
cd freqpilot-intel
python scripts/bootstrap.py
```

Bootstrap creates missing local config files and initializes an empty SQLite DB.
It never overwrites an existing config or database, fetches content, starts a
collector, or enables a tunnel. Example source lists are empty and archive
extraction is disabled.

Windows PowerShell:

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m bti serve --host 127.0.0.1 --port 8768
```

Linux/macOS:

```sh
PYTHONPATH=src python -m bti serve --host 127.0.0.1 --port 8768
```

Open **http://127.0.0.1:8768**. This is a read-only research app, not an HTTP admin
or upload service. Importing source material and enabling collection are explicit
operator CLI/configuration actions; review source access and redistribution rights
first. Do not use credentials with trading or withdrawal permissions.

## Tests

Run bootstrap with `--config-only` before testing. In PowerShell:

```powershell
./scripts/verify.ps1
```

On Linux/macOS:

```sh
python scripts/bootstrap.py --config-only
PYTHONPATH=src python -m unittest discover -s tests -v
node --check src/bti/static/app.js
node --check src/bti/static/workspace.js
node --check src/bti/static/drafts.js
node --check src/bti/static/composer.js
node --check src/bti/static/images.js
node --test tests/test_workspace.js tests/test_drafts.js tests/test_images.js
```

CI also exercises the signed executor protocol, hybrid shared authority, bounded
remote transport and supervisor cadence. Test fixtures are synthetic and do not
establish current access to Binance or any archive provider.

## Privacy and deployment

Personal research and drafts stay in localStorage for the exact origin/browser
profile. Private draft backups include notes and complete source references; keep
them private. Export before changing browsers or hostnames, then import at the new
origin. Clearing site data removes local drafts. No draft text or private note is
sent to the server; the public export API cannot include them. Research and draft
stores are independent, so this update does not migrate or overwrite existing plans.
Viewing source images sends an ordinary image request directly from the viewer's
browser to Binance's CDN, with no referrer. The CDN sees the viewer's IP address.
No private draft text is included in these requests.

Database, credentials, raw captures and generated reports remain ignored. Review
the [operations guide](docs/OPERATIONS.md) before exposing the loopback app through
a tunnel. Repository pushes do not automatically deploy to a hosted instance.

## License

Application code and project-authored documentation are licensed under
**AGPL-3.0-only**; see [LICENSE](LICENSE) and [NOTICE](NOTICE). When distributing a
modified version or offering a modified network service, comply with the applicable
source-sharing obligations. Keep a prominent link to the corresponding deployed
source. Private undistributed experiments do not require a public GitHub repository.

Third-party source content, datasets, names and trademarks retain their respective
rights. This software license does not grant permission to scrape, republish or
relicense someone else's content. FreqPilot Intel is an independent project and is
not affiliated with or endorsed by Binance. The software is provided without warranty.
