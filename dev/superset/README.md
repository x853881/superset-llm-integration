# Patched Superset image

The Superset that [docker-compose.yaml](../../docker-compose.yaml) runs, for development and the
acceptance tests: `apache/superset:6.1.0` with small fixes to its frontend, plus the Postgres driver
for the Warehouse database. Otherwise it is upstream's image: the same Python, `superset` CLI,
`superset` user (uid 1000), entrypoints and configuration. Only its built frontend assets differ.

The mechanism comes from superset-git-plugin's `superset/` folder, which also carries a patch that
only its own plugin needs. This repository takes the mechanism and the fixes it wants, not that
patch.

## Build

`docker compose up --build` (or `npm run harness:replay` in `e2e/`) builds it. The first build takes
several minutes, mostly building Superset's frontend. After that an unchanged patch is a Docker
cache hit. Building needs network access (GitHub for Superset's source, npm for its dependencies)
and about 8 GB of memory for Docker, as Superset's own frontend build does.

Upstream's image and the commit it was built from are pinned together in the
[Dockerfile](Dockerfile) (`SUPERSET_IMAGE`, `SUPERSET_COMMIT`). Change neither without the other, and
check the patches still apply.

## Patches

`patches/*.patch` are applied in name order to apache/superset at the commit upstream's 6.1.0 image
was built from (`c83fb2bb1dcfac41ac51bcebd82471f4a7180d18`, tag `6.1.0`). If one doesn't apply, the
build fails.

| Patch | What it fixes |
|---|---|
| `0001-sqllab-same-millisecond-load.patch` | SQL Lab no longer stays on its loading spinner when its saved state and the server's answer carry the same millisecond. `<=` becomes `<` in `src/pages/SqlLab/index.tsx`. (superset-git-plugin's `0002`.) |

Patches may change only `superset-frontend/`; the image keeps upstream's Python.

To add or change one, work in a clone of apache/superset at `6.1.0`, then write the patch from there:

    git -C path/to/superset diff -- superset-frontend/<files> > patches/NNNN-short-name.patch
