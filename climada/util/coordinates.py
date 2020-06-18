"""
This file is part of CLIMADA.

Copyright (C) 2017 ETH Zurich, CLIMADA contributors listed in AUTHORS.

CLIMADA is free software: you can redistribute it and/or modify it under the
terms of the GNU Lesser General Public License as published by the Free
Software Foundation, version 3.

CLIMADA is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License along
with CLIMADA. If not, see <https://www.gnu.org/licenses/>.

---

Define functions to handle with coordinates
"""
import os
import copy
import logging
from multiprocessing import cpu_count
import math
import numpy as np
from cartopy.io import shapereader
import shapely.vectorized
import shapely.ops
from shapely.geometry import Polygon, MultiPolygon, Point, box
from fiona.crs import from_epsg
from iso3166 import countries as iso_cntry
import geopandas as gpd
import rasterio
import shapefile
from rasterio import MemoryFile
from rasterio.transform import from_origin
from rasterio.crs import CRS
from rasterio.mask import mask
from rasterio.warp import reproject, Resampling, calculate_default_transform
from rasterio.features import rasterize
import dask.dataframe as dd
import pandas as pd
import xarray as xr
import scipy.interpolate

from climada.util.constants import DEF_CRS, SYSTEM_DIR, NAT_REG_ID, GLB_CENTROIDS_NC

pd.options.mode.chained_assignment = None

LOGGER = logging.getLogger(__name__)

NE_EPSG = 4326
""" Natural Earth CRS EPSG """

NE_CRS = from_epsg(NE_EPSG)
""" Natural Earth CRS """

TMP_ELEVATION_FILE = os.path.join(SYSTEM_DIR, 'tmp_elevation.tif')
""" Path of elevation file written in set_elevation """

DEM_NODATA = -9999
""" Value to use for no data values in DEM, i.e see points """

MAX_DEM_TILES_DOWN = 300
""" Maximum DEM tiles to dowload """

def grid_is_regular(coord):
    """Return True if grid is regular. If True, returns height and width.

    Parameters:
        coord (np.array):

    Returns:
        bool (is regular), int (height), int (width)
    """
    regular = False
    _, count_lat = np.unique(coord[:, 0], return_counts=True)
    _, count_lon = np.unique(coord[:, 1], return_counts=True)
    uni_lat_size = np.unique(count_lat).size
    uni_lon_size = np.unique(count_lon).size
    if uni_lat_size == uni_lon_size and uni_lat_size == 1 \
    and count_lat[0] > 1 and count_lon[0] > 1:
        regular = True
    return regular, count_lat[0], count_lon[0]

def get_coastlines(bounds=None, resolution=110):
    """ Get Polygones of coast intersecting given bounds

    Parameter:
        bounds (tuple): min_lon, min_lat, max_lon, max_lat in EPSG:4326
        resolution (float, optional): 10, 50 or 110. Resolution in m. Default:
            110m, i.e. 1:110.000.000

    Returns:
        GeoDataFrame
    """
    resolution = nat_earth_resolution(resolution)
    shp_file = shapereader.natural_earth(resolution=resolution,
                                         category='physical',
                                         name='coastline')
    coast_df = gpd.read_file(shp_file)
    coast_df.crs = NE_CRS
    if bounds is None:
        return coast_df[['geometry']]
    tot_coast = np.zeros(1)
    while not np.any(tot_coast):
        tot_coast = coast_df.envelope.intersects(box(*bounds))
        bounds = (bounds[0] - 20, bounds[1] - 20,
                  bounds[2] + 20, bounds[3] + 20)
    return coast_df[tot_coast][['geometry']]

def convert_wgs_to_utm(lon, lat):
    """ Get EPSG code of UTM projection for input point in EPSG 4326

    Parameter:
        lon (float): longitude point in EPSG 4326
        lat (float): latitude of point (lat, lon) in EPSG 4326

    Return:
        int
    """
    epsg_utm_base = 32601 + (0 if lat >= 0 else 100)
    return epsg_utm_base + (math.floor((lon + 180) / 6) % 60)

