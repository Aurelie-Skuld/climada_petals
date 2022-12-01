import sys
import logging
from pathlib import Path
from copy import deepcopy
from typing import Optional, Union, List, Mapping, Any
from collections import deque
from collections.abc import Iterable
import re

import numpy as np
import xarray as xr
from scipy.stats import gumbel_r
from scipy.interpolate import interp1d
import pandas as pd

import dantro as dtr
from dantro.data_ops import is_operation
from dantro.data_loaders import AllAvailableLoadersMixin
from dantro.containers import XrDataContainer
from dantro.tools import load_yml
from dantro.groups import OrderedDataGroup

from climada.hazard import Hazard
from climada.util.constants import SYSTEM_DIR
from climada.util.coordinates import get_country_geometries, country_to_iso
from climada_petals.hazard.river_flood import RiverFlood
from climada_petals.util import glofas_request

LOGGER = logging.getLogger(__name__)


def save_file(
    data: Union[xr.Dataset, xr.DataArray],
    output_path: Union[Path, str],
    **encoding_kwargs,
):
    """Save xarray data as a file with default compression

    Parameters
    ----------
    data : xr.Dataset or xr.Dataarray
        The data to be stored in the file
    output_path : pathlib.Path or str
        The file path to store the data into. If it does not contain a suffix, ``.nc``
        is automatically appended. The enclosing folder must already exist.
    encoding_kwargs
        Optional keyword arguments for the encoding, which applies to every data
        variable. Default encoding settings are:
        ``dict(dtype="float32", zlib=True, complevel=4)``
    """
    # Store encoding
    encoding = dict(dtype="float32", zlib=True, complevel=4)
    encoding.update(encoding_kwargs)
    encoding = {var: encoding for var in data.data_vars}

    # Repeat encoding for each variable
    output_path = Path(output_path)
    if not output_path.suffix:
        output_path = output_path.with_suffix(".nc")
    data.to_netcdf(output_path, encoding=encoding)


def sel_lon_lat_slice(target: xr.DataArray, source: xr.DataArray) -> xr.DataArray:
    """Select a lon/lat slice from 'target' using coordinates of 'source'"""
    lon = source["longitude"][[0, -1]]
    lat = source["latitude"][[0, -1]]
    return target.sel(longitude=slice(*lon), latitude=slice(*lat))


def reindex(
    target: xr.DataArray,
    source: xr.DataArray,
    tolerance=None,
    fill_value=np.nan,
    assert_no_fill_value=False,
) -> xr.DataArray:
    """Reindex target to source with nearest neighbor lookup

    Parameters
    ----------
    target : xr.DataArray
        Array to be reindexed.
    source : xr.DataArray
        Array whose coordinates are used for reindexing.
    tolerance : float (optional)
        Maximum distance between coordinates. If it is superseded, the ``fill_value`` is
        inserted instead of the nearest neighbor value. Defaults to NaN
    assert_no_fill_value : bool (optional)
        Throw an error if fill values are found in the data after reindexing. This will
        also throw an error if the fill value is present in the ``target`` before
        reindexing (because the check afterwards would else not make sense)

    Returns
    -------
    target : xr.DataArray
        Target reindexed like 'source' with nearest neighbor lookup for the data.

    Raises
    ------
    ValueError
        If tolerance is exceeded when reindexing, in case ``assert_no_fill_value`` is
        ``True``.
    ValueError
        If ``target`` already contains the ``fill_value`` before reindexing, in case
        ``assert_no_fill_value`` is ``True``.
    """

    def has_fill_value(arr):
        return arr.isin(fill_value).any() or (
            np.isnan(fill_value) and arr.isnull().any()
        )

    # Check for fill values before
    if assert_no_fill_value and has_fill_value(target):
        raise ValueError(
            f"Array '{target.name}' does already contain reindex fill value"
        )

    # Reindex operation
    target = target.reindex_like(
        source, method="nearest", tolerance=tolerance, copy=False, fill_value=fill_value
    )

    # Check for fill values after
    if assert_no_fill_value and has_fill_value(target):
        raise ValueError(
            f"Reindexing '{target.name}' to '{source.name}' exceeds tolerance! "
            "Try interpolating the datasets or increasing the tolerance"
        )

    return target


