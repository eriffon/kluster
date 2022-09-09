import os
import xarray as xr
import numpy as np
from osgeo import gdal
from pyproj import Transformer, CRS, Geod
from typing import Union
from datetime import datetime
import geohash
from shapely import geometry
import queue

from HSTB.kluster.xarray_helpers import stack_nan_array, reform_nan_array
from HSTB.kluster import kluster_variables

try:
    from vyperdatum.points import VyperPoints
    from vyperdatum.core import VyperCore, vertical_datum_to_wkt, DatumData
    from vyperdatum.vypercrs import VyperPipelineCRS, CompoundCRS
    vyperdatum_found = True
except ModuleNotFoundError:
    vyperdatum_found = False

try:
    from aviso import fes
    fes_grids = list(fes.regional_sep_catalog.keys())
    fes_grids = [fg for fg in fes_grids if os.path.exists(fes.__dict__[fg])]
    fes_found = True
except ModuleNotFoundError:
    fes_found = False
    fes_grids = []

fes_model = None
fes_model_description = ''


def distrib_run_georeference(dat: list):
    """
    Convenience function for mapping build_beam_pointing_vectors across cluster.  Assumes that you are mapping this
    function with a list of data.

    distrib functions also return a processing status array, here a beamwise array = 4, which states that all
    processed beams are at the 'georeference' status level

    Parameters
    ----------
    dat
        [sv_data, altitude, longitude, latitude, heading, heave, waterline, vert_ref, horizontal_crs, z_offset, vdatum_directory, tide_corrector]

    Returns
    -------
    list
        [xr.DataArray alongtrack offset (time, beam), xr.DataArray acrosstrack offset (time, beam),
         xr.DataArray down offset (time, beam), xr.DataArray corrected heave for TX - RP lever arm, all zeros if in 'ellipse' mode (time),
         xr.DataArray corrected altitude for TX - RP lever arm, all zeros if in 'vessel' or 'waterline' mode (time),
         processing_status]
    """

    ans = georef_by_worker(dat[0], dat[1], dat[2], dat[3], dat[4], dat[5], dat[6], dat[7], dat[8], dat[9], dat[10], dat[11], dat[12])
    # return processing status = 4 for all affected soundings
    processing_status = xr.DataArray(np.full_like(dat[0][0], 4, dtype=np.uint8),
                                     coords={'time': dat[0][0].coords['time'],
                                             'beam': dat[0][0].coords['beam']},
                                     dims=['time', 'beam'])
    ans.append(processing_status)
    return ans


