# GDI-JS folder selection and scanning

This feature adds the authenticated JSON protocol used by the supplied GDI-JS
worker. The old HTML/public-folder scanner remains available as a separate source
type. It is not Google OAuth login and does not require a service account in
MovieBox: the existing GDI-JS worker handles Google Drive access.

## Configure after this change is deployed

1. Open **Settings → Google Drive Source**.
2. Choose **GDI-JS index (login + folder selection)** as the source type.
3. Set **Google Drive Index URL** to the HTTPS index root, for example
   `https://your-index.example/0:/`. Keep the trailing slash. Another drive number
   or a subfolder root is supported. Do not use a Google sharing URL here.
4. Enter the **GDI-JS account** username/password if the index requires login.
   These are not your MovieBox admin credentials or Google account credentials.
   Leave both blank for an anonymous index. Save settings.
5. Open **Tools → Google Drive Scanner → Connect / Browse**.
6. Tick one or more folders. **Open** enters a folder; **Up** and **Root** navigate.
   **Load more folders** fetches the next API page when available. To scan videos
   directly in the current folder, tick **Include this folder and its subfolders**.
7. Click **Save folder selection**, then **Start Scan**. Selecting a parent includes
   all its children, so overlapping selections are combined. Use **Full Rescan**
   to refresh already-indexed items. Missing items are not automatically deleted.
8. Later, browse again and add/remove selections, then save. Selections persist in
   MongoDB across refreshes and app restarts. Deselecting a folder does not delete
   its already-indexed media. Stop a running scan before changing source settings.

If you are using the earlier Colab notebook pinned to PR #3's commit
`9002b5f22a1b3d40b47ec8d7a06bb727ed434937`, rerunning it will deploy the OLD code.
After this feature is reviewed/merged, use its actual reviewed commit in the
notebook's `COMMIT` field. Use a fresh runtime so its recorded build ID is not
reused. Do not deploy an unreviewed moving branch accidentally.

## What was fixed

- The Drive scan handlers had been declared inside the Telegram `renderScan`
  function. They were unavailable to button handlers and page initialization.
  Drive controls now live in their own script loaded before Tools initialization.
- Folder browsing uses POST JSON with `page_token`/`page_index`, reads
  `data.files`/`nextPageToken`, and supports worker form login at `/login`.
- New media IDs store a stable canonical path, not an expiring signed link.
  Playback requests fresh file metadata and a fresh `/download.aspx` URL. Range
  and HEAD handling continue through the existing streaming proxy.
- An authenticated GDI-JS download requires the exact stream ID and URL to exist
  in the indexed library. A forged addon path cannot use server credentials to
  read an arbitrary file in the index.
  Movie/episode stream-ID indexes are created to keep this lookup efficient.
- Discovery shows pages/videos found; indexing shows processed totals. Login,
  network, API, limit and selection failures are visible instead of silently idle.
- Failed discovery and cancellation preserve existing media. A failed MongoDB
  settings save no longer reports a successful in-memory-only save.

## Credential and network handling

Index passwords are server-side MongoDB configuration, **not encrypted at rest by
this feature**. Protect MongoDB and use a dedicated index account where possible.
Passwords are masked in settings responses/templates and excluded from config
exports/restores. Blank password means keep; **Clear saved index password** removes
it. Changing the index origin requires re-entering or clearing the password.

Cookies are held only in a bounded process-memory cache. The API and addon links
do not return login cookies, passwords or signed download URLs. Credentials are
sent only to the configured HTTPS origin. Every network request validates and
pins a public DNS address; redirects are rejected instead of forwarding cookies
or login bodies to another endpoint. The uploaded worker source and its secrets
are not included in this repository.

## Limits and live verification

- This matches the supplied worker's ordinary username/password JSON login and
  same-origin signed-download API. It does not bypass Cloudflare challenges,
  external SSO, referer protection, or per-folder `.password` checks. An index
  requiring one of those flows needs a separately supported integration.
- IP-locked signed links require stable server egress; dynamic Heroku egress may
  invalidate them. Single-session worker accounts may displace browser sessions;
  prefer an account dedicated to MovieBox.
- One configured index root at a time. Up to 100 selected folders, 1,000 API pages
  and 50,000 videos per scan; API pages are capped at 4 MiB and individual API
  requests at 35 seconds. Hitting a cap reports an error, not false completion.
- The worker is path-based: duplicate names in the same Google Drive folder and
  names containing unsafe path separators are not disambiguated by this adapter.
- Folder selections persist, but an in-progress Drive scan cursor does not yet
  survive a dyno restart. Start Scan skips known stable IDs when run again.
- Filename-to-metadata matching is unchanged. If discovery succeeds but
  **Skipped (meta)** rises, check filenames/TMDB/TVDB configuration separately.
- The live index could not be opened by the available web checker. No actual
  index credentials, production database, Telegram session, or Heroku deployment
  were used to validate this feature. Test one small selected folder first, then
  video playback/seek/HEAD, a second folder, a failed scan, and a restart.

## Offline tests

```sh
.venv/bin/python -m unittest discover -s tests -v
node tests/test_gdrive_controls.cjs
node --check Backend/fastapi/static/gdrive_tools.js
uvx ruff check --select F821,F822,F823 Backend tests
.venv/bin/python -m compileall -q Backend tests
git diff --check
```

The Python fixtures cover login/session reuse, pagination, subtree scope, bad
paths, credential redirects, size limits, filtering, cancellation, fresh playback
links/ranges, indexed-media authorization and settings safety. The dependency-free
JavaScript tests use a DOM model for browse/select/save/start/stop and error states.

An additional real-browser smoke test is provided at `tests/test_gdrive_ui.cjs`.
It requires Playwright and its Chromium executable; optional `CHROMIUM_PATH` can
point to an installed Chromium. All application HTTP responses are local/mocked.
This browser test was **not run successfully in the preparation environment**:
Chromium was absent and its download timed out. Visual/mobile layout and live
deployment validation are still required; DOM-model tests do not replace them.
