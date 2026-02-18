# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased](https://github.com/mlcast-community/mlcast-dataset-tiff2zarr)

## [v0.1.0](https://github.com/mlcast-community/mlcast-dataset-tiff2zarr/releases/tag/v0.1.0)(https://github.com/mlcast-community/mlcast-dataset-tiff2zarr/releases/tag/v0.1.0) - 2026-02-18

### Added

- `zarr_converter_v3.py`: generic GeoTIFF → mlcast-compliant Zarr v3 sharded converter
  - CF-1.8 output with time, x/y projected coordinates, lat/lon grids, and CRS grid-mapping variable
  - Zarr v3 sharded storage with Zstd compression
  - Parallel writing via multiprocessing
  - Support for `scale_factor`, `fill_value`, variable frequency bands, and grid-size filtering
- `convert_it_dpc_sri.sh`: ready-to-use script for the Italian DPC SRI radar dataset (2010–2025, 1 km, 10/15 min)
- `convert_uk_metoffice.sh`: ready-to-use script for the UK Met Office C-band rain radar (2005–, 1 km, 5 min)
- Dual license: Apache-2.0 OR BSD-3-Clause
- pre-commit configuration (trailing-whitespace, end-of-file-fixer, isort, black, flake8)