def georef_by_worker(sv_corr: list, alt: xr.DataArray, lon: xr.DataArray, lat: xr.DataArray, hdng: xr.DataArray,
                     heave: xr.DataArray, wline: float, vert_ref: str, input_crs: CRS, horizontal_crs: CRS,
                     z_offset: float, vdatum_directory: str = None, tide_corrector: xr.DataArray = None):
    """
    Use the raw attitude/navigation to transform the vessel relative along/across/down offsets to georeferenced
    soundings.  Will support transformation to geographic and projected coordinate systems and with a vertical
    reference that you select.

    Parameters
    ----------
    sv_corr
        [x, y, z] offsets generated with sv_correct
    alt
        1d (time) altitude in meters
    lon
        1d (time) longitude in degrees
    lat
        1d (time) latitude in degrees
    hdng
        1d (time) heading in degrees
    heave
        1d (time) heave in degrees
    wline
        waterline offset from reference point
    vert_ref
        vertical reference point, one of ['ellipse', 'vessel', 'waterline', 'NOAA MLLW', 'NOAA MHW]
    input_crs
        pyproj CRS object, input coordinate reference system information for this run
    horizontal_crs
        pyproj CRS object, destination coordinate reference system information for this run
    z_offset
        lever arm from reference point to transmitter
    vdatum_directory
        if 'NOAA MLLW' 'NOAA MHW' is the vertical reference, a path to the vdatum directory is required here
    tide_corrector
        if 'Aviso MLLW' is the vertical reference, this is the tide correction in meters

    Returns
    -------
    list
        [xr.DataArray easting (time, beam), xr.DataArray northing (time, beam), xr.DataArray depth (time, beam),
         xr.DataArray corrected heave for TX - RP lever arm, all zeros if in 'ellipse' mode (time),
         xr.DataArray corrected altitude for TX - RP lever arm, all zeros if in 'vessel' or 'waterline' mode (time),
         xr.DataArray VDatum uncertainty if using a VDatum vertical reference, all zeros otherwise,
         xr.DataArray computed geohash as string encoded base32]
    """

    g = horizontal_crs.get_geod()

    # unpack the sv corrected data output
    alongtrack = sv_corr[0]
    acrosstrack = sv_corr[1]
    depthoffset = sv_corr[2] + z_offset
    # generate the corrected depth offset depending on the desired vertical reference
    corr_dpth = None
    corr_heave = None
    corr_altitude = None
    if vert_ref in kluster_variables.ellipse_based_vertical_references:
        # first bring depths to the ellipse, then use vyperdatum below to transform to the desired ers datum
        if vert_ref == 'ellipse':
            corr_altitude = transform_ellipse(lon, lat, alt, input_crs, horizontal_crs)
        else:
            corr_altitude = alt
        corr_heave = xr.zeros_like(corr_altitude)
        corr_dpth = (depthoffset - corr_altitude.values[:, None]).astype(np.float32) * -1
    elif vert_ref == 'vessel':
        corr_heave = heave
        corr_altitude = xr.zeros_like(corr_heave)
        corr_dpth = (depthoffset + corr_heave.values[:, None]).astype(np.float32)
    elif vert_ref == 'waterline':
        corr_heave = heave
        corr_altitude = xr.zeros_like(corr_heave)
        corr_dpth = (depthoffset + corr_heave.values[:, None] - wline).astype(np.float32)
    elif vert_ref == 'Aviso MLLW':
        corr_heave = heave
        corr_altitude = xr.zeros_like(corr_heave)
        corr_dpth = (depthoffset + corr_heave.values[:, None] - wline - tide_corrector).astype(np.float32)

    # get the sv corrected alongtrack/acrosstrack offsets stacked without the NaNs (arrays have NaNs for beams that do not exist in that sector)
    at_idx, alongtrack_stck = stack_nan_array(alongtrack, stack_dims=('time', 'beam'))
    ac_idx, acrosstrack_stck = stack_nan_array(acrosstrack, stack_dims=('time', 'beam'))

    # determine the beam wise offsets
    bm_azimuth = np.rad2deg(np.arctan2(acrosstrack_stck, alongtrack_stck)) + np.float32(hdng[at_idx[0]].values)
    bm_radius = np.sqrt(acrosstrack_stck ** 2 + alongtrack_stck ** 2)
    pos = g.fwd(lon[at_idx[0]].values, lat[at_idx[0]].values, bm_azimuth.values, bm_radius.values)
    z = np.around(corr_dpth, 3)

    if vert_ref in ['NOAA MLLW', 'NOAA MHW']:
        z_stck = z.values[ac_idx]  # get the depth values where there are valid acrosstrack results (i.e. svcorrect worked)
        if vert_ref == 'NOAA MLLW':
            z_stck, vdatum_unc = transform_vyperdatum(pos[0], pos[1], z_stck, input_crs.to_epsg(), 'mllw', vdatum_directory=vdatum_directory, horizontal_crs=horizontal_crs)
        else:
            z_stck, vdatum_unc = transform_vyperdatum(pos[0], pos[1], z_stck, input_crs.to_epsg(), 'mhw', vdatum_directory=vdatum_directory, horizontal_crs=horizontal_crs)
        vdatum_unc = reform_nan_array(vdatum_unc, ac_idx, z.shape, z.coords, z.dims)
        z = reform_nan_array(z_stck, ac_idx, z.shape, z.coords, z.dims)
    else:
        vdatum_unc = xr.zeros_like(z)

    # compute the geohash for each beam return, the base32 encoded cell that the beam falls within, used for spatial indexing
    try:
        ghash = compute_geohash(pos[1], pos[0], precision=kluster_variables.geohash_precision)
    except:
        ghash = np.array([' ' * kluster_variables.geohash_precision])

    if horizontal_crs.is_projected:
        # Transformer.transform input order is based on the CRS, see CRS.geodetic_crs.axis_info
        # - lon, lat - this appears to be valid when using CRS from proj4 string
        # - lat, lon - this appears to be valid when using CRS from epsg
        # use the always_xy option to force the transform to expect lon/lat order
        georef_transformer = Transformer.from_crs(input_crs, horizontal_crs, always_xy=True)
        newpos = georef_transformer.transform(pos[0], pos[1], errcheck=False)  # longitude / latitude order (x/y)
    else:
        newpos = pos

    bad_nav_mask = np.isinf(newpos[0])
    if bad_nav_mask.any():
        newpos[0][bad_nav_mask] = np.nan
        newpos[1][bad_nav_mask] = np.nan
    x = reform_nan_array(np.around(newpos[0], 3), at_idx, alongtrack.shape, alongtrack.coords, alongtrack.dims)
    y = reform_nan_array(np.around(newpos[1], 3), ac_idx, acrosstrack.shape, acrosstrack.coords, acrosstrack.dims)
    ghash = reform_nan_array(ghash, ac_idx, acrosstrack.shape, acrosstrack.coords, acrosstrack.dims)
    if bad_nav_mask.any():
        final_mask = ~np.isnan(x)
        if final_mask.any():
            z = z.where(final_mask, np.nan)
            vdatum_unc = vdatum_unc.where(final_mask, np.nan)
            ghash = ghash.where(final_mask, b' ' * kluster_variables.geohash_precision)

    return [x, y, z, corr_heave, corr_altitude, vdatum_unc, ghash]


