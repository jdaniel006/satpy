"""GMS-5 VISSR Navigation.

Reference: `GMS User Guide`_, Appendix E, S-VISSR Mapping.

.. _GMS User Guide:
    https://www.data.jma.go.jp/mscweb/en/operation/fig/GMS_Users_Guide_3rd_Edition_Rev1.pdf
"""

from collections import namedtuple

import dask.array as da
import numba
import numpy as np

from satpy.utils import get_legacy_chunk_size

CHUNK_SIZE = get_legacy_chunk_size()

EARTH_FLATTENING = 1 / 298.257
EARTH_EQUATORIAL_RADIUS = 6378136.0
EARTH_POLAR_RADIUS = EARTH_EQUATORIAL_RADIUS * (1 - EARTH_FLATTENING)
"""Constants taken from JMA's Msial library."""


Attitude = namedtuple(
    "Attitude",
    [
        "angle_between_earth_and_sun",
        "angle_between_sat_spin_and_z_axis",
        "angle_between_sat_spin_and_yz_plane",
    ],
)


Orbit = namedtuple(
    "Orbit",
    [
        "angles",
        "sat_position",
        "nutation_precession",
    ],
)
Orbit.__doc__ = """Orbital Parameters

Args:
    angles (OrbitAngles): Orbit angles
    sat_position (SatellitePositionEarthFixed): Satellite position
    nutation_precession: Nutation and precession matrix (3x3)
"""


OrbitAngles = namedtuple(
    "OrbitAngles",
    [
        "greenwich_sidereal_time",
        "declination_from_sat_to_sun",
        "right_ascension_from_sat_to_sun",
    ],
)

SatellitePositionEarthFixed = namedtuple(
    "SatellitePositionEarthFixed",
    ["x", "y", "z"],
)

ScanningParameters = namedtuple(
    "ScanningParameters",
    ["start_time_of_scan", "spinning_rate", "num_sensors", "sampling_angle"],
)

ProjectionParameters = namedtuple(
    "ProjectionParameters",
    [
        "line_offset",
        "pixel_offset",
        "stepping_angle",
        "sampling_angle",
        "misalignment",
        "earth_flattening",
        "earth_equatorial_radius",
    ],
)


_AttitudePrediction = namedtuple(
    "_AttitudePrediction",
    [
        "prediction_times",
        "angle_between_earth_and_sun",
        "angle_between_sat_spin_and_z_axis",
        "angle_between_sat_spin_and_yz_plane",
    ],
)


_OrbitPrediction = namedtuple(
    "_OrbitPrediction",
    [
        "prediction_times",
        "angles",
        "sat_position",
        "nutation_precession",
    ],
)


class AttitudePrediction:
    """Attitude prediction.

    Use .to_numba() to pass this object to jitted methods. This extra
    layer avoids usage of jitclasses and having to re-implement np.unwrap in
    numba.
    """

    def __init__(
        self,
        prediction_times,
        angle_between_earth_and_sun,
        angle_between_sat_spin_and_z_axis,
        angle_between_sat_spin_and_yz_plane,
    ):
        """Initialize attitude prediction.

        In order to accelerate interpolation, the 2-pi periodicity of angles
        is unwrapped here already (that means phase jumps greater than pi
        are wrapped to their 2*pi complement).

        Args:
            prediction_times: Timestamps of predicted attitudes
            angle_between_earth_and_sun: Angle between earth and sun
            angle_between_sat_spin_and_z_axis: Angle between satellite's
                spin-axis and the z-axis of the coordinate system
            angle_between_sat_spin_and_yz_plane: Angle between satellite's
                spin-axis and the yz-plane of the coordinate system
        """
        self.prediction_times = prediction_times
        self.angle_between_earth_and_sun = np.unwrap(angle_between_earth_and_sun)
        self.angle_between_sat_spin_and_z_axis = np.unwrap(
            angle_between_sat_spin_and_z_axis
        )
        self.angle_between_sat_spin_and_yz_plane = np.unwrap(
            angle_between_sat_spin_and_yz_plane
        )

    def to_numba(self):
        """Convert to numba-compatible type."""
        return _AttitudePrediction(
            prediction_times=self.prediction_times,
            angle_between_earth_and_sun=self.angle_between_earth_and_sun,
            angle_between_sat_spin_and_z_axis=self.angle_between_sat_spin_and_z_axis,
            angle_between_sat_spin_and_yz_plane=self.angle_between_sat_spin_and_yz_plane,
        )