def utm_zones(wgs_bounds):
    """ Get EPSG code and bounds of UTM zones covering specified region

    Parameter:
        wgs_bounds (tuple): lon_min, lat_min, lon_max, lat_max

    Returns:
        list of pairs (zone_epsg, zone_wgs_bounds)
    """
    lon_min, lat_min, lon_max, lat_max = wgs_bounds
    lon_min, lon_max = max(-179.99, lon_min), min(179.99, lon_max)
    utm_min, utm_max = [math.floor((l + 180) / 6) for l in [lon_min, lon_max]]
    zones = []
    for utm in range(utm_min, utm_max + 1):
        epsg = 32601 + utm
        bounds = (-180 + 6 * utm, 0, -180 + 6 * (utm + 1), 90)
        if lat_max >= 0:
            zones.append((epsg, bounds))
        if lat_min < 0:
            bounds = (bounds[0], -90, bounds[2], 0)
            zones.append((epsg + 100, bounds))
    return zones

def dist_to_coast(coord_lat, lon=None):
    """ Comput distance to coast from input points in meters.

    Parameters:
        coord_lat (GeoDataFrame or np.array or float):
            - GeoDataFrame with geometry column in epsg:4326
            - np.array with two columns, first for latitude of each point and
                second with longitude in epsg:4326
            - np.array with one dimension containing latitudes in epsg:4326
            - float with a latitude value in epsg:4326
        lon (np.array or float, optional):
            - np.array with one dimension containing longitudes in epsg:4326
            - float with a longitude value in epsg:4326

    Returns:
        np.array
    """
    if isinstance(coord_lat, (gpd.GeoDataFrame, gpd.GeoSeries)):
        if not equal_crs(coord_lat.crs, NE_CRS):
            LOGGER.error('Input CRS is not %s', str(NE_CRS))
            raise ValueError
        geom = coord_lat
    else:
        if lon is None:
            if isinstance(coord_lat, np.ndarray) and coord_lat.shape[1] == 2:
                lat, lon = coord_lat[:, 0], coord_lat[:, 1]
            else:
                LOGGER.error('Missing longitude values.')
                raise ValueError
        else:
            lat, lon = [np.asarray(v).reshape(-1) for v in [coord_lat, lon]]
            if lat.size != lon.size:
                LOGGER.error('Mismatching input coordinates size: %s != %s',
                             lat.size, lon.size)
                raise ValueError
        geom = gpd.GeoDataFrame(geometry=list(map(Point, lon, lat)), crs=NE_CRS)

    pad = 20
    bounds = (geom.total_bounds[0] - pad, geom.total_bounds[1] - pad,
              geom.total_bounds[2] + pad, geom.total_bounds[3] + pad)
    coast = get_coastlines(bounds, 10).geometry
    coast = gpd.GeoDataFrame(geometry=coast, crs=NE_CRS)
    dist = np.empty(geom.shape[0])
    zones = utm_zones(geom.geometry.total_bounds)
    for izone, (epsg, bounds) in enumerate(zones):
        to_crs = from_epsg(epsg)
        zone_mask = (bounds[1] <= geom.geometry.y) \
                  & (geom.geometry.y <= bounds[3]) \
                  & (bounds[0] <= geom.geometry.x) \
                  & (geom.geometry.x <= bounds[2])
        if np.count_nonzero(zone_mask) == 0:
            continue
        LOGGER.info("dist_to_coast: UTM %d (%d/%d)",
                    epsg, izone + 1, len(zones))
        bounds = geom[zone_mask].total_bounds
        bounds = (bounds[0] - pad, bounds[1] - pad,
                  bounds[2] + pad, bounds[3] + pad)
        coast_mask = coast.envelope.intersects(box(*bounds))
        utm_coast = coast[coast_mask].geometry.unary_union
        utm_coast = gpd.GeoDataFrame(geometry=[utm_coast], crs=NE_CRS)
        utm_coast = utm_coast.to_crs(to_crs).geometry[0]
        dist[zone_mask] = geom[zone_mask].to_crs(to_crs).distance(utm_coast)
    return dist