def transform_ellipse(x: Union[np.array, xr.DataArray], y: Union[np.array, xr.DataArray], z: Union[np.array, xr.DataArray],
                      source_datum: CRS, final_datum: CRS):
    """
    For ellipsoidally relative datums (mllw for instance) the ellipsoid transformation is included in the overall vertical
    transformation.  If vertical reference is 'ellipse' we need a separate process to handle this transformation.  If the input/output
    vertical datum is the same (i.e. both are NAD83/GRS80) we can return the existing z.  Otherwise, use PROJ to transform from
    input vertical datum to output vertical datum.

    Parameters
    ----------
    x
        easting for each point in source_datum coordinate system
    y
        northing for each point in source_datum coordinate system
    z
        depth offset for each point in source_datum coordinate system
    source_datum
        The horizontal coordinate system of the xyz provided, should be a string identifier ('nad83') or an EPSG code
        specifying the horizontal coordinate system
    final_datum
        horizontal coordinate system of the desired output data

    Returns
    -------
    Union[np.array, xr.DataArray]
        corrected altitude for ellipsoid transformation
    """

    expected_names = ['ITRF2008', 'ITRF2014', 'ITRF2020', 'WGS 84', 'WGS84', 'NAD83']
    try:
        input_name = expected_names[np.where([source_datum.name.find(en) != -1 for en in expected_names])[0][0]]
    except:
        raise ValueError(f'ERROR: Unable to determine the associated ellipsoid for input datum {source_datum.name}')
    try:
        final_name = expected_names[np.where([final_datum.name.find(en) != -1 for en in expected_names])[0][0]]
    except:
        raise ValueError(f'ERROR: Unable to determine the associated ellipsoid for output datum {final_datum.name}')
    if input_name == final_name:
        return z
    else:
        # currently use ITRF2014 as WGS84 equivalent
        expected_epsg = {'ITRF2008': 7911, 'ITRF2014': 7912, 'ITRF2020': 9989, 'WGS 84': 7912, 'WGS84': 7912, 'NAD83': 6319}
        georef_transformer = Transformer.from_crs(CRS.from_epsg(expected_epsg[input_name]), CRS.from_epsg(expected_epsg[final_name]), always_xy=True)
        final_altitude = georef_transformer.transform(x, y, z)[-1]
        if isinstance(z, np.ndarray):
            return final_altitude
        else:
            return xr.DataArray(final_altitude, coords={'time': z.time})