class OrbitPrediction:
    """Orbit prediction.

    Use .to_numba() to pass this object to jitted methods. This extra
    layer avoids usage of jitclasses and having to re-implement np.unwrap in
    numba.
    """

    def __init__(
        self,
        prediction_times,
        angles,
        sat_position,
        nutation_precession,
    ):
        """Initialize orbit prediction.

        In order to accelerate interpolation, the 2-pi periodicity of angles
        is unwrapped here already (that means phase jumps greater than pi
        are wrapped to their 2*pi complement).

        Args:
            prediction_times: Timestamps of orbit prediction.
            angles (OrbitAngles): Orbit angles
            sat_position (SatellitePositionEarthFixed): Satellite position
            nutation_precession: Nutation and precession matrix.
        """
        self.prediction_times = prediction_times
        self.angles = self._unwrap_angles(angles)
        self.sat_position = sat_position
        self.nutation_precession = nutation_precession

    def _unwrap_angles(self, angles):
        return OrbitAngles(
            greenwich_sidereal_time=np.unwrap(angles.greenwich_sidereal_time),
            declination_from_sat_to_sun=np.unwrap(angles.declination_from_sat_to_sun),
            right_ascension_from_sat_to_sun=np.unwrap(
                angles.right_ascension_from_sat_to_sun
            ),
        )

    def to_numba(self):
        """Convert to numba-compatible type."""
        return _OrbitPrediction(
            prediction_times=self.prediction_times,
            angles=self.angles,
            sat_position=self.sat_position,
            nutation_precession=self.nutation_precession,
        )


def get_lons_lats(lines, pixels, static_params, predicted_params):
    """Compute lon/lat coordinates given VISSR image coordinates.

    Args:
        lines: VISSR image lines
        pixels: VISSR image pixels
        static_params: Static navigation parameters
        predicted_params: Predicted time-dependent navigation parameters
    """
    pixels_2d, lines_2d = da.meshgrid(pixels, lines)
    lons, lats = da.map_blocks(
        _get_lons_lats_numba,
        lines_2d,
        pixels_2d,
        static_params=static_params,
        predicted_params=_make_predicted_params_numba_compatible(predicted_params),
        **_get_map_blocks_kwargs(pixels_2d.chunks)
    )
    return lons, lats


def _make_predicted_params_numba_compatible(predicted_params):
    att_pred, orb_pred = predicted_params
    return att_pred.to_numba(), orb_pred.to_numba()


def _get_map_blocks_kwargs(chunks):
    # Get keyword arguments for da.map_blocks, so that it can be used
    # with a function that returns two arguments.
    return {
        "new_axis": 0,
        "chunks": (2,) + chunks,
        "dtype": np.float32,
    }


@numba.njit
def _get_lons_lats_numba(lines_2d, pixels_2d, static_params, predicted_params):
    shape = lines_2d.shape
    lons = np.zeros(shape, dtype=np.float32)
    lats = np.zeros(shape, dtype=np.float32)
    for i in range(shape[0]):
        for j in range(shape[1]):
            point = (lines_2d[i, j], pixels_2d[i, j])
            nav_params = _get_navigation_parameters(
                point, static_params, predicted_params
            )
            lon, lat = get_lon_lat(point, nav_params)
            lons[i, j] = lon
            lats[i, j] = lat
    # Stack lons and lats because da.map_blocks doesn't support multiple
    # return values.
    return np.stack((lons, lats))