def get_land_geometry(country_names=None, extent=None, resolution=10):
    """Get union of all the countries or the provided ones or the points inside
    the extent.

    Parameters:
        country_names (list, optional): list with ISO3 names of countries, e.g
            ['ZWE', 'GBR', 'VNM', 'UZB']
        extent (tuple, optional): (min_lon, max_lon, min_lat, max_lat)
        resolution (float, optional): 10, 50 or 110. Resolution in m. Default:
            10m, i.e. 1:10.000.000

    Returns:
        shapely.geometry.multipolygon.MultiPolygon
    """
    resolution = nat_earth_resolution(resolution)
    shp_file = shapereader.natural_earth(resolution=resolution,
                                         category='cultural',
                                         name='admin_0_countries')
    reader = shapereader.Reader(shp_file)
    if (country_names is None) and (extent is None):
        LOGGER.info("Computing earth's land geometry ...")
        geom = list(reader.geometries())
        geom = shapely.ops.cascaded_union(geom)

    elif country_names:
        countries = list(reader.records())
        geom = [country.geometry for country in countries
                if (country.attributes['ISO_A3'] in country_names) or
                (country.attributes['WB_A3'] in country_names) or
                (country.attributes['ADM0_A3'] in country_names)]
        geom = shapely.ops.cascaded_union(geom)

    else:
        extent_poly = Polygon([(extent[0], extent[2]), (extent[0], extent[3]),
                               (extent[1], extent[3]), (extent[1], extent[2])])
        geom = []
        for cntry_geom in reader.geometries():
            inter_poly = cntry_geom.intersection(extent_poly)
            if not inter_poly.is_empty:
                geom.append(inter_poly)
        geom = shapely.ops.cascaded_union(geom)
    if not isinstance(geom, MultiPolygon):
        geom = MultiPolygon([geom])
    return geom

def coord_on_land(lat, lon, land_geom=None):
    """Check if point is on land (True) or water (False) of provided coordinates.
    All globe considered if no input countries.

    Parameters:
        lat (np.array): latitude of points in epsg:4326
        lon (np.array): longitude of points in epsg:4326
        land_geom (shapely.geometry.multipolygon.MultiPolygon, optional):
            profiles of land.

    Returns:
        np.array(bool)
    """
    if lat.size != lon.size:
        LOGGER.error('Wrong size input coordinates: %s != %s.', lat.size,
                     lon.size)
        raise ValueError
    delta_deg = 1
    if land_geom is None:
        land_geom = get_land_geometry(extent=(np.min(lon)-delta_deg, \
            np.max(lon)+delta_deg, np.min(lat)-delta_deg, \
            np.max(lat)+delta_deg), resolution=10)
    return shapely.vectorized.contains(land_geom, lon, lat)

def nat_earth_resolution(resolution):
    """Check if resolution is available in Natural Earth. Build string.

    Parameters:
        resolution (int): resolution in millions, 110 == 1:110.000.000.

    Returns:
        str

    Raises:
        ValueError
    """
    avail_res = [10, 50, 110]
    if resolution not in avail_res:
        LOGGER.error('Natural Earth does not accept resolution %s m.',
                     resolution)
        raise ValueError
    return str(resolution) + 'm'

