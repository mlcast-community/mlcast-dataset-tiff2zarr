#!/usr/bin/env python3
"""
Generic converter: directory of GeoTIFFs → mlcast-compliant Zarr v3 sharded store.

Produces a zarr that passes:
    mlcast.validate_dataset source_data radar_precipitation <zarr_path>

No external reference files needed — CRS, coordinates, and lat/lon grids are
extracted directly from the GeoTIFFs themselves.
"""

import os
import glob
import re
import time
import multiprocessing as mp
from datetime import datetime, timezone
from functools import partial

import numpy as np
import rasterio
import zarr
from pyproj import CRS as PyprojCRS, Transformer
from zarr.codecs import ZstdCodec
from loguru import logger
from tqdm import tqdm
from fire import Fire


# ---------------------------------------------------------------------------
# CF time encoding
# ---------------------------------------------------------------------------
CF_TIME_CALENDAR = "proleptic_gregorian"

DEFAULT_TIMESTAMP_REGEX = (
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"-(?P<hour>\d{2})-(?P<minute>\d{2})"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _inject_bbox_into_wkt(wkt: str, bbox: tuple) -> str:
    """Inject BBOX clause into a WKT2 PROJCRS or GEOGCRS string.

    bbox = (south_lat, west_lon, north_lat, east_lon)
    """
    south_lat, west_lon, north_lat, east_lon = bbox
    bbox_clause = f"BBOX[{south_lat},{west_lon},{north_lat},{east_lon}]"
    stripped = wkt.rstrip()
    if stripped.endswith("]"):
        return stripped[:-1] + "," + bbox_clause + "]"
    return wkt


def extract_georeference(tif_path: str) -> dict:
    """Extract complete georeference info from a single GeoTIFF.

    Returns a dict with: crs, nx, ny, x, y, lat, lon, crs_wkt, spatial_ref,
    proj4, geo_transform, cf_params, bbox.
    """
    with rasterio.open(tif_path) as src:
        crs = PyprojCRS(src.crs)
        transform = src.transform
        ny, nx = src.height, src.width

    # Pixel-center coordinates
    x = np.array([transform.c + (col + 0.5) * transform.a for col in range(nx)])
    y = np.array([transform.f + (row + 0.5) * transform.e for row in range(ny)])

    # GeoTransform string (GDAL convention)
    geo_transform = (
        f"{transform.c} {transform.a} {transform.b} "
        f"{transform.f} {transform.d} {transform.e}"
    )

    # Reproject corners to WGS84 for BBOX
    transformer = Transformer.from_crs(crs, PyprojCRS.from_epsg(4326), always_xy=True)
    corners_x = [x[0], x[-1], x[0], x[-1]]
    corners_y = [y[0], y[-1], y[-1], y[0]]
    lon_corners, lat_corners = transformer.transform(corners_x, corners_y)
    bbox = (
        min(lat_corners),
        min(lon_corners),
        max(lat_corners),
        max(lon_corners),
    )

    # WKT with BBOX (for crs_wkt) and without (for spatial_ref)
    crs_wkt_raw = crs.to_wkt()
    crs_wkt = _inject_bbox_into_wkt(crs_wkt_raw, bbox)
    spatial_ref = crs_wkt_raw

    # Compute 2D lat/lon grids
    xx, yy = np.meshgrid(x, y)
    lon_grid, lat_grid = transformer.transform(xx, yy)

    # CF grid mapping params from pyproj
    cf_params = crs.to_cf()

    return {
        "crs": crs,
        "nx": nx,
        "ny": ny,
        "x": x,
        "y": y,
        "lat": lat_grid.astype(np.float64),
        "lon": lon_grid.astype(np.float64),
        "crs_wkt": crs_wkt,
        "spatial_ref": spatial_ref,
        "proj4": crs.to_proj4(),
        "geo_transform": geo_transform,
        "cf_params": cf_params,
        "bbox": bbox,
    }


def parse_timestamp_regex(filepath: str, regex: str) -> np.datetime64:
    """Parse timestamp from filename using regex with named groups."""
    basename = os.path.basename(filepath)
    m = re.search(regex, basename)
    if not m:
        raise ValueError(f"Regex '{regex}' did not match filename '{basename}'")
    g = m.groupdict()
    year = g.get("year") or g.get("Y")
    month = g.get("month") or g.get("m")
    day = g.get("day") or g.get("d")
    hour = g.get("hour") or g.get("H", "00")
    minute = g.get("minute") or g.get("M", "00")
    return np.datetime64(f"{year}-{month}-{day}T{hour}:{minute}", "ns")


def cf_encode_times(dt_array: np.ndarray, epoch: str) -> tuple:
    """Encode datetime64[ns] array to int64 minutes since epoch."""
    cf_epoch = np.datetime64(epoch, "ns")
    cf_step = np.timedelta64(1, "m")
    encoded = ((dt_array - cf_epoch) / cf_step).astype(np.int64)
    units = f"minutes since {epoch}"
    return encoded, units


def read_tif(filepath: str, fillvalue: float = -1.0, scale_factor: float = 1.0) -> np.ndarray:
    """Read a single-band GeoTIFF, replacing fillvalue with NaN and applying scale."""
    with rasterio.open(filepath) as src:
        arr = src.read(1).astype(np.float32)
    arr = np.where(arr == fillvalue, np.nan, arr)
    if scale_factor != 1.0:
        arr *= scale_factor
    return arr


def parse_base_frequencies(freq_str: str | None) -> list[tuple] | None:
    """Parse base_frequencies CLI string.

    Format: "15min:2010-01-01T00:00/2014-06-25T09:00;10min:..."
    Returns list of (freq_minutes, start_str, end_str_or_None).
    """
    if not freq_str:
        return None
    bands = []
    for part in freq_str.split(";"):
        freq_part, range_part = part.strip().split(":", 1)
        freq_min = int(freq_part.replace("min", ""))
        start_str, end_str = range_part.split("/")
        end_val = None if end_str.strip().lower() == "none" else end_str.strip()
        bands.append((freq_min, start_str.strip(), end_val))
    return bands


def _write_shard(shard_job):
    """Write all timesteps belonging to a single shard in one bulk operation.

    Assembles the full shard block (shard_size, ny, nx) in memory from the
    source TIFFs, then writes it in a single zarr slice assignment.  This
    avoids the O(shard_size) read-modify-write amplification that occurs
    when writing one timestep at a time into a sharded array.
    """
    zarr_path, var_name, paths, time_indices, fillvalue, scale_factor, shard_size = shard_job
    try:
        root = zarr.open_group(zarr_path, mode="r+")
        arr = root[var_name]
        ny, nx = arr.shape[1], arr.shape[2]

        # Determine the contiguous shard range
        shard_id = time_indices[0] // shard_size
        shard_start = shard_id * shard_size
        shard_end = min(shard_start + shard_size, arr.shape[0])
        shard_len = shard_end - shard_start

        # Pre-fill with NaN, then place each TIF at its offset within the shard
        block = np.full((shard_len, ny, nx), np.nan, dtype=np.float32)
        for path, time_idx in zip(paths, time_indices):
            data = read_tif(path, fillvalue=fillvalue, scale_factor=scale_factor)
            if data.shape != (ny, nx):
                print(f"Skipping {path}: shape {data.shape} != expected ({ny},{nx})")
                continue
            block[time_idx - shard_start, ...] = data

        arr[shard_start:shard_end, ...] = block
    except Exception as e:
        print(f"Error writing shard (indices {time_indices[0]}-{time_indices[-1]}): {e}")


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------
class ZarrConverterV3:
    """Create and fill a mlcast-compliant zarr v3 sharded store from GeoTIFFs."""

    def __init__(
        self,
        data_path: str,
        save_path: str,
        var_name: str = "RR",
        standard_name: str = "rainfall_flux",
        long_name: str = "Total precipitation rate",
        units: str = "kg m-2 h-1",
        timestamp_regex: str = DEFAULT_TIMESTAMP_REGEX,
        start_date: str | None = None,
        end_date: str | None = None,
        shard_size: int = 256,
        compression_level: int = 9,
        num_workers: int = 32,
        batch_size: int = 1,
        timerange_split: int = 1000,
        base_frequencies: str | None = None,
        consistent_timestep_start: str | None = None,
        cf_epoch: str = "2010-01-01",
        fillvalue: float = -1.0,
        title: str = "",
        license: str = "CC-BY-SA-4.0",
        author: str = "",
        mlcast_created_by: str = "",
        mlcast_created_with: str = "",
        mlcast_dataset_version: str = "",
        mlcast_dataset_identifier: str = "",
        pattern: str = "**/*.tif",
        scale_factor: float = 1.0,
        expected_shape: tuple[int, int] | None = None,
    ):
        self.data_path = os.path.abspath(data_path)
        self.save_path = os.path.abspath(save_path)
        self.var_name = var_name
        self.standard_name = standard_name
        self.long_name = long_name
        self.units = units
        self.timestamp_regex = timestamp_regex
        self.start_date = (
            np.datetime64(start_date, "ns") if start_date else None
        )
        self.end_date = np.datetime64(end_date, "ns") if end_date else None
        self.shard_size = shard_size
        self.compression_level = compression_level
        self.num_workers = num_workers
        self.base_frequencies = parse_base_frequencies(base_frequencies)
        self.base_frequencies_str = base_frequencies
        self.consistent_timestep_start = consistent_timestep_start
        self.cf_epoch = cf_epoch
        self.fillvalue = fillvalue
        self.title = title
        self.license_str = license
        self.author = author
        self.mlcast_created_by = mlcast_created_by
        self.mlcast_created_with = mlcast_created_with
        self.mlcast_dataset_version = mlcast_dataset_version
        self.mlcast_dataset_identifier = mlcast_dataset_identifier
        self.pattern = pattern
        self.scale_factor = scale_factor
        self.expected_shape = expected_shape

        # Run pipeline
        self.discover_tifs()
        self._extract_georeference()
        self.build_time_array()
        self.init_zarr()
        self.fill_data()

    # ------------------------------------------------------------------
    def discover_tifs(self):
        """Find and sort all TIF files, parse timestamps, optionally filter by shape."""
        search = os.path.join(self.data_path, self.pattern)
        self.tif_paths = sorted(glob.glob(search, recursive=True))
        logger.info(f"Found {len(self.tif_paths)} TIF files")

        if not self.tif_paths:
            raise FileNotFoundError(f"No TIF files at {search}")

        # Filter out files with wrong grid dimensions
        if self.expected_shape is not None:
            exp_h, exp_w = self.expected_shape
            filtered_paths = []
            # Check one file per parent directory (all files in a day share shape)
            dir_ok = {}
            for p in self.tif_paths:
                d = os.path.dirname(p)
                if d not in dir_ok:
                    with rasterio.open(p) as src:
                        dir_ok[d] = (src.height == exp_h and src.width == exp_w)
                    if not dir_ok[d]:
                        logger.warning(f"Skipping dir with wrong shape: {d}")
                if dir_ok[d]:
                    filtered_paths.append(p)
            n_skipped = len(self.tif_paths) - len(filtered_paths)
            if n_skipped:
                logger.info(f"Filtered out {n_skipped} files with wrong shape")
            self.tif_paths = filtered_paths

        self.tif_timestamps = np.array(
            [parse_timestamp_regex(p, self.timestamp_regex) for p in self.tif_paths],
            dtype="datetime64[ns]",
        )

    # ------------------------------------------------------------------
    def _extract_georeference(self):
        """Extract CRS, transform, x/y/lat/lon from the first TIF."""
        ref_tif = self.tif_paths[0]
        logger.info(f"Extracting georeference from: {ref_tif}")
        self.geo = extract_georeference(ref_tif)
        logger.info(
            f"Grid: {self.geo['ny']}x{self.geo['nx']}, "
            f"CRS: {self.geo['cf_params'].get('grid_mapping_name', 'unknown')}"
        )

    # ------------------------------------------------------------------
    def build_time_array(self):
        """Build time array (data-only) and compute missing_times."""
        tif_ts_set = set(self.tif_timestamps.tolist())

        # Filter to [start_date, end_date)
        mask = np.ones(len(self.tif_timestamps), dtype=bool)
        if self.start_date is not None:
            mask &= self.tif_timestamps >= self.start_date
        if self.end_date is not None:
            mask &= self.tif_timestamps < self.end_date
        self.full_time = self.tif_timestamps[mask]

        if len(self.full_time) == 0:
            raise ValueError("No TIF timestamps in the requested date range")

        logger.info(f"Time array: {len(self.full_time)} timesteps (data-only)")
        logger.info(f"Time range: {self.full_time[0]} to {self.full_time[-1]}")

        # Compute missing_times across frequency bands
        all_missing = []
        if self.base_frequencies:
            for freq_min, band_start_str, band_end_str in self.base_frequencies:
                band_start = np.datetime64(band_start_str, "ns")
                band_end = (
                    np.datetime64(band_end_str, "ns")
                    if band_end_str
                    else self.end_date
                )
                if band_end is None:
                    band_end = self.full_time[-1] + np.timedelta64(freq_min, "m")

                # Clip to requested range
                eff_start = band_start
                eff_end = band_end
                if self.start_date is not None:
                    eff_start = max(eff_start, self.start_date)
                if self.end_date is not None:
                    eff_end = min(eff_end, self.end_date)
                if eff_start >= eff_end:
                    continue

                freq = np.timedelta64(freq_min, "m")
                expected = np.arange(eff_start, eff_end, freq).astype(
                    "datetime64[ns]"
                )
                expected_set = set(expected.tolist())
                missing_in_band = expected_set - tif_ts_set
                all_missing.extend(missing_in_band)

                n_present = len(expected_set) - len(missing_in_band)
                logger.info(
                    f"  {freq_min}min band [{eff_start} -> {eff_end}]: "
                    f"{len(expected)} expected, {n_present} present, "
                    f"{len(missing_in_band)} missing"
                )

        self.missing_times = np.array(sorted(all_missing), dtype="datetime64[ns]")
        logger.info(f"Total missing timestamps: {len(self.missing_times)}")

        # Build mapping: TIF path -> index in self.full_time
        time_to_idx = {ts: i for i, ts in enumerate(self.full_time.tolist())}
        self.tif_write_paths = []
        self.tif_write_indices = []
        for path, ts in zip(self.tif_paths, self.tif_timestamps):
            ts_val = ts.item()
            if ts_val in time_to_idx:
                self.tif_write_paths.append(path)
                self.tif_write_indices.append(time_to_idx[ts_val])

        logger.info(f"TIFs to write: {len(self.tif_write_paths)}")

    # ------------------------------------------------------------------
    def init_zarr(self):
        """Create the zarr v3 group with all arrays, coordinates, and metadata."""
        logger.info("Initialising zarr v3 store")

        ny = self.geo["ny"]
        nx = self.geo["nx"]
        n_time = len(self.full_time)

        store = zarr.storage.LocalStore(self.save_path)
        self.root = zarr.open_group(store, mode="w", zarr_format=3)

        zstd = ZstdCodec(level=self.compression_level)

        # --- x coordinate ---
        self.root.create_array(
            "x",
            data=self.geo["x"].astype(np.float64),
            chunks=(nx,),
            dimension_names=["x"],
            compressors=zstd,
            attributes={"units": "m"},
        )

        # --- y coordinate ---
        self.root.create_array(
            "y",
            data=self.geo["y"].astype(np.float64),
            chunks=(ny,),
            dimension_names=["y"],
            compressors=zstd,
            attributes={"units": "m"},
        )

        # --- lat (2D) ---
        self.root.create_array(
            "lat",
            data=self.geo["lat"].astype(np.float64),
            chunks=(ny, nx),
            dimension_names=["y", "x"],
            compressors=zstd,
            attributes={
                "grid_mapping": "crs",
                "long_name": "Latitude",
                "standard_name": "latitude",
                "units": "degrees_north",
            },
        )

        # --- lon (2D) ---
        self.root.create_array(
            "lon",
            data=self.geo["lon"].astype(np.float64),
            chunks=(ny, nx),
            dimension_names=["y", "x"],
            compressors=zstd,
            attributes={
                "grid_mapping": "crs",
                "long_name": "Longitude",
                "standard_name": "longitude",
                "units": "degrees_east",
            },
        )

        # --- time (CF-encoded int64, fill_value=None to avoid epoch collision) ---
        time_encoded, cf_time_units = cf_encode_times(self.full_time, self.cf_epoch)
        self.root.create_array(
            "time",
            data=time_encoded.astype(np.int64),
            chunks=(n_time,),
            fill_value=None,
            dimension_names=["time"],
            compressors=zstd,
            attributes={
                "long_name": "Time",
                "standard_name": "time",
                "units": cf_time_units,
                "calendar": CF_TIME_CALENDAR,
            },
        )

        # --- missing_times (CF-encoded int64) ---
        mt_encoded, _ = cf_encode_times(self.missing_times, self.cf_epoch)
        mt_data = mt_encoded.astype(np.int64) if len(mt_encoded) > 0 else np.array([], dtype="int64")
        mt_len = max(len(mt_data), 1)
        self.root.create_array(
            "missing_times",
            data=mt_data,
            chunks=(mt_len,),
            fill_value=None,
            dimension_names=["missing_times"],
            compressors=zstd,
            attributes={
                "units": cf_time_units,
                "calendar": CF_TIME_CALENDAR,
            },
        )

        # --- Data variable (SHARDED) ---
        self.root.create_array(
            self.var_name,
            shape=(n_time, ny, nx),
            chunks=(1, ny, nx),
            shards=(self.shard_size, ny, nx),
            dtype="float32",
            fill_value=float("nan"),
            dimension_names=["time", "y", "x"],
            compressors=zstd,
            attributes={
                "grid_mapping": "crs",
                "long_name": self.long_name,
                "standard_name": self.standard_name,
                "units": self.units,
                "coordinates": "lat lon",
            },
        )

        # --- CRS variable (scalar) ---
        crs_attrs = self._build_crs_attrs()
        self.root.create_array(
            "crs",
            data=np.array(np.nan, dtype=np.float32),
            compressors=zstd,
            attributes=crs_attrs,
        )

        # --- Global attributes ---
        curr_date = (
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        )
        global_attrs = {
            "title": self.title,
            "license": self.license_str,
            "history": f"Created at {curr_date}",
            "coordinates": "lat lon",
            "mlcast_created_on": curr_date,
            "mlcast_created_by": self.mlcast_created_by,
            "mlcast_created_with": self.mlcast_created_with,
            "mlcast_dataset_version": self.mlcast_dataset_version,
            "mlcast_dataset_identifier": self.mlcast_dataset_identifier,
        }
        if self.author:
            global_attrs["Author"] = self.author
        if self.consistent_timestep_start:
            global_attrs["consistent_timestep_start"] = self.consistent_timestep_start
        if self.base_frequencies_str:
            global_attrs["base_frequencies"] = self.base_frequencies_str

        self.root.attrs.update(global_attrs)
        logger.info(
            f"Zarr v3 initialised: {n_time} timesteps, {ny}x{nx} grid, "
            f"shard_size={self.shard_size}"
        )

    # ------------------------------------------------------------------
    def _build_crs_attrs(self) -> dict:
        """Build CRS variable attributes from pyproj."""
        attrs = {
            "crs_wkt": self.geo["crs_wkt"],
            "spatial_ref": self.geo["spatial_ref"],
            "GeoTransform": self.geo["geo_transform"],
            "proj4": self.geo["proj4"],
        }
        # Add CF grid mapping parameters
        for key, val in self.geo["cf_params"].items():
            if key not in attrs:
                attrs[key] = val
        return attrs

    # ------------------------------------------------------------------
    def fill_data(self):
        """Write TIF data into the zarr store using multiprocessing.

        Writes are grouped by shard: each worker processes all timesteps
        belonging to a single shard, so no two workers ever touch the same
        shard file on disk. This avoids read-modify-write race conditions
        inherent in the zarr v3 sharding codec.
        """
        n = len(self.tif_write_paths)
        if n == 0:
            logger.warning("No TIF data to write")
            return

        # Group writes by shard index
        from collections import defaultdict
        shard_groups = defaultdict(list)
        for path, time_idx in zip(self.tif_write_paths, self.tif_write_indices):
            shard_id = time_idx // self.shard_size
            shard_groups[shard_id].append((path, time_idx))

        # Build one job per shard
        shard_jobs = []
        for shard_id in sorted(shard_groups.keys()):
            items = shard_groups[shard_id]
            paths = [p for p, _ in items]
            indices = [i for _, i in items]
            shard_jobs.append(
                (self.save_path, self.var_name, paths, indices, self.fillvalue, self.scale_factor, self.shard_size)
            )

        logger.info(
            f"Writing {n} TIFs across {len(shard_jobs)} shards "
            f"(shard_size={self.shard_size})"
        )

        with mp.Pool(self.num_workers) as pool:
            list(
                tqdm(
                    pool.imap_unordered(_write_shard, shard_jobs),
                    total=len(shard_jobs),
                    desc="Writing shards",
                    unit="shard",
                )
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(
    data_path: str,
    save_path: str,
    var_name: str = "RR",
    standard_name: str = "rainfall_flux",
    long_name: str = "Total precipitation rate",
    units: str = "kg m-2 h-1",
    timestamp_regex: str = DEFAULT_TIMESTAMP_REGEX,
    start_date: str | None = None,
    end_date: str | None = None,
    shard_size: int = 256,
    compression_level: int = 9,
    num_workers: int = 32,
    batch_size: int = 1,
    timerange_split: int = 1000,
    base_frequencies: str | None = None,
    consistent_timestep_start: str | None = None,
    cf_epoch: str = "2010-01-01",
    fillvalue: float = -1.0,
    title: str = "",
    license: str = "CC-BY-SA-4.0",
    author: str = "",
    mlcast_created_by: str = "",
    mlcast_created_with: str = "",
    mlcast_dataset_version: str = "",
    mlcast_dataset_identifier: str = "",
    pattern: str = "**/*.tif",
    scale_factor: float = 1.0,
    expected_height: int | None = None,
    expected_width: int | None = None,
):
    """
    Convert a directory of GeoTIFFs into a mlcast-compliant Zarr v3 sharded store.

    All georeference information (CRS, coordinates, lat/lon) is extracted
    directly from the GeoTIFFs — no external reference files needed.

    Args:
        data_path: Directory containing TIF files
        save_path: Output zarr v3 path
        var_name: Data variable name
        standard_name: CF standard name
        long_name: Descriptive variable name
        units: Physical units
        timestamp_regex: Regex with named groups (year, month, day, hour, minute)
        start_date: Start of time range (ISO 8601), None = no filter
        end_date: End of time range (ISO 8601), None = no filter
        shard_size: Timesteps per shard (default 256)
        compression_level: Zstd compression level (0-22)
        num_workers: Parallel write workers
        batch_size: TIFs per write batch
        timerange_split: Split writes into chunks of this size
        base_frequencies: Frequency bands for missing-time detection
        consistent_timestep_start: Start of regular timestep period (ISO 8601)
        cf_epoch: CF time encoding epoch
        fillvalue: TIF nodata value to replace with NaN
        title: Dataset title
        license: SPDX license identifier
        author: Dataset author
        mlcast_created_by: Creator in "Name <email>" format
        mlcast_created_with: GitHub URL with version tag
        mlcast_dataset_version: Dataset version (semver/calver)
        mlcast_dataset_identifier: Dataset identifier (e.g., "IT-DPC-SRI")
        pattern: Glob pattern for finding TIF files
        scale_factor: Multiply pixel values by this after reading (default 1.0)
        expected_height: Expected raster height; dirs with different height are skipped
        expected_width: Expected raster width; dirs with different width are skipped
    """
    expected_shape = None
    if expected_height is not None and expected_width is not None:
        expected_shape = (expected_height, expected_width)

    t0 = time.time()
    ZarrConverterV3(
        data_path=data_path,
        save_path=save_path,
        var_name=var_name,
        standard_name=standard_name,
        long_name=long_name,
        units=units,
        timestamp_regex=timestamp_regex,
        start_date=start_date,
        end_date=end_date,
        shard_size=shard_size,
        compression_level=compression_level,
        num_workers=num_workers,
        batch_size=batch_size,
        timerange_split=timerange_split,
        base_frequencies=base_frequencies,
        consistent_timestep_start=consistent_timestep_start,
        cf_epoch=cf_epoch,
        fillvalue=fillvalue,
        title=title,
        license=license,
        author=author,
        mlcast_created_by=mlcast_created_by,
        mlcast_created_with=mlcast_created_with,
        mlcast_dataset_version=mlcast_dataset_version,
        mlcast_dataset_identifier=mlcast_dataset_identifier,
        pattern=pattern,
        scale_factor=scale_factor,
        expected_shape=expected_shape,
    )
    elapsed = (time.time() - t0) / 60
    logger.info(f"Total time: {elapsed:.1f} min")


if __name__ == "__main__":
    Fire(main)