@numba.njit
def _get_navigation_parameters(point, static_params, predicted_params):
    scan_params, proj_params = static_params
    attitude_prediction, orbit_prediction = predicted_params
    obs_time = get_observation_time(point, scan_params)
    attitude, orbit = interpolate_navigation_prediction(
        attitude_prediction, orbit_prediction, obs_time
    )
    return attitude, orbit, proj_params


@numba.njit
def get_observation_time(point, scan_params):
    """Calculate observation time of a VISSR pixel."""
    relative_time = _get_relative_observation_time(point, scan_params)
    return scan_params.start_time_of_scan + relative_time


@numba.njit
def _get_relative_observation_time(point, scan_params):
    line, pixel = point
    pixel = pixel + 1
    line = line + 1
    spinning_freq = 1440 * scan_params.spinning_rate
    line_step = np.floor((line - 1) / scan_params.num_sensors)
    pixel_step = (scan_params.sampling_angle * pixel) / (2 * np.pi)
    return (line_step + pixel_step) / spinning_freq


@numba.njit
def interpolate_navigation_prediction(
    attitude_prediction, orbit_prediction, observation_time
):
    """Interpolate predicted navigation parameters."""
    attitude = interpolate_attitude_prediction(attitude_prediction, observation_time)
    orbit = interpolate_orbit_prediction(orbit_prediction, observation_time)
    return attitude, orbit


@numba.njit
def get_lon_lat(point, nav_params):
    """Get longitude and latitude coordinates for a given image pixel.

    Args:
        point: Point (line, pixel) in image coordinates.
        nav_params: Navigation parameters (Attitude, Orbit, Projection
            Parameters)
    Returns:
        Longitude and latitude in degrees.
    """
    attitude, orbit, proj_params = nav_params
    scan_angles = transform_image_coords_to_scanning_angles(
        point, _get_image_offset(proj_params), _get_sampling(proj_params)
    )
    view_vector_sat = transform_scanning_angles_to_satellite_coords(
        scan_angles, proj_params.misalignment
    )
    view_vector_earth_fixed = transform_satellite_to_earth_fixed_coords(
        view_vector_sat,
        orbit,
        attitude.angle_between_earth_and_sun,
        _get_spin_angles(attitude),
    )
    point_on_earth = intersect_with_earth(
        view_vector_earth_fixed, _get_sat_pos(orbit), _get_ellipsoid(proj_params)
    )
    lon, lat = transform_earth_fixed_to_geodetic_coords(
        point_on_earth, proj_params.earth_flattening
    )
    return lon, lat


@numba.njit
def _get_image_offset(proj_params):
    return proj_params.line_offset, proj_params.pixel_offset


@numba.njit
def _get_sampling(proj_params):
    return proj_params.stepping_angle, proj_params.sampling_angle


@numba.njit
def _get_spin_angles(attitude):
    return (
        attitude.angle_between_sat_spin_and_z_axis,
        attitude.angle_between_sat_spin_and_yz_plane,
    )


@numba.njit
def _get_sat_pos(orbit):
    return np.array(
        (
            orbit.sat_position.x,
            orbit.sat_position.y,
            orbit.sat_position.z,
        )
    )


@numba.njit
def _get_ellipsoid(proj_params):
    return (proj_params.earth_equatorial_radius, proj_params.earth_flattening)


@numba.njit
def transform_image_coords_to_scanning_angles(point, offset, sampling):
    """Transform image coordinates to scanning angles.

    Args:
        point: Point (line, pixel) in image coordinates.
        offset: Offset (line, pixel) from image center.
        sampling: Stepping angle (along line) and sampling angle (along pixels)
            in radians.
    Returns:
        Scanning angles (x, y) at the pixel center (rad).
    """
    line, pixel = point
    line_offset, pixel_offset = offset
    stepping_angle, sampling_angle = sampling
    x = sampling_angle * (pixel + 1 - pixel_offset)
    y = stepping_angle * (line + 1 - line_offset)
    return np.array([x, y])