@is_operation
def merge_flood_maps(flood_maps: OrderedDataGroup) -> xr.Dataset:
    """Merge the flood maps GeoTIFFs into one NetCDF file

    Adds a "zero" flood map (all zeros)

    Parameters
    ----------
    flood_maps : dantro.OrderedDataGroup
        The flood maps stored in a data group. Each flood map is expected to be an
        xarray Dataset named ``floodMapGL_rpXXXy``, where ``XXX`` indicates the return
        period of the respective map.

    """
    # print(flood_maps)
    expr = re.compile(r"floodMapGL_rp(\d+)y")
    years = [int(expr.match(name).group(1)) for name in flood_maps]
    idx = np.argsort(years)
    dsets = list(flood_maps.values())
    dsets = [dsets[i].drop_vars("spatial_ref").squeeze("band", drop=True) for i in idx]

    # Add zero flood map
    # NOTE: Return period of 1 is the minimal value
    ds_null_flood = xr.zeros_like(dsets[0])
    dsets.insert(0, ds_null_flood)

    # Concatenate and rename
    years = np.insert(np.array(years)[idx], 0, 1)
    ds_flood_maps = xr.concat(dsets, pd.Index(years, name="return_period"))
    ds_flood_maps = ds_flood_maps.rename(
        band_data="flood_depth", x="longitude", y="latitude"
    )
    return ds_flood_maps


@is_operation
def fit_gumbel_r(
    input_data: xr.DataArray, fit_method: str = "MLE", min_samples: int = 2
):
    """Fit a right-handed Gumbel distribution to the data

    input_data : xr.DataArray
        The input time series to compute the distributions for. It must contain the
        dimension ``year``.
    fit_method : str
        The method used for computing the distribution. Either ``MLE`` (Maximum
        Likelihood Estimation) or ``MM`` (Method of Moments).
    min_samples : int
        The number of finite samples along the ``year`` dimension required for a
        successful fit. If there are fewer samples, the fit result will be NaN.
    """

    def fit(time_series):
        # Count finite samples
        samples = np.isfinite(time_series)
        if np.count_nonzero(samples) < min_samples:
            return np.nan, np.nan

        # Mask array
        return gumbel_r.fit(time_series[samples], method=fit_method)

    # Apply fitting
    loc, scale = xr.apply_ufunc(
        fit,
        input_data,
        input_core_dims=[["year"]],
        output_core_dims=[[], []],
        exclude_dims={"year"},
        vectorize=True,
        dask="parallelized",
        output_dtypes=[np.float64, np.float64],
    )

    return xr.Dataset(dict(loc=loc, scale=scale))


@is_operation
def download_glofas_discharge(
    product: str,
    date_from: str,
    date_to: Optional[str],
    num_proc: int = 1,
    download_path: Union[str, Path] = Path(SYSTEM_DIR, "glofas-discharge"),
    countries: Optional[Union[List[str], str]] = None,
    preprocess: Optional[str] = None,
    open_mfdataset_kw: Optional[Mapping[str, Any]] = None,
    **request_kwargs,
) -> xr.DataArray:
    """Download the GloFAS data and return the resulting dataset

    Several parameters are passed directly to
    :py:func:`climada_petals.util.glofas_request`. See this functions documentation for
    further information.

    Parameters
    ----------
    product : str
        The string identifier of the product to download. See
        :py:func:`climada_petals.util.glofas_request` for supported products.
    date_from : str
        Earliest date to download. Specification depends on the ``product`` chosen.
    date_to : str or None
        Latest date to download. If ``None``, only download the ``date_from``.
        Specification depends on the ``product`` chosen.
    num_proc : int
        Number of parallel processes to use for downloading. Defaults to 1.
    download_path : str or pathlib.Path
        Directory to store the downloaded data. The directory (and all required parent
        directories!) will be created if it does not yet exist. Defaults to
        ``~/climada/data/glofas-discharge/``.
    countries : str or list of str, optional
        Countries to download data for. Uses the maximum extension of all countries for
        selecting the latitude/longitude range of data to download.
    preprocess : str, optional
        String expression for preprocessing the data before merging it into one dataset.
        Must be valid Python code. The downloaded data is passed as variable ``x``.
    open_mfdataset_kw : dict, optional
        Optional keyword arguments for the ``xarray.open_mfdataset`` function.
    request_kwargs:
        Keyword arguments for the Copernicus data store request.
    """
    # Create the download path if it does not yet exist
    LOGGER.debug("Preparing download directory: %s", download_path)
    download_path = Path(download_path)  # Make sure it is a Path
    download_path.mkdir(parents=True, exist_ok=True)

    # Determine area from 'countries'
    if countries is not None:
        LOGGER.debug("Choosing lat/lon bounds from countries %s", countries)
        # Fetch area and reorder appropriately
        # NOTE: 'area': north, west, south, east
        #       'extent': lon min (west), lon max (east), lat min (south), lat max (north)
        area = request_kwargs.get("area")
        if area is not None:
            LOGGER.debug("Subsetting country geometries with 'area'")
            area = [area[1], area[3], area[2], area[0]]

        # Fetch geometries and bounds of requested countries
        iso = country_to_iso(countries)
        geo = get_country_geometries(iso, extent=area)

        # NOTE: 'bounds': minx (west), miny (south), maxx (east), maxy (north)
        # NOTE: Explicitly cast to float to ensure that YAML parser can dump the data
        bounds = deque(map(float, geo.total_bounds.flat))
        bounds.rotate(1)

        # Insert into kwargs
        request_kwargs["area"] = list(bounds)

    # Request the data
    files = glofas_request(
        product=product,
        date_from=date_from,
        date_to=date_to,
        num_proc=num_proc,
        output_dir=download_path,
        request_kw=request_kwargs,
    )

    # Set arguments for 'open_mfdataset'
    open_kwargs = dict(chunks={}, combine="nested", concat_dim="time")
    if open_mfdataset_kw is not None:
        open_kwargs.update(open_mfdataset_kw)

    # Preprocessing
    if preprocess is not None:
        open_kwargs.update(preprocess=lambda x: eval(preprocess))

    # Open the data and return it
    return xr.open_mfdataset(files, **open_kwargs)["dis24"]