def get_country_geometries(country_names=None, extent=None, resolution=10):
    """Returns a gpd GeoSeries of natural earth multipolygons of the
    specified countries, resp. the countries that lie within the specified
    extent. If no arguments are given, simply returns the whole natural earth
    dataset.
    Take heed: we assume WGS84 as the CRS unless the Natural Earth download
    utility from cartopy starts including the projection information. (They
    are saving a whopping 147 bytes by omitting it.) Same goes for UTF.

    Parameters:
        country_names (list, optional): list with ISO3 names of countries, e.g
            ['ZWE', 'GBR', 'VNM', 'UZB']
        extent (tuple, optional): (min_lon, max_lon, min_lat, max_lat) assumed
            to be in the same CRS as the natural earth data.
        resolution (float, optional): 10, 50 or 110. Resolution in m. Default:
            10m

    Returns:
        GeoDataFrame
    """
    resolution = nat_earth_resolution(resolution)
    shp_file = shapereader.natural_earth(resolution=resolution,
                                         category='cultural',
                                         name='admin_0_countries')
    nat_earth = gpd.read_file(shp_file, encoding='UTF-8')

    if not nat_earth.crs:
        nat_earth.crs = NE_CRS

    # fill gaps in nat_earth
    gap_mask = (nat_earth['ISO_A3'] == '-99')
    nat_earth.loc[gap_mask, 'ISO_A3'] = nat_earth.loc[gap_mask, 'ADM0_A3']

    gap_mask = (nat_earth['ISO_N3'] == '-99')
    for idx in nat_earth[gap_mask].index:
        for col in ['ISO_A3', 'ADM0_A3', 'NAME']:
            try:
                num = iso_cntry.get(nat_earth.loc[idx, col]).numeric
            except KeyError:
                continue
            else:
                nat_earth.loc[idx, 'ISO_N3'] = num
                break

    out = nat_earth
    if country_names:
        if isinstance(country_names, str):
            country_names = [country_names]
        out = out[out.ISO_A3.isin(country_names)]

    if extent:
        bbox = Polygon([
            (extent[0], extent[2]),
            (extent[0], extent[3]),
            (extent[1], extent[3]),
            (extent[1], extent[2])
        ])
        bbox = gpd.GeoSeries(bbox, crs=out.crs)
        bbox = gpd.GeoDataFrame({'geometry': bbox}, crs=out.crs)
        out = gpd.overlay(out, bbox, how="intersection")

    return out

def get_isimip_natids(lat, lon):
    """ Get ISIMIP NatIDs of given coordinates

    Parameters:
        lat (np.array): latitude of points in epsg:4326
        lon (np.array): longitude of points in epsg:4326

    Returns:
        natids (np.array): NatIDs between 1 and 230. The value is 0 in oceans.
    """
    lat, lon = [np.asarray(ar).ravel() for ar in [lat, lon]]
    latlon = np.vstack([lat, lon]).T
    isimip_grid = xr.open_dataset(GLB_CENTROIDS_NC)
    isimip_lon = isimip_grid.lon.data
    isimip_lat = isimip_grid.lat.data
    natids = isimip_grid.NatIdGrid.data
    natids[np.isnan(natids)] = 0
    natids = scipy.interpolate.interpn((isimip_lat, isimip_lon), natids,
                                       latlon, method='nearest',
                                       bounds_error=False, fill_value=0)
    return natids

def get_isimip_gridpoints(countries=None, regions=None, iso=True, rect=False):
    """ Get coordinates of gridpoints in specified countries or regions

    Parameters:
        countries (list, optional): ISO 3166-1 alpha-3 codes of countries, or
            ISIMIP NatID if iso is set to False.
        regions (list, optional): Region IDs.
        iso (bool, optional): If True, assume that countries are given by their
            ISO 3166-1 alpha-3 codes (instead of the ISIMIP NatID).
            Default: True.
        box (bool, optional): If True, a rectangular box around the specified
            countries/regions is selected. Default: False.

    Returns:
        lat (np.array): latitude of points in epsg:4326
        lon (np.array): longitude of points in epsg:4326
    """
    if countries is None:
        countries = []
    if regions is None:
        regions = []

    LOGGER.info("select area for countries: %s", str(countries))

    isimip_grid = xr.open_dataset(GLB_CENTROIDS_NC)
    isimip_lon = isimip_grid.lon.data
    isimip_lat = isimip_grid.lat.data
    lon, lat = np.meshgrid(isimip_lon, isimip_lat)

    if iso:
        countries = isimip_iso2natid(countries)
    countries += isimip_region2natids(regions)
    countries = np.unique(countries)

    if len(countries) > 0:
        msk = np.isin(isimip_grid.NatIdGrid.data, countries)
        if rect:
            msk = msk.any(axis=0)[None] * msk.any(axis=1)[:, None]
            msk |= (lat >= np.floor(lat[msk].min())) \
                 & (lon >= np.floor(lon[msk].min())) \
                 & (lat <= np.ceil(lat[msk].max())) \
                 & (lon <= np.ceil(lon[msk].max()))
        lat, lon = lat[msk], lon[msk]
    else:
        lat, lon = [ar.ravel() for ar in [lat, lon]]
    return lat, lon