@numba.njit
def transform_scanning_angles_to_satellite_coords(angles, misalignment):
    """Transform scanning angles to satellite angular momentum coordinates.

    Args:
        angles: Scanning angles (x, y) in radians.
        misalignment: Misalignment matrix (3x3)

    Returns:
        View vector (x, y, z) in satellite angular momentum coordinates.
    """
    rotation, vector = _get_transforms_from_scanning_angles_to_satellite_coords(angles)
    return np.dot(rotation, np.dot(misalignment, vector))


@numba.njit
def _get_transforms_from_scanning_angles_to_satellite_coords(angles):
    x, y = angles
    cos_x = np.cos(x)
    sin_x = np.sin(x)
    rot = np.array(((cos_x, -sin_x, 0), (sin_x, cos_x, 0), (0, 0, 1)))
    vec = np.array([np.cos(y), 0, np.sin(y)])
    return rot, vec


@numba.njit
def transform_satellite_to_earth_fixed_coords(
    point,
    orbit,
    earth_sun_angle,
    spin_angles,
):
    """Transform from earth-fixed to satellite angular momentum coordinates.

    Args:
        point: Point (x, y, z) in satellite angular momentum coordinates.
        orbit (Orbit): Orbital parameters
        earth_sun_angle: Angle between sun and earth center on the z-axis
            vertical plane (rad)
        spin_angles: Angle between satellite spin axis and z-axis (rad),
            angle between satellite spin axis and yz-plane
    Returns:
        Point (x', y', z') in earth-fixed coordinates.
    """
    sat_unit_vectors = _get_satellite_unit_vectors(
        orbit,
        earth_sun_angle,
        spin_angles,
    )
    return np.dot(sat_unit_vectors, point)


@numba.njit
def _get_satellite_unit_vectors(
    orbit,
    earth_sun_angle,
    spin_angles,
):
    unit_vector_z = _get_satellite_unit_vector_z(
        spin_angles, orbit.angles.greenwich_sidereal_time, orbit.nutation_precession
    )
    unit_vector_x = _get_satellite_unit_vector_x(
        earth_sun_angle, orbit.angles, unit_vector_z
    )
    unit_vector_y = _get_satellite_unit_vector_y(unit_vector_x, unit_vector_z)
    return np.stack((unit_vector_x, unit_vector_y, unit_vector_z), axis=-1)


@numba.njit
def _get_satellite_unit_vector_z(
    spin_angles, greenwich_sidereal_time, nutation_precession
):
    sat_z_axis_1950 = _get_satellite_z_axis_1950(spin_angles)
    rotation = _get_transform_from_1950_to_earth_fixed(greenwich_sidereal_time)
    z_vec = np.dot(rotation, np.dot(nutation_precession, sat_z_axis_1950))
    return normalize_vector(z_vec)


@numba.njit
def _get_satellite_z_axis_1950(spin_angles):
    """Get satellite z-axis (spin) in mean of 1950 coordinates."""
    alpha, delta = spin_angles
    cos_delta = np.cos(delta)
    x = np.sin(delta)
    y = -cos_delta * np.sin(alpha)
    z = cos_delta * np.cos(alpha)
    return np.array([x, y, z])


@numba.njit
def _get_transform_from_1950_to_earth_fixed(greenwich_sidereal_time):
    cos = np.cos(greenwich_sidereal_time)
    sin = np.sin(greenwich_sidereal_time)
    return np.array(((cos, sin, 0), (-sin, cos, 0), (0, 0, 1)))


