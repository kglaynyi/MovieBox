# Startup and source-safety review — 2026-09-04

Reviewed against `master` at `93438d07c664230d743b8f32c429387875ff9068`.

## Scope and fixes

The repository snapshot was checked for Python compile errors, undefined names,
template syntax, missing route templates, and defects in startup, source scanning,
playback, and access control. This is not a guarantee that every runtime path is
bug-free or a complete security audit.

- Disable runtime auto-update in startup, the Restart button, environment settings,
  and the settings UI. `update.py` remains a harmless compatibility entry point.
  No deployed files, Git history, or logs are erased at startup.
- Use only `pyproject.toml`/`uv.lock`; remove the conflicting root `requirements.txt`.
  Add a Heroku `Procfile`, install locked production dependencies during Docker
  builds, and start the installed interpreter without network dependency resolution.
- Remove artificial startup delays, bind HTTP before Telegram initialization, and
  return 503 with Retry-After until services are ready. Database initialization
  errors now propagate instead of allowing partially initialized startup.
- Skip failed extra Telegram clients rather than unpacking `None`. Report the
  number of clients actually started.
- Import the missing SettingsManager used by Drive scans. Refresh Drive entries
  by identity without deleting the library or replacing unrelated files of the
  same resolution. Rescan intentionally does not delete absent sources; remove
  stale sources explicitly after checking them.
- Stream remote media from one unread HTTP response. Preserve status/range/length
  headers and raw bytes, handle HEAD and 416, reject HTML error pages, close
  connections on disconnect, and feed Drive bytes into existing usage tracking.
- Check public DNS addresses and pin the validated address for each redirect hop;
  preserve the original Host and TLS SNI hostname. Reject embedded credentials,
  private addresses, and HTTPS downgrades. Index reads are capped at 4 MiB/page.
  Stream access and health checks do not buffer whole videos.
- Do not classify rate limits, access failures, or server errors as deleted files.
  Restrict Google URL rewriting to Google Drive hosts. Traverse directories before
  applying filename include filters, and stay within the configured folder path.
- Require admin login for stream telemetry. Enforce token expiry and quota on
  direct downloads/subtitles, not just when producing addon listings.
- Restore missing `/status` and `/stremio` templates, preserving NLYNN branding.

## Verification

```sh
uv sync --locked --no-dev
.venv/bin/python -m unittest discover -s tests -v
uvx ruff check --select F821,F822,F823 Backend tests
.venv/bin/python -m compileall -q Backend update.py tests
bash -n start.sh
git diff --check
```

30 offline regression tests pass with the locked Python 3.11 environment. They
cover full/ranged/HEAD responses, disconnect cleanup, unsafe redirects and DNS,
scan failure preservation, idempotent Drive updates, failed extra clients,
admin-only telemetry, direct-download access checks, all Python source syntax,
all template syntax, and route template existence. Application imports also pass
with fake configuration, without contacting Telegram or MongoDB.

Existing unused-import/local-variable lint findings outside this patch were not
mass-rewritten. TemplateResponse positional argument deprecation remains in
existing template handlers.

## Still needs live verification

- The supplied Heroku SIGKILL/137/H10 lines do not establish the original cause.
  Check logs before the kill for R10 boot timeout, R14/R15 memory errors, or an
  application exception. This patch is not proof that every cause of that crash
  is resolved.
- No production credentials, live MongoDB, Telegram bot, Heroku deployment, or
  real Drive playback were used in these tests. Back up the database, deploy a
  reviewed commit to staging, then test login, scan, seek, HEAD, token expiry,
  restart, and a failed/cancelled rescan before production rollout.
- The existing index crawler is an HTML-link crawler, not a verified GDI-JS JSON
  adapter. JavaScript-only/authenticated indexes may still discover no files.
  Native GDI-JS support needs the actual worker version/API and a safe sample.
  Discovery still has a 120-page cap and no persisted Drive resume cursor.
- Direct Google folder discovery uses public embedded-folder HTML, not OAuth or
  the Drive API; private folders and quota/confirmation pages are not supported.
- Existing deployment workflows are untouched. In particular, merging into
  `master` can trigger the configured Hugging Face sync. This fix branch does
  not deploy production by itself.

HTTPX documents the IP-pinning/TLS mechanism at
https://www.python-httpx.org/advanced/extensions/#sni_hostname.
Heroku package-manager guidance is at
https://devcenter.heroku.com/articles/python-support.

## GDI-JS finalization review — 2026-09-04

The open GDI-JS implementation from PR #4 (`e94f912`) was used as the base for
this follow-up. Its existing protocol fixtures and credential restrictions were
retained. Fixed the three review findings (WebDAV source-kind propagation, early
folder-navigation clicks, and backup import across index origins), and added
MongoDB page checkpoints with bounded page-by-page indexing and explicit retry.
See `docs/GDI_JS_SETUP.md` for current source setup and scan semantics; the older
HTML-only/cursor limitations above describe the previous master snapshot.

Validation: **70 offline Python tests pass on Python 3.12**, dependency-free
JavaScript control tests pass (including delayed configuration/early navigation),
undefined-name lint passes, Python/template and JavaScript syntax checks pass,
and shell/whitespace checks pass. The new CI workflow targets both Python 3.11
(the deployment version) and 3.12; local validation alone does not establish the
Python 3.11 CI result.

Real-browser QA remains unavailable: Chromium is absent and its download failed
with HTTP 502/timeouts. No production Heroku, MongoDB, Telegram or live indexed
media playback was exercised. Review the CI result and deploy the exact updated
PR commit, then test one small selected folder, seek/HEAD, WebDAV and restart/resume.
No production settings or data were changed by this review.
