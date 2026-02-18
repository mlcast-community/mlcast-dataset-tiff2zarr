#!/usr/bin/env bash
# Convert UK Met Office C-band rain radar GeoTIFFs to mlcast-compliant Zarr v3.
#
# Source data: ~1.9M TIFFs in /disks/fast/uk/
#   - 1725x2175 grid (from 2005-07-05), EPSG:27700 (British National Grid), 1 km
#   - int16, nodata = -1, stored as mm/h * 32 (scale_factor = 0.03125)
#   - Filenames: metoffice-c-band-rain-radar_uk_YYYYMMDDHHmm_1km-composite.tiff
#   - 5-minute frequency throughout
#   - 3 reversion days with wrong grid size (640x775) are auto-skipped
#     via --expected_height / --expected_width

set -euo pipefail

DATA_PATH="/disks/fast/uk"
SAVE_PATH="/disks/fast/uk-metoffice-c-band-rain-radar.zarr"

uv run python zarr_converter_v3.py \
    --data_path="$DATA_PATH" \
    --save_path="$SAVE_PATH" \
    --var_name=RR \
    --standard_name=rainfall_flux \
    --long_name="Radar precipitation rate" \
    --units="kg m-2 h-1" \
    --timestamp_regex='(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})' \
    --start_date=2005-07-05 \
    --pattern='**/*.tiff' \
    --scale_factor=0.03125 \
    --expected_height=2175 \
    --expected_width=1725 \
    --fillvalue=-1 \
    --cf_epoch=2005-01-01 \
    --base_frequencies='5min:2005-07-05T00:00/None' \
    --consistent_timestep_start=2005-07-05T00:00 \
    --shard_size=288 \
    --num_workers=16 \
    --compression_level=9 \
    --title="UK Met Office C-band rain radar 1 km composite" \
    --license=OGL-UK-3.0 \
    --mlcast_created_by="Gabriele Franch <franch@fbk.eu>" \
    --mlcast_created_with="https://github.com/mlcast-community/mlcast-dataset-UK-METOFFICE-RADAR@v0.1.0" \
    --mlcast_dataset_version=0.1.0 \
    --mlcast_dataset_identifier=UK-METOFFICE-RADAR