@numba.njit
def _get_satellite_unit_vector_x(earth_sun_angle, orbit_angles, sat_unit_vector_z):
    beta = earth_sun_angle
    sat_sun_vector = _get_vector_from_satellite_to_sun(orbit_angles)
    z_cross_satsun = np.cross(sat_unit_vector_z, sat_sun_vector)
    z_cross_satsun = normalize_vector(z_cross_satsun)
    x_vec = z_cross_satsun * np.sin(beta) + np.cross(
        z_cross_satsun, sat_unit_vector_z
    ) * np.cos(beta)
    return normalize_vector(x_vec)


@numba.njit
def _get_vector_from_satellite_to_sun(orbit_angles):
    declination = orbit_angles.declination_from_sat_to_sun
    right_ascension = orbit_angles.right_ascension_from_sat_to_sun
    cos_declination = np.cos(declination)
    x = cos_declination * np.cos(right_ascension)
    y = cos_declination * np.sin(right_ascension)
    z = np.sin(declination)
    return np.array([x, y, z])


@numba.njit
def _get_satellite_unit_vector_y(sat_unit_vector_x, sat_unit_vector_z):
    y_vec = np.cross(sat_unit_vector_z, sat_unit_vector_x)
    return normalize_vector(y_vec)


@numba.njit
def intersect_with_earth(view_vector, sat_pos, ellipsoid):
    """Intersect instrument viewing vector with the earth's surface.

    Args:
        view_vector: Instrument viewing vector (x, y, z) in earth-fixed
            coordinates.
        sat_pos: Satellite position (x, y, z) in earth-fixed coordinates.
        ellipsoid: Flattening and equatorial radius of the earth.
    Returns:
        Intersection (x', y', z') with the earth's surface.
    """
    distance = _get_distance_to_intersection(view_vector, sat_pos, ellipsoid)
    return sat_pos + distance * view_vector


@numba.njit
def _get_distance_to_intersection(view_vector, sat_pos, ellipsoid):
    """Get distance to intersection with the earth.

    If the instrument is pointing towards the earth, there will be two
    intersections with the surface. Choose the one on the instrument-facing
    side of the earth.
    """
    d1, d2 = _get_distances_to_intersections(view_vector, sat_pos, ellipsoid)
    return min(d1, d2)


@numba.njit
def _get_distances_to_intersections(view_vector, sat_pos, ellipsoid):
    equatorial_radius, flattening = ellipsoid
    flat2 = (1 - flattening) ** 2
    ux, uy, uz = view_vector
    x, y, z = sat_pos

    a = flat2 * (ux**2 + uy**2) + uz**2
    b = flat2 * (x * ux + y * uy) + z * uz
    c = flat2 * (x**2 + y**2 - equatorial_radius**2) + z**2

    tmp = np.sqrt((b**2 - a * c))
    dist_1 = (-b + tmp) / a
    dist_2 = (-b - tmp) / a
    return dist_1, dist_2


@numba.njit
def transform_earth_fixed_to_geodetic_coords(point, earth_flattening):
    """Transform from earth-fixed to geodetic coordinates.

    Args:
        point: Point (x, y, z) in earth-fixed coordinates.
        earth_flattening: Flattening of the earth.

    Returns:
        Geodetic longitude and latitude (degrees).
    """
    x, y, z = point
    f = earth_flattening
    lon = np.arctan2(y, x)
    lat = np.arctan2(z, ((1 - f) ** 2 * np.sqrt(x**2 + y**2)))
    return np.rad2deg(lon), np.rad2deg(lat)


@numba.njit
def normalize_vector(v):
    """Normalize the given vector."""
    return v / np.sqrt(np.dot(v, v))


@numba.njit
def interpolate_orbit_prediction(orbit_prediction, observation_time):
    """Interpolate orbit prediction."""
    angles = _interpolate_orbit_angles(observation_time, orbit_prediction)
    sat_position = _interpolate_sat_position(observation_time, orbit_prediction)
    nutation_precession = interpolate_nearest(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.nutation_precession,
    )
    return Orbit(
        angles=angles,
        sat_position=sat_position,
        nutation_precession=nutation_precession,
    )