def transform_vyperdatum(x: np.array, y: np.array, z: np.array, source_datum: Union[str, int] = 'nad83',
                         final_datum: str = 'mllw', vdatum_directory: str = None, horizontal_crs: CRS = None):
    """
    When we specify a NOAA vertical datum (NOAA Mean Lower Low Water, NOAA Mean High Water) in Kluster, we use
    vyperdatum/VDatum to transform the points to the appropriate vertical datum.

    Parameters
    ----------
    x
        easting for each point in source_datum coordinate system
    y
        northing for each point in source_datum coordinate system
    z
        depth offset for each point in source_datum coordinate system
    source_datum
        The horizontal coordinate system of the xyz provided, should be a string identifier ('nad83') or an EPSG code
        specifying the horizontal coordinate system
    final_datum
        The desired final_datum vertical datum as a string (one of 'mllw', 'mhw')
    vdatum_directory
        if 'NOAA MLLW' 'NOAA MHW' is the vertical reference, a path to the vdatum directory is required here
    horizontal_crs
        if included here, we use it to determine if we should include an output datum, which can be used to determine a 3d shift

    Returns
    -------
    xr.DataArray
        original z array with vertical transformation applied, this new z is at final_datum
    xr.DataArray
        uncertainty associated with the vertical transformation between the source and destination datum
    """

    if final_datum == 'mllw':  # we need to let vyperdatum know this is positive down, do that by giving it the mllw epsg
        final_datum = 5866
    horizontal_crs = None
    if horizontal_crs:
        if horizontal_crs.name.find('NAD83'):
            final_datum = (kluster_variables.epsg_nad83, final_datum)
        elif horizontal_crs.name.find('WGS'):
            final_datum = (kluster_variables.epsg_wgs84, final_datum)

    if vdatum_directory:
        vp = VyperPoints(vdatum_directory=vdatum_directory, silent=True)
    else:
        vp = VyperPoints(silent=True)

    if not os.path.exists(vp.datum_data.vdatum_path):
        raise EnvironmentError('Unable to find path to VDatum folder: {}'.format(vp.datum_data.vdatum_path))
    if source_datum == 'nad83':
        source_datum = kluster_variables.epsg_nad83
    elif source_datum == 'wgs84':
        source_datum = kluster_variables.epsg_wgs84
    vp.transform_points((source_datum, 'ellipse'), final_datum, x, y, z=z, sample_distance=0.0001)  # sample distance in degrees

    return np.around(vp.z, 3), np.around(vp.unc, 3)


def apply_grid_to_soundings(grid_file: str, x_loc: np.ndarray, y_loc: np.ndarray, sounding_datum: CRS):
    # IN PROGRESS
    dataset = gdal.Open(grid_file)
    w_raster = f'/vsimem/{os.path.split(grid_file)[1]}_{datetime.now().timestamp()}/'
    w_raster_ds = gdal.Warp(w_raster, grid_file, dstSRS=f'EPSG:{sounding_datum.to_epsg()}')

    band = w_raster_ds.GetRasterBand(1)

    cols = dataset.RasterXSize
    rows = dataset.RasterYSize

    transform = dataset.GetGeoTransform()

    xOrigin = transform[0]
    yOrigin = transform[3]
    pixelWidth = transform[1]
    pixelHeight = -transform[5]

    data = band.ReadAsArray(0, 0, cols, rows)
    #
    # points_list = [(355278.165927, 4473095.13829), (355978.319525, 4472871.11636)]  # list of X,Y coordinates
    #
    # for point in points_list:
    #     col = int((point[0] - xOrigin) / pixelWidth)
    #     row = int((yOrigin - point[1]) / pixelHeight)
    #
    # data = row, col, data[row][col]


def set_vyperdatum_vdatum_path(vdatum_path: str):
    """
    Set the vyperdatum VDatum path, required to use the VDatum grids to do the vertical transformations

    Parameters
    ----------
    vdatum_path
        path to the vdatum folder

    Returns
    -------
    err
        True if there was an error in setting the vyperdatum vdatum path
    status
        status message from vyperdatum
    """

    err = False
    status = ''
    try:
        # first time setting vdatum path sets the settings file with the correct path
        vc = VyperCore(vdatum_directory=vdatum_path)
        if not vc.datum_data.vdatum_version:
            assert False
        status = 'Found {} at {}'.format(vc.datum_data.vdatum_version, vc.datum_data.vdatum_path)
    except:
        err = True
        status = 'No valid vdatum found at {}'.format(vdatum_path)
    return err, status


def clear_vdatum_path():
    """
    clear the set vdatum path in the vyperdatum configuration
    """
    try:
        # first try the workflow for when VyperCore has been set up with a vdatum path
        #  this initialization below works, because it has a saved vdatum_path already in settings
        vc = VyperCore()
        # remove the saved vdatum_path
        vc.datum_data.remove_from_config('vdatum_path')
    except:
        # special case for where VyperCore is either initialized with a broken path, or no path at all
        # initialize the datumdata object with a fake directory
        datum_data = DatumData(vdatum_directory=r'c:\\')
        # and then remove that directory to clear the vdatum path
        datum_data.remove_from_config('vdatum_path')