def isimip_region2natids(regions):
    """ Convert region names to ISIMIP NatIDs of countries

    Parameters:
        regions (str or list of str): Region name(s).

    Returns:
        natids (list of str): Sorted list of NatIDs of all countries in
            specified region(s).
    """
    regions = [regions] if isinstance(regions, str) else regions

    natid_info = pd.read_csv(NAT_REG_ID)
    natids = []
    for region in regions:
        region_msk = (natid_info['Reg_name'] == region)
        if not any(region_msk):
            LOGGER.error('Unknown region name: %s', region)
            raise KeyError
        natids += list(natid_info['ID'][region_msk].values)
    return list(set(natids))

def isimip_iso2natid(isos):
    """ Convert ISO 3166-1 alpha-3 codes to ISIMIP NatIDs

    Parameters:
        isos (str or list of str): ISO codes of countries (or single code).

    Returns:
        natids (str or list of str): NatIDs.
    """
    return_str = isinstance(isos, str)
    isos = [isos] if return_str else isos

    natid_info = pd.read_csv(NAT_REG_ID)
    natids = []
    for iso in isos:
        country_msk = (natid_info['ISO'] == iso)
        if not any(country_msk):
            LOGGER.error('Unknown country ISO: %s', iso)
            raise KeyError
        natids.append(int(natid_info['ID'][country_msk].values[0]))

    return natids[0] if return_str else natids

def get_country_code(lat, lon):
    """ Provide numeric country iso code for every point.

    Parameters:
        lat (np.array): latitude of points in epsg:4326
        lon (np.array): longitude of points in epsg:4326

    Returns:
        np.array(int)
    """
    lat = np.array(lat)
    lon = np.array(lon)
    LOGGER.debug('Setting region_id %s points.', str(lat.size))
    countries = get_country_geometries(extent=(lon.min()-0.001, lon.max()+0.001,
                                               lat.min()-0.001, lat.max()+0.001))
    region_id = np.zeros(lon.size, dtype=int)
    for geom in zip(countries.geometry, countries.ISO_N3):
        select = shapely.vectorized.contains(geom[0], lon, lat)
        region_id[select] = int(geom[1])
    return region_id

def get_admin1_info(country_names):
    """ Provide registry info and shape files for admin1 regions

    Parameters:
        country_names (list): list with ISO3 names of countries, e.g.
                ['ZWE', 'GBR', 'VNM', 'UZB']

    Returns:
        admin1_info (dict)
        admin1_shapes (dict)
    """

    if isinstance(country_names, str):
        country_names = [country_names]
    admin1_file = shapereader.natural_earth(resolution='10m',
                                            category='cultural',
                                            name='admin_1_states_provinces')
    admin1_recs = shapefile.Reader(admin1_file)
    admin1_info = dict()
    admin1_shapes = dict()
    for iso3 in country_names:
        admin1_info[iso3] = list()
        admin1_shapes[iso3] = list()
        for rec, rec_shp in zip(admin1_recs.records(), admin1_recs.shapes()):
            if rec['adm0_a3'] == iso3:
                admin1_info[iso3].append(rec)
                admin1_shapes[iso3].append(rec_shp)
    return admin1_info, admin1_shapes