@numba.njit
def _interpolate_orbit_angles(observation_time, orbit_prediction):
    sidereal_time = interpolate_angles(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.angles.greenwich_sidereal_time,
    )
    declination = interpolate_angles(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.angles.declination_from_sat_to_sun,
    )
    right_ascension = interpolate_angles(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.angles.right_ascension_from_sat_to_sun,
    )
    return OrbitAngles(
        greenwich_sidereal_time=sidereal_time,
        declination_from_sat_to_sun=declination,
        right_ascension_from_sat_to_sun=right_ascension,
    )


@numba.njit
def _interpolate_sat_position(observation_time, orbit_prediction):
    x = interpolate_continuous(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.sat_position.x,
    )
    y = interpolate_continuous(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.sat_position.y,
    )
    z = interpolate_continuous(
        observation_time,
        orbit_prediction.prediction_times,
        orbit_prediction.sat_position.z,
    )
    return SatellitePositionEarthFixed(x, y, z)


@numba.njit
def interpolate_attitude_prediction(attitude_prediction, observation_time):
    """Interpolate attitude prediction."""
    angle_between_earth_and_sun = interpolate_angles(
        observation_time,
        attitude_prediction.prediction_times,
        attitude_prediction.angle_between_earth_and_sun,
    )
    angle_between_sat_spin_and_z_axis = interpolate_angles(
        observation_time,
        attitude_prediction.prediction_times,
        attitude_prediction.angle_between_sat_spin_and_z_axis,
    )
    angle_between_sat_spin_and_yz_plane = interpolate_angles(
        observation_time,
        attitude_prediction.prediction_times,
        attitude_prediction.angle_between_sat_spin_and_yz_plane,
    )
    return Attitude(
        angle_between_earth_and_sun,
        angle_between_sat_spin_and_z_axis,
        angle_between_sat_spin_and_yz_plane,
    )


@numba.njit
def interpolate_continuous(x, x_sample, y_sample):
    """Linear interpolation of continuous quantities.

    Numpy equivalent would be np.interp(..., left=np.nan, right=np.nan), but
    numba currently doesn't support those keyword arguments.
    """
    try:
        return _interpolate(x, x_sample, y_sample)
    except Exception:
        # Numba cannot distinguish exception types
        return np.nan


@numba.njit
def _interpolate(x, x_sample, y_sample):
    i = _find_enclosing_index(x, x_sample)
    offset = y_sample[i]
    x_diff = x_sample[i + 1] - x_sample[i]
    y_diff = y_sample[i + 1] - y_sample[i]
    slope = y_diff / x_diff
    dist = x - x_sample[i]
    return offset + slope * dist


@numba.njit
def _find_enclosing_index(x, x_sample):
    """Find where x_sample encloses x."""
    for i in range(len(x_sample) - 1):
        if x_sample[i] <= x < x_sample[i + 1]:
            return i
    raise Exception("x not enclosed by x_sample")


@numba.njit
def interpolate_angles(x, x_sample, y_sample):
    """Linear interpolation of angles.

    Requires 2-pi periodicity to be unwrapped before (for
    performance reasons). Interpolated angles are wrapped
    back to [-pi, pi] to restore periodicity.
    """
    return _wrap_2pi(interpolate_continuous(x, x_sample, y_sample))


@numba.njit
def _wrap_2pi(values):
    """Wrap values to interval [-pi, pi].

    Source: https://stackoverflow.com/a/15927914/5703449
    """
    return (values + np.pi) % (2 * np.pi) - np.pi


@numba.njit
def interpolate_nearest(x, x_sample, y_sample):
    """Nearest neighbour interpolation."""
    try:
        return _interpolate_nearest(x, x_sample, y_sample)
    except Exception:
        return np.nan * np.ones_like(y_sample[0])


@numba.njit
def _interpolate_nearest(x, x_sample, y_sample):
    i = _find_enclosing_index(x, x_sample)
    return y_sample[i]
