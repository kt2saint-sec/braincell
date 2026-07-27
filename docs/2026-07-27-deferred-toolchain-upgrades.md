# Deferred toolchain upgrades

These upgrades are intentionally separate from the project-only completion
checkpoint because each changes third-party runtime or CI infrastructure.

## Starlette TestClient and httpx2

Starlette 1.3 warns that `starlette.testclient` with `httpx` is deprecated in
favor of the newly released `httpx2`. BrainCell runtime code does not use
Starlette TestClient; it is test-only through FastAPI. `httpx2` was released on
2026-07-23 and has not completed the project's supply-chain cooldown, so this
checkpoint keeps `httpx` and narrowly filters only
`StarletteDeprecationWarning` with the exact message. Re-evaluate the dependency
after the cooldown and remove the filter when adopted.

## GitHub Actions Node 24 majors

The workflows still use `actions/checkout@v4` and
`actions/setup-python@v5`. Their current Node 24 majors are checkout v6 and
setup-python v6. Upgrade them in a dedicated CI-only checkpoint after verifying
the hosted/self-hosted runner minimums and the complete Python 3.11–3.13
matrix. Do not combine that infrastructure change with runtime contract work.

## Native QtWebEngine prerequisites

Native renderer tests run with `QT_QPA_PLATFORM=offscreen` and Chromium's
`--no-sandbox` flag, so they do not need an X display or `xvfb-run`. The
Ubuntu 24.04 GitHub-hosted image currently includes Xvfb anyway. Local
verification found `libEGL.so.1`, Xvfb, successful QtWebEngine import, and
passing native renderer tests. No explicit CI apt step is needed now; a missing
Qt/PySide import remains an intentional `ImportError` skip, while renderer
failures after import remain test failures.

References:

- https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md
- https://github.com/actions/checkout/releases
- https://github.com/actions/setup-python/releases