def get_resolution(lat, lon, min_resol=1.0e-8):
    """ Compute resolution of points in lat and lon

    Parameters:
        lat (np.array): latitude of points
        lon (np.array): longitude of points
        min_resol (float, optional): minimum resolution to consider. Default: 1.0e-8.

    Returns:
        float
    """
    # ascending lat and lon
    res_lat, res_lon = np.diff(np.sort(lat)), np.diff(np.sort(lon))
    try:
        res_lat = res_lat[res_lat > min_resol].min()
    except ValueError:
        res_lat = 0
    try:
        res_lon = res_lon[res_lon > min_resol].min()
    except ValueError:
        res_lon = 0
    return res_lat, res_lon

def pts_to_raster_meta(points_bounds, res):
    """" Transform vector data coordinates to raster. Returns number of rows,
    columns and affine transformation

    Parameters:
        points_bounds (tuple): points total bounds (xmin, ymin, xmax, ymax)
        res (float): resolution of output raster

    Returns:
        int, int, affine.Affine
    """
    xmin, ymin, xmax, ymax = points_bounds
    rows = int(np.floor((ymax-ymin) /  res) + 1)
    cols = int(np.floor((xmax-xmin) / res) + 1)
    ras_trans = from_origin(xmin - res / 2, ymax + res / 2, res, res)
    if xmax > xmin - res / 2 + cols * res:
        cols += 1
    if ymin < ymax + res / 2 - rows * res:
        rows += 1
    return rows, cols, ras_trans

def equal_crs(crs_one, crs_two):
    """ Compare two crs

    Parameters:
        crs_one (dict or string or wkt): user crs
        crs_two (dict or string or wkt): user crs

    Returns:
        bool
    """
    return CRS.from_user_input(crs_one) == CRS.from_user_input(crs_two)

