#!/usr/bin/env bash
# Convert Italian DPC SRI radar GeoTIFFs to mlcast-compliant Zarr v3.
#
# Source data: ~1M TIFFs in /disks/fast/italian-radar-dpc-tiff/
#   - 1400x1200 grid, Transverse Mercator (42N, 12.5E), 1 km resolution
#   - float32, nodata = -1, units already in mm/h (no scaling needed)
#   - Filenames: SRI-YYYY-MM-DD-HH-MM.tif
#   - Two frequency bands: 15 min (2010–2014) then 10 min (2014–present)

set -euo pipefail

DATA_PATH="/disks/fast/italian-radar-dpc-tiff"
SAVE_PATH="/disks/fast/italian-radar-dpc-sri.zarr"

uv run python zarr_converter_v3.py \
    --data_path="$DATA_PATH" \
    --save_path="$SAVE_PATH" \
    --var_name=RR \
    --standard_name=rainfall_flux \
    --long_name="Total precipitation rate" \
    --units="kg m-2 h-1" \
    --timestamp_regex='(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})-(?P<hour>\d{2})-(?P<minute>\d{2})' \
    --start_date=2010-01-01 \
    --pattern='**/*.tif' \
    --fillvalue=-1 \
    --cf_epoch=2010-01-01 \
    --base_frequencies='15min:2010-01-01T00:00/2014-06-25T09:00;10min:2014-06-25T09:00/None' \
    --consistent_timestep_start=2014-06-25T09:00 \
    --shard_size=256 \
    --num_workers=32 \
    --compression_level=9 \
    --title="IT-DPC-SRI: Italian Radar Precipitation (2010--2025)" \
    --license=CC-BY-SA-4.0 \
    --mlcast_created_by="Gabriele Franch <franch@fbk.eu>" \
    --mlcast_created_with="https://github.com/mlcast-community/mlcast-dataset-IT-DPC-SRI@v1.0.0" \
    --mlcast_dataset_version=1.0.0 \
    --mlcast_dataset_identifier=IT-DPC-SRI