@is_operation
def max_from_isel(
    array: xr.DataArray, dim: str, selections: List[Union[Iterable, slice]]
):
    """Compute the maximum over several selections of an array dimension"""
    if not all(
        [isinstance(sel, Iterable) or isinstance(sel, slice) for sel in selections]
    ):
        raise TypeError(
            "This function only works with iterables or slices as selection"
        )

    data = [array.isel({dim: sel}) for sel in selections]
    return xr.concat(
        [da.max(dim=dim, skipna=True) for da in data],
        dim=pd.Index(list(range(len(selections))), name="select")
        # dim=xr.concat([da[dim].max() for da in data], dim=dim)
    )


@is_operation
def return_period(
    discharge: xr.DataArray, gev_loc: xr.DataArray, gev_scale: xr.DataArray
) -> xr.DataArray:
    """Compute the return period for a discharge from a Gumbel EV distribution fit

    Coordinates of the three datasets must match up to a tolerance of 1e-3 degrees. If
    they do not, an error is thrown.
    """
    gev_loc = reindex(
        gev_loc, discharge, tolerance=1e-3, fill_value=-1, assert_no_fill_value=True
    )
    gev_scale = reindex(
        gev_scale, discharge, tolerance=1e-3, fill_value=-1, assert_no_fill_value=True
    )

    # Compute the return period
    def rp(dis, loc, scale):
        return 1.0 / (1.0 - gumbel_r.cdf(dis, loc=loc, scale=scale))

    # Apply and return
    return xr.apply_ufunc(
        rp,
        discharge,
        gev_loc,
        gev_scale,
        dask="parallelized",
        output_dtypes=[np.float32],
    ).rename("Return Period")


@is_operation
def interpolate_space(
    return_period: xr.DataArray,
    flood_maps: xr.DataArray,
    method: str = "linear",
) -> xr.DataArray:
    """Interpolate the return period in space onto the flood maps grid"""
    # Select lon/lat for flood maps
    flood_maps = sel_lon_lat_slice(flood_maps, return_period)

    # Interpolate the return period
    return return_period.interp(
        coords=dict(longitude=flood_maps["longitude"], latitude=flood_maps["latitude"]),
        method=method,
        kwargs=dict(fill_value=None),  # Extrapolate
    )


@is_operation
def flood_depth(return_period: xr.DataArray, flood_maps: xr.DataArray) -> xr.DataArray:
    def interpolate(return_period, hazard, return_periods):
        """Linearly interpolate the hazard to a given return period

        Args:
            return_period (float): The return period to evaluate the hazard at
            hazard (np.array): The hazard at given return periods (dependent var)
            return_periods (np.array): The given return periods (independent var)

        Returns:
            float: The hazard at the requested return period.

            The hazard cannot become negative. Values beyond the given return periods
            range are extrapolated.
        """
        # Shortcut for only NaNs
        if np.all(np.isnan(hazard)):
            return np.full_like(return_period, np.nan)

        # Make NaNs to zeros
        # NOTE: NaNs should be grouped at lower end of 'return_periods', so this should
        #       be sane.
        hazard = np.nan_to_num(hazard)

        # Use extrapolation and have 0.0 as minimum value
        ret = interp1d(
            return_periods,
            hazard,
            fill_value="extrapolate",
            assume_sorted=True,
            copy=False,
        )(return_period)
        ret = np.maximum(ret, 0.0)
        return ret

    # Select lon/lat for flood maps
    flood_maps = sel_lon_lat_slice(flood_maps, return_period)

    # All but 'longitude' and 'latitude' are core dimensions for this operation
    dims = set(return_period.dims)
    core_dims = dims - {"longitude", "latitude"}

    # Perform operation
    return xr.apply_ufunc(
        interpolate,
        return_period,
        flood_maps,
        flood_maps["return_period"],
        input_core_dims=[list(core_dims), ["return_period"], ["return_period"]],
        output_core_dims=[list(core_dims)],
        exclude_dims={"return_period"},  # Add 'step' and 'number' here?
        dask="parallelized",
        vectorize=True,
        output_dtypes=[np.float32],
    ).rename("Flood Depth")