def read_raster(file_name, band=[1], src_crs=None, window=False, geometry=False,
                dst_crs=False, transform=None, width=None, height=None,
                resampling=Resampling.nearest):
    """ Read raster of bands and set 0 values to the masked ones. Each
    band is an event. Select region using window or geometry. Reproject
    input by proving dst_crs and/or (transform, width, height). Returns matrix
    in 2d: band x coordinates in 1d (evtl. reshape to band x height x width)

    Parameters:
        file_name (str): name of the file
        band (list(int), optional): band number to read. Default: 1
        window (rasterio.windows.Window, optional): window to read
        geometry (shapely.geometry, optional): consider pixels only in shape
        dst_crs (crs, optional): reproject to given crs
        transform (rasterio.Affine): affine transformation to apply
        wdith (float): number of lons for transform
        height (float): number of lats for transform
        resampling (rasterio.warp,.Resampling optional): resampling
            function used for reprojection to dst_crs

    Returns:
        dict (meta), np.array (band x coordinates_in_1d)
    """
    LOGGER.info('Reading %s', file_name)
    if os.path.splitext(file_name)[1] == '.gz':
        file_name = '/vsigzip/' + file_name
    with rasterio.Env():
        with rasterio.open(file_name, 'r') as src:
            if src_crs is None:
                src_meta = CRS.from_dict(DEF_CRS) if not src.crs else src.crs
            else:
                src_meta = src_crs
            if dst_crs or transform:
                LOGGER.debug('Reprojecting ...')
                if not dst_crs:
                    dst_crs = src_meta
                if not transform:
                    transform, width, height = calculate_default_transform(\
                        src_meta, dst_crs, src.width, src.height, *src.bounds)
                dst_meta = src.meta.copy()
                dst_meta.update({'crs': dst_crs,
                                 'transform': transform,
                                 'width': width,
                                 'height': height
                                })
                kwargs = {}
                if src.meta['nodata']:
                    kwargs['src_nodata'] = src.meta['nodata']
                    kwargs['dst_nodata'] = src.meta['nodata']

                intensity = np.zeros((len(band), height, width))
                for idx_band, i_band in enumerate(band):
                    reproject(source=src.read(i_band),
                              destination=intensity[idx_band, :],
                              src_transform=src.transform,
                              src_crs=src_meta,
                              dst_transform=transform,
                              dst_crs=dst_crs,
                              resampling=resampling,
                              **kwargs)

                    if dst_meta['nodata'] and np.isnan(dst_meta['nodata']):
                        intensity[idx_band, :][np.isnan(intensity[idx_band, :])] = 0
                    else:
                        intensity[idx_band, :][intensity[idx_band, :] == dst_meta['nodata']] = 0
                meta = dst_meta

                if geometry:
                    intensity = intensity.astype('float32')
                    # update driver to GTiff as netcdf does not work reliably
                    meta.update(driver='GTiff')
                    with MemoryFile() as memfile:
                        # Open as DatasetWriter
                        with memfile.open(**meta) as dst_inten:
                            dst_inten.write(intensity)
                        # Reopen as DatasetReader
                        with memfile.open() as dst_inten:
                            inten, mask_trans = mask(dst_inten, geometry,
                                                     crop=True, indexes=band)
                            meta.update({"height": inten.shape[1],
                                         "width": inten.shape[2],
                                         "transform": mask_trans})
                    intensity = inten[range(len(band)), :]
                    intensity = intensity.astype('float64')
                    # reset nodata values again as driver Gtiff resets them again
                    if dst_meta['nodata'] and np.isnan(dst_meta['nodata']):
                        intensity[np.isnan(intensity)] = 0
                    else:
                        intensity[intensity == dst_meta['nodata']] = 0

                return meta, intensity.reshape((len(band), meta['height']*meta['width']))

            meta = src.meta.copy()
            if geometry:
                inten, mask_trans = mask(src, geometry, crop=True, indexes=band)
                if meta['nodata'] and np.isnan(meta['nodata']):
                    inten[np.isnan(inten)] = 0
                else:
                    inten[inten == meta['nodata']] = 0
                meta.update({"height": inten.shape[1],
                             "width": inten.shape[2],
                             "transform": mask_trans})
            else:
                masked_array = src.read(band, window=window, masked=True)
                inten = masked_array.data
                inten[masked_array.mask] = 0
                if window:
                    meta.update({"height": inten.shape[1], \
                        "width": inten.shape[2], \
                        "transform": rasterio.windows.transform(window, src.transform)})
            if not meta['crs']:
                meta['crs'] = CRS.from_dict(DEF_CRS)
            intensity = inten[range(len(band)), :]
            return meta, intensity.reshape((len(band), meta['height']*meta['width']))

def read_vector(file_name, field_name, dst_crs=None):
    """ Read vector file format supported by fiona. Each field_name name is
    considered an event.

    Parameters:
        file_name (str): vector file with format supported by fiona and
            'geometry' field.
        field_name (list(str)): list of names of the columns with values.
        dst_crs (crs, optional): reproject to given crs

    Returns:
        np.array (lat), np.array (lon), geometry (GeiSeries), np.array (value)
    """
    LOGGER.info('Reading %s', file_name)
    data_frame = gpd.read_file(file_name)
    if not data_frame.crs:
        data_frame.crs = DEF_CRS
    if dst_crs is None:
        geometry = data_frame.geometry
    else:
        geometry = data_frame.geometry.to_crs(dst_crs)
    lat, lon = geometry[:].y.values, geometry[:].x.values
    value = np.zeros([len(field_name), lat.size])
    for i_inten, inten in enumerate(field_name):
        value[i_inten, :] = data_frame[inten].values
    return lat, lon, geometry, value

