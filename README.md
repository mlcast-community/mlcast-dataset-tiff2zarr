# mlcast-dataset-tiff2zarr

<!-- SPDX-License-Identifier: Apache-2.0 OR BSD-3-Clause -->

[![linting](https://github.com/mlcast-community/mlcast-dataset-tiff2zarr/actions/workflows/pre-commit.yml/badge.svg)](https://github.com/mlcast-community/mlcast-dataset-tiff2zarr/actions/workflows/pre-commit.yml)

Generic converter: a directory of GeoTIFFs → an [mlcast](https://github.com/mlcast-community)-compliant Zarr v3 sharded store.

Converts radar precipitation (or any 2-D gridded) GeoTIFF archives into
Zarr v3 stores that pass validation by
[mlcast-dataset-validator](https://github.com/mlcast-community/mlcast-dataset-validator).
CRS, grid coordinates, and lat/lon grids are extracted directly from the
GeoTIFFs — no external reference files are needed.

## Features

- Zarr v3 with sharded storage and Zstd compression
- CF-1.8 compliant output (time, x/y, lat/lon, CRS grid-mapping)
- Parallel writing with configurable worker count
- Supports `scale_factor` / `fill_value` (integer-packed rasters)
- Variable temporal frequency bands (`base_frequencies` attribute)
- Grid-size filtering to skip malformed files
- Drop-in `uv run` invocation — no install step

## Included conversion scripts

| Script | Dataset |
|--------|---------|
| `convert_it_dpc_sri.sh` | Italian DPC SRI radar (2010–2025, 1 km, 10/15 min) |
| `convert_uk_metoffice.sh` | UK Met Office C-band rain radar (2005–, 1 km, 5 min) |

## Usage

### Prerequisites

[uv](https://docs.astral.sh/uv/) must be installed.

### Run a bundled conversion script

```bash
# Edit DATA_PATH / SAVE_PATH inside the script first
bash convert_it_dpc_sri.sh
```

### Run directly

```bash
uv run python zarr_converter_v3.py \
    --data_path=/path/to/tiffs \
    --save_path=/path/to/output.zarr \
    --var_name=RR \
    --standard_name=rainfall_flux \
    --long_name="Precipitation rate" \
    --units="kg m-2 h-1" \
    --timestamp_regex='(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})-(?P<hour>\d{2})-(?P<minute>\d{2})' \
    --start_date=2010-01-01 \
    --pattern='**/*.tif' \
    --fillvalue=-1 \
    --shard_size=256 \
    --num_workers=16 \
    --compression_level=9 \
    --title="My radar dataset" \
    --license=CC-BY-SA-4.0 \
    --mlcast_created_by="Your Name <you@example.com>" \
    --mlcast_dataset_version=0.1.0 \
    --mlcast_dataset_identifier=MY-DATASET
```

### Key options

| Option | Default | Description |
|--------|---------|-------------|
| `--data_path` | required | Root directory of GeoTIFFs |
| `--save_path` | required | Output `.zarr` path |
| `--pattern` | `**/*.tif` | Glob pattern relative to `data_path` |
| `--timestamp_regex` | ISO-like | Named groups: `year`, `month`, `day`, `hour`, `minute` |
| `--start_date` | `1970-01-01` | Ignore files before this date |
| `--scale_factor` | `1.0` | Multiply raw integer values by this to get physical units |
| `--fillvalue` | `-1` | Raw nodata value (mapped to NaN) |
| `--expected_height` / `--expected_width` | `None` | Skip files with wrong grid dimensions |
| `--shard_size` | `256` | Number of timesteps per shard |
| `--num_workers` | `8` | Parallel writer processes |
| `--compression_level` | `9` | Zstd compression level (1–22) |
| `--base_frequencies` | `""` | Semicolon-separated frequency bands, e.g. `15min:2010-01-01/2014-06-25;10min:2014-06-25/None` |

## Contributing

Issues and pull requests are welcome. Please run pre-commit before submitting:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

## License

This project is dual-licensed under either:

* Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or http://www.apache.org/licenses/LICENSE-2.0)
* BSD 3-Clause License ([LICENSE-BSD](LICENSE-BSD) or https://opensource.org/licenses/BSD-3-Clause)

at your option.

See [LICENSE](LICENSE) for more details.