class ClimadaDataManager(AllAvailableLoadersMixin, dtr.DataManager):
    """A DataManager that can load many different file formats"""

    _HDF5_DSET_DEFAULT_CLS = XrDataContainer
    """Tells the HDF5 loader which container class to use"""

    _NEW_CONTAINER_CLS = XrDataContainer
    """Which container class to use when adding new containers"""


def finalize(*args, data, **kwargs):
    """Store tagged nodes in files or in the DataManager depending on the user input"""
    # Write data to files
    output_dir = Path(kwargs["out_path"]).parent
    for entry in kwargs.get("to_file", {}):
        if isinstance(entry, dict):
            tag = entry["tag"]
            filename = entry.get("filename", tag)
            encoding = entry.get("encoding", {})
        else:
            tag = entry
            filename = entry
            encoding = {}
        save_file(data[tag], output_dir / filename, **encoding)

    # Store data in DataManager
    for entry in kwargs.get("to_dm", {}):
        if isinstance(entry, dict):
            tag = entry["tag"]
            name = entry.get("name", tag)
        else:
            tag = entry
            name = entry
        data["data_manager"].new_container(name, data=data[tag])


DEFAULT_DATA_DIR = SYSTEM_DIR / "glofas-computation"


def dantro_transform(yaml_cfg_path):
    # Load the config
    cfg = load_yml(yaml_cfg_path)

    # Create data directory
    data_dir = Path(cfg.get("data_dir", DEFAULT_DATA_DIR)).expanduser().absolute()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Set up DataManager
    dm = ClimadaDataManager(data_dir, **cfg.get("data_manager", {}))
    dm.load_from_cfg(load_cfg=cfg["data_manager"]["load_cfg"], print_tree=True)

    # Set up the PlotManager ...
    pm = dtr.PlotManager(dm=dm, **cfg.get("plot_manager"))

    # ... and use it to invoke some evaluation routine
    pm.plot_from_cfg(plots_cfg=cfg.get("eval"))

    # Return the DataManager
    print(dm.tree)
    return dm


def prepare(
    cfg=Path(
        "~/coding/climada_petals/climada_petals/hazard/rf_glofas_util.yml"
    ).expanduser(),
):
    dantro_transform(cfg)


def compute_hazard_series(
    cfg=Path(
        "~/coding/climada_petals/climada_petals/hazard/rf_glofas.yml"
    ).expanduser(),
    hazard_concat_dim="number",
):
    dm = dantro_transform(cfg)
    ds_hazard = dm["flood_depth"].data.to_dataset()

    def create_hazard(ds: xr.Dataset) -> Hazard:
        """Create hazard from a GloFASRiverFlood hazard dataset"""
        return RiverFlood.from_raster_xarray(
            ds,
            hazard_type="RF",
            intensity="Flood Depth",
            intensity_unit="m",
            coordinate_vars=dict(event=hazard_concat_dim),
            data_vars=dict(date="time"),
        )

    # Iterate over all dimensions that are not lon, lat, or number
    # NOTE: Why would we have others than "time"? Multiple instances of 'max' over
    #       'step'? How would this look like in the DAG? Check this first!
    iter_dims = list(set(ds_hazard.dims) - {"longitude", "latitude", hazard_concat_dim})
    if iter_dims:
        index = pd.MultiIndex.from_product(
            [ds_hazard[dim].values for dim in iter_dims], names=iter_dims
        )
        hazards = [
            create_hazard(ds_hazard.sel(dict(zip(iter_dims, idx))))
            for idx in index.to_flat_index()
        ]
    else:
        index = None
        hazards = [create_hazard(ds_hazard)]

    return pd.Series(hazards, index=index)