def write_raster(file_name, data_matrix, meta):
    """ Write raster in GeoTiff format

    Parameters:
        fle_name (str): file name to write
        data_matrix (np.array): 2d raster data. Either containing one band,
            or every row is a band and the column represents the grid in 1d.
        meta (dict): rasterio meta dictionary containing raster
            properties: width, height, crs and transform must be present
            at least (transform needs to contain upper left corner!)
    """
    LOGGER.info('Writting %s', file_name)
    if data_matrix.shape != (meta['height'], meta['width']):
        # every row is an event (from hazard intensity or fraction) == band
        profile = copy.deepcopy(meta)
        profile.update(driver='GTiff', dtype=rasterio.float32, count=data_matrix.shape[0])
        with rasterio.open(file_name, 'w', **profile) as dst:
            dst.write(np.asarray(data_matrix, dtype=rasterio.float32).\
                reshape((data_matrix.shape[0], profile['height'], profile['width'])), \
                indexes=np.arange(1, data_matrix.shape[0]+1))
    else:
        # only one band
        profile = copy.deepcopy(meta)
        profile.update(driver='GTiff', dtype=rasterio.float32, count=1)
        with rasterio.open(file_name, 'w', **profile) as dst:
            dst.write(np.asarray(data_matrix, dtype=rasterio.float32))

def points_to_raster(points_df, val_names=['value'], res=None, raster_res=None,
                     scheduler=None):
    """ Compute raster matrix and transformation from value column

    Parameters:
        points_df (GeoDataFrame): contains columns latitude, longitude and in
            val_names
        res (float, optional): resolution of current data in units of latitude
            and longitude, approximated if not provided.
        raster_res (float, optional): desired resolution of the raster
        scheduler (str): used for dask map_partitions. “threads”,
                “synchronous” or “processes”

    Returns:
        np.array, affine.Affine

    """

    if not res:
        res = min(get_resolution(points_df.latitude.values, points_df.longitude.values))
    if not raster_res:
        raster_res = res

    def apply_box(df_exp):
        return df_exp.apply((lambda row: Point(row.longitude, row.latitude). \
                             buffer(res/2).envelope), axis=1)
    LOGGER.info('Raster from resolution %s to %s.', res, raster_res)
    df_poly = points_df[val_names]
    if not scheduler:
        df_poly['geometry'] = apply_box(points_df)
    else:
        ddata = dd.from_pandas(points_df[['latitude', 'longitude']],
                               npartitions=cpu_count())
        df_poly['geometry'] = ddata.map_partitions(apply_box, meta=Polygon).\
        compute(scheduler=scheduler)
    # construct raster
    xmin, ymin, xmax, ymax = points_df.longitude.min(), points_df.latitude.min(), \
    points_df.longitude.max(), points_df.latitude.max()
    rows, cols, ras_trans = pts_to_raster_meta((xmin, ymin, xmax, ymax), raster_res)
    raster_out = np.zeros((len(val_names), rows, cols))
    # TODO: parallel rasterize
    for i_val, val_name in enumerate(val_names):
        raster_out[i_val, :, :] = rasterize([(x, val) for (x, val) in zip(df_poly.geometry, \
            df_poly[val_name])], out_shape=(rows, cols), transform=ras_trans, \
            fill=0, all_touched=True, dtype=rasterio.float32, )
    meta = {'crs': points_df.crs, 'height':rows, 'width':cols, 'transform': ras_trans}
    return raster_out, meta

def set_df_geometry_points(df_val, scheduler=None):
    """ Set given geometry to given dataframe using dask if scheduler

    Parameters:
        df_val (DataFrame or GeoDataFrame): contains latitude and longitude columns
        scheduler (str): used for dask map_partitions. “threads”,
                “synchronous” or “processes”
    """
    LOGGER.info('Setting geometry points.')
    def apply_point(df_exp):
        return df_exp.apply((lambda row: Point(row.longitude, row.latitude)), axis=1)
    if not scheduler:
        df_val['geometry'] = apply_point(df_val)
    else:
        ddata = dd.from_pandas(df_val, npartitions=cpu_count())
        df_val['geometry'] = ddata.map_partitions(apply_point, meta=Point).\
        compute(scheduler=scheduler)