def new_geohash(latitude: float, longitude: float, precision: int):
    """
    compute new geohash for given latitude longitude

    Parameters
    ----------
    latitude
        latitude as float
    longitude
        longitude as float
    precision
        precision as integer

    Returns
    -------
    bytes
        geohash string encoded as bytes
    """

    if np.isnan(latitude) or np.isnan(longitude):
        return b' ' * precision
    return geohash.encode(latitude, longitude, precision=precision).encode()


def compute_geohash(latitude: np.array, longitude: np.array, precision: int):
    """
    Geohash is a geocoding method to encode a specific latitude/longitude into a string representing an area of a given
    precision.  String is a custom base32 implementation encoded string.  We use the python-geohash library to do this.
    The result is a string of length = precision encoding the position.  Higher precision will give you a more
    accurate geohash, i.e. smaller tile.

    Parameters
    ----------
    latitude
        numpy array of latitude values
    longitude
        numpy array of longitude values
    precision
        integer precision, the length of the returned string, higher precision generates a smaller cell area code

    Returns
    -------
    np.array
        array of bytestrings dtype='SX' where X is the precision you have given
    """
    if latitude.size > 1:
        vectorhash = np.vectorize(new_geohash)
        return vectorhash(latitude, longitude, precision)
    else:
        return new_geohash(latitude, longitude, precision)


def decode_geohash(ghash: Union[str, bytes]):
    """
    Take the given geohash and return the centroid of the geohash cell

    Parameters
    ----------
    ghash
        string geohash or bytestring geohash
    Returns
    -------
    float
        latitude
    float
        longitude
    """

    if isinstance(ghash, str):
        return geohash.decode(ghash)
    else:
        return geohash.decode(ghash.decode())


def geohash_to_polygon(ghash: Union[str, bytes]):
    """
    Take a geohash string and return the shapely polygon object that represents the geohash cell

    Parameters
    ----------
    ghash
        string geohash or bytestring geohash

    Returns
    -------
    geometry.Polygon
        Polygon object for the geohash cell
    """

    if isinstance(ghash, str):
        lat_centroid, lng_centroid, lat_offset, lng_offset = geohash.decode_exactly(ghash)
    else:
        lat_centroid, lng_centroid, lat_offset, lng_offset = geohash.decode_exactly(ghash.decode())

    corner_1 = (lat_centroid - lat_offset, lng_centroid - lng_offset)[::-1]
    corner_2 = (lat_centroid - lat_offset, lng_centroid + lng_offset)[::-1]
    corner_3 = (lat_centroid + lat_offset, lng_centroid + lng_offset)[::-1]
    corner_4 = (lat_centroid + lat_offset, lng_centroid - lng_offset)[::-1]

    return geometry.Polygon([corner_1, corner_2, corner_3, corner_4, corner_1])


def polygon_to_geohashes(polygon: Union[np.array, geometry.Polygon], precision):
    """
    Take a polygon and return a list of all of the geohash codes/cells that are completely inside and those that are
    intersecting

    Parameters
    ----------
    polygon
        polygon as an existing shapely polygon object or a numpy array of coordinates (lat, lon order)
    precision
        length of the geohash string

    Returns
    -------
    list
        list of bytestrings for the geohash cells that are completely inside the polygon
    list
        list of bytestrings for the geohash cells that only intersect the polygon
    """

    if not isinstance(polygon, geometry.Polygon):
        polygon = geometry.Polygon(polygon)

    intersect_geohashes = set()
    inner_geohashes = set()
    outer_geohashes = set()

    envelope = polygon.envelope
    centroid = polygon.centroid

    testing_geohashes = queue.Queue()
    testing_geohashes.put(new_geohash(centroid.y, centroid.x, precision))

    while not testing_geohashes.empty():
        current_geohash = testing_geohashes.get()

        if current_geohash not in inner_geohashes and current_geohash not in outer_geohashes and current_geohash not in intersect_geohashes:
            current_polygon = geohash_to_polygon(current_geohash)

            if envelope.intersects(current_polygon):
                if polygon.contains(current_polygon):
                    inner_geohashes.add(current_geohash)
                if polygon.intersects(current_polygon):
                    intersect_geohashes.add(current_geohash)
                else:
                    outer_geohashes.add(current_geohash)

                for neighbor in geohash.neighbors(current_geohash.decode()):
                    neighbor = neighbor.encode()
                    if neighbor not in inner_geohashes and neighbor not in outer_geohashes and neighbor not in intersect_geohashes:
                        testing_geohashes.put(neighbor)

    return list(inner_geohashes), list(intersect_geohashes)


def distance_between_coordinates(lat_one: Union[float, np.ndarray], lon_one: Union[float, np.ndarray],
                                 lat_two: Union[float, np.ndarray], lon_two: Union[float, np.ndarray], ellipse_string: str = 'WGS84'):
    """
    Use the pyproj inverse transformation to determine the distance between the given point(s).  Can either be a single point,
    or an array of points

    Parameters
    ----------
    lat_one
        latitude of the initial point
    lon_one
        longitude of the initial point
    lat_two
        latitude of the terminus point
    lon_two
        longitude of the terminus point
    ellipse_string
        initialization string for the geod object

    Returns
    -------
    Union[float, np.ndarray]
        either a float or an array of floats for the distance between the point(s), in meters
    """

    g = Geod(ellps=ellipse_string)
    _, _, dist = g.inv(lon_one, lat_one, lon_two, lat_two)
    return dist


def determine_aviso_grid(longitude: float):
    """
    The Aviso module contains gridded regions where we have computed tides.  Determine which region the given longitude
    falls within.  Expects (-180 to 180) longitude.

    Parameters
    ----------
    longitude
        longitude of point

    Returns
    -------
    str
        gridded region name
    """

    # I computed these after loading Jack's EEZ dataset with numpy, transforming from 3338 to 6318
    # these are the geographic longitudinal extents in 6318
    alaska_min_x = -179.9937741
    alaska_max_x = -127.8790600
    # and these from loading the NBS NE ERTDM
    nbs_ne_min_x = -77.3833313
    nbs_ne_max_x = -62.3833313

    if len(fes_grids) > 2:
        print(f"WARNING: determine_aviso_grid: found {len(fes_grids)} grids available in aviso, expected only two, this function might need to be updated.")

    if (longitude <= alaska_max_x) and (longitude >= alaska_min_x):
        grid = 'jAcK_EEZ_ERTDM_2021'
    elif (longitude <= nbs_ne_max_x) and (longitude >= nbs_ne_min_x):
        grid = 'gERTDM_NBS_NE'
    else:  # currently there are only two grids, so this logic is simple
        raise ValueError(f'determine_aviso_grid: Unable to determine grid for longitude={longitude}.  Tried jAcK_EEZ_ERTDM_2021 ({alaska_min_x} '
                         f'to {alaska_max_x}) and gERTDM_NBS_NE ({nbs_ne_min_x} to {nbs_ne_max_x})')

    if grid not in fes_grids:
        raise ValueError(f'determine_aviso_grid: Attempting to load {grid}, but unable to find this file in the file system.')
    return grid


def aviso_tide_correct(latitudes: np.ndarray, longitudes: np.ndarray, times: np.ndarray, region: str, datum: str):
    """
    Run the aviso fes module to get tide corrections for the given positions/times.  Used for tide correcting svcorrected depths,
    where you would subtract the return from this function from your (+ DOWN) depths to get a tide corrected answer.

    Parameters
    ----------
    latitudes
        1d array of latitudes
    longitudes
        1d array of longitudes
    times
        1d array of utc timestamps in seconds
    region
        one of the fes grid names, see georeference_fes_grids
    datum
        one of the supported vertical datum descriptors in fes

    Returns
    -------
    np.ndarray
        1d array of tide corrector values for the given points/times
    """

    if not fes_found:
        raise ValueError(f'aviso_tide_correct: fes module not found.')
    if region not in fes_grids:
        raise ValueError(f'aviso_tide_correct: Attempting to load {region}, but unable to find this file in the file system.')
    if not (latitudes.size == longitudes.size == times.size):
        raise ValueError(f'aviso_tide_correct: given different length arrays, longitudes/latitudes/times must all be the same length.')

    # we have logic here to store the model as a global, so that each call doesn't have to reload the grid
    global fes_model
    global fes_model_description
    if (region != fes_model_description) or (not fes_model):
        fes_model = fes.Model(sep_region=region)
        fes_model_description = region

    dtimes = (times * 10**6).astype('datetime64[us]')
    wl_fes = fes_model.tides(longitudes, latitudes, dtimes, datum=datum)
    wl_fes = xr.DataArray(wl_fes, coords={'time': times})
    return wl_fes


def aviso_clear_model():
    """
    We cache the model in between calls to aviso_tide_correct, as the model can be quite large, and takes some time to
    load.  Use this call after your tide correct calls to clear the model and free up the memory.
    """

    global fes_model
    global fes_model_description
    fes_model = None
    fes_model_description = ''
