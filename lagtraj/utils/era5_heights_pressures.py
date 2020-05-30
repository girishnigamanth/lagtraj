"""
ERA5 utilities that can
- Add heights and pressures to an input data array on model levels
- Interpolate from model levels to constant height levels (using Steffen interpolation)
- Calculate gradients using boundary values or regression method
- Extract local profiles and mean profiles
- Subselect a domain
- Filter/mask data: e.g. "ocean values only"
- Add auxiliary variables

TODO
- Move some functionality (e.g. auxiliary variables) to more generic utilities
- May want to write/use some code for degrees versus radians
- Discuss need to check float vs double
- Discuss data filter and weight procedures
- Compare regression and boundary gradients
- Look into other mean/gradient techniques (e.g. Gaussian weighted, see CSET code).
- Further documentation
- Check cf conventions
"""

import os
import numbers
import datetime
import numpy as np
import pandas as pd
import xarray as xr

# Optional numba dependency
try:
    from numba import njit

    print("Running with numba")
except ImportError:

    def njit(numba_function):
        """Dummy numba function"""
        return numba_function

    print("Running without numba")


# ECMWF CONSTANTS
rd = 287.06
rg = 9.80665
rv_over_rd_minus_one = 0.609133

# OTHER CONSTANTS NEED TO GO ELSEWHERE EVENTUALLY?
p_ref = 1.0e5
cp = 1004.0
rd_over_cp = rd / cp
p_ref_inv = 1.0 / p_ref
r_earth = 6371000.0
pi = np.pi

levels_file = os.path.dirname(__file__) + "/137levels.dat"
levels_table = pd.read_table(levels_file, sep="\s+")
# skip the first row, as it corresponds to the top of the atmosphere
# which is not in the data
a_coeffs_137 = levels_table["a[Pa]"].values[1:]
b_coeffs_137 = levels_table["b"].values[1:]


@njit
def calculate_heights_and_pressures(
    p_surf, height_surf, a_coeffs, b_coeffs, t_field, q_field
):
    """Calculate heights and pressures at model levels using
    the hydrostatic equation (not taking into account hydrometeors).
    """
    k_max = t_field.shape[0]
    j_max = t_field.shape[1]
    i_max = t_field.shape[2]
    height_h = np.empty((k_max, j_max, i_max))
    height_f = np.empty((k_max, j_max, i_max))
    p_h = np.empty((k_max, j_max, i_max))
    p_f = np.empty((k_max, j_max, i_max))
    rd_over_rg = rd / rg
    for i in range(i_max):
        for j in range(j_max):
            p_s = p_surf[j, i]
            p_h[k_max - 1, j, i] = p_s
            p_h_k_plus = p_s
            z_s = height_surf[j, i]
            height_h[k_max - 1, j, i] = z_s
            height_h_k_plus = z_s
            for k in range(k_max - 2, -1, -1):
                # Pressure at this half level
                p_h_k = a_coeffs[k] + (b_coeffs[k] * p_s)
                p_h[k, j, i] = p_h_k
                # Pressure at corresponding full level
                p_f_k_plus = 0.5 * (p_h_k + p_h_k_plus)
                p_f[k + 1, j, i] = p_f_k_plus
                # Virtual temperature
                tvrd_over_rg = (
                    rd_over_rg
                    * t_field[k, j, i]
                    * (1.0 + rv_over_rd_minus_one * q_field[k, j, i])
                )
                # Integration to half level first
                height_f[k + 1, j, i] = height_h_k_plus + (
                    tvrd_over_rg * np.log(p_h_k_plus / p_f_k_plus)
                )
                # Integration to full levels
                # reset of scalar temporary variables
                height_h_k_plus = height_h_k_plus + (
                    tvrd_over_rg * np.log(p_h_k_plus / p_h_k)
                )
                height_h[k, j, i] = height_h_k_plus
                p_h_k_plus = p_h_k
            p_f_k_plus = 0.5 * p_h_k_plus
            p_f[0, j, i] = p_f_k_plus
            height_f[0, j, i] = height_h_k_plus + (
                tvrd_over_rg * np.log(p_h_k_plus / p_f_k_plus)
            )

    return height_h, height_f, p_h, p_f


@njit
def steffen_3d(
    input_data,
    input_levels,
    output_level_array,
    lower_extrapolation_surface,
    lower_extrapolation_with_gradient=False,
):
    """
    Performs Steffen interpolation on each individual column.
    Steffen, M. (1990). A simple method for monotonic interpolation
    in one dimension. Astronomy and Astrophysics, 239, 443.
    """
    k_max = input_data.shape[0]
    j_max = input_data.shape[1]
    i_max = input_data.shape[2]
    k_max_output = output_level_array.shape[0]
    k_max_minus = k_max - 1
    linear_slope = np.empty((k_max, j_max, i_max))
    output_data = np.empty((k_max_output, j_max, i_max))
    for i in range(i_max):
        for j in range(j_max):
            # first point
            delta_lower = input_levels[1, j, i] - input_levels[0, j, i]
            delta_upper = input_levels[2, j, i] - input_levels[1, j, i]
            if delta_lower < 0:
                raise Exception("Non-montonic increase in input_levels")
            if delta_upper < 0:
                raise Exception("Non-montonic increase in input_levels")
            slope_lower = (input_data[1, j, i] - input_data[0, j, i]) / delta_lower
            slope_upper = (input_data[2, j, i] - input_data[1, j, i]) / delta_upper

            weighted_slope = slope_lower * (
                1 + delta_lower / (delta_lower + delta_upper)
            ) - slope_upper * delta_lower / (delta_lower + delta_upper)
            if weighted_slope * slope_lower <= 0.0:
                linear_slope[0, j, i] = 0.0
            elif np.abs(weighted_slope) > 2 * np.abs(slope_lower):
                linear_slope[0, j, i] = 2.0 * slope_lower
            else:
                linear_slope[0, j, i] = weighted_slope

            # intermediate points
            for k in range(1, k_max_minus):
                delta_lower = input_levels[k, j, i] - input_levels[k - 1, j, i]
                delta_upper = input_levels[k + 1, j, i] - input_levels[k, j, i]
                slope_lower = (
                    input_data[k, j, i] - input_data[k - 1, j, i]
                ) / delta_lower
                slope_upper = (
                    input_data[k + 1, j, i] - input_data[k, j, i]
                ) / delta_upper
                weighted_slope = (
                    slope_lower * delta_upper + slope_upper * delta_lower
                ) / (delta_lower + delta_upper)

                if slope_lower * slope_upper <= 0.0:
                    linear_slope[k, j, i] = 0.0
                elif np.abs(weighted_slope) > 2.0 * np.abs(slope_lower):
                    linear_slope[k, j, i] = np.copysign(2.0, slope_lower) * min(
                        np.abs(slope_lower), np.abs(slope_upper)
                    )
                elif np.abs(weighted_slope) > 2.0 * np.abs(slope_upper):
                    linear_slope[k, j, i] = np.copysign(2.0, slope_lower) * min(
                        np.abs(slope_lower), np.abs(slope_upper)
                    )
                else:
                    linear_slope[k, j, i] = weighted_slope

            # last point
            delta_lower = (
                input_levels[k_max_minus - 1, j, i]
                - input_levels[k_max_minus - 2, j, i]
            )
            delta_upper = (
                input_levels[k_max_minus, j, i] - input_levels[k_max_minus - 1, j, i]
            )
            slope_lower = (
                input_data[k_max_minus - 1, j, i] - input_data[k_max_minus - 2, j, i]
            ) / delta_lower
            slope_upper = (
                input_data[k_max_minus, j, i] - input_data[k_max_minus - 1, j, i]
            ) / delta_upper
            weighted_slope = slope_upper * (
                1 + delta_upper / (delta_upper + delta_lower)
            ) - slope_lower * delta_upper / (delta_upper + delta_lower)
            if weighted_slope * slope_upper <= 0.0:
                linear_slope[k_max_minus, j, i] = 0.0
            elif np.abs(weighted_slope) > 2.0 * np.abs(slope_upper):
                linear_slope[k_max_minus, j, i] = 2.0 * slope_upper
            else:
                linear_slope[k_max_minus, j, i] = weighted_slope

            # loop over output points
            k_temp = 0
            for k_out in range(k_max_output):
                while (k_temp < k_max) and (
                    input_levels[k_temp, j, i] < output_level_array[k_out]
                ):
                    k_temp = k_temp + 1
                if k_temp > 0 and k_temp < k_max:
                    k_high = k_temp
                    k_low = k_high - 1
                    delta = input_levels[k_high, j, i] - input_levels[k_low, j, i]
                    slope = (input_data[k_high, j, i] - input_data[k_low, j, i]) / delta
                    a = (
                        linear_slope[k_low, j, i]
                        + linear_slope[k_high, j, i]
                        - 2 * slope
                    ) / (delta * delta)
                    b = (
                        3 * slope
                        - 2 * linear_slope[k_low, j, i]
                        - linear_slope[k_high, j, i]
                    ) / delta
                    c = linear_slope[k_low, j, i]
                    d = input_data[k_low, j, i]
                    t = output_level_array[k_out] - input_levels[k_low, j, i]
                    t_2 = t * t
                    t_3 = t_2 * t
                    output_data[k_out, j, i] = a * t_3 + b * t_2 + c * t + d
                elif (k_temp == 0) and (
                    output_level_array[k_out] >= lower_extrapolation_surface[j, i]
                ):
                    if lower_extrapolation_with_gradient:
                        output_data[k_out, j, i] = input_data[0, j, i] + linear_slope[
                            0, j, i
                        ] * (output_level_array[k_out] - input_levels[0, j, i])
                    else:
                        output_data[k_out, j, i] = input_data[0, j, i]
                else:
                    output_data[k_out, j, i] = np.nan
    return output_data


def add_heights_and_pressures(ds_from_era5):
    """Adds height and pressure fields to ERA5 model level data arrays"""
    len_temp = len(ds_from_era5["t"])
    shape_temp = np.shape(ds_from_era5["t"])
    ds_from_era5["height_h"] = (
        ("time", "level", "latitude", "longitude"),
        np.empty(shape_temp),
        {"long_name": "height above sea level at half level", "units": "metres"},
    )
    ds_from_era5["height_f"] = (
        ("time", "level", "latitude", "longitude"),
        np.empty(shape_temp),
        {"long_name": "height above sea level at full level", "units": "metres"},
    )
    ds_from_era5["p_h"] = (
        ("time", "level", "latitude", "longitude"),
        np.empty(shape_temp),
        {"long_name": "pressure at half level", "units": "Pa"},
    )
    ds_from_era5["p_f"] = (
        ("time", "level", "latitude", "longitude"),
        np.empty(shape_temp),
        {"long_name": "pressure at full level", "units": "Pa"},
    )
    for time_index in range(len_temp):
        p_surf = ds_from_era5["sp"].values[time_index, :, :]
        # Convert from geopotential to height
        height_surf = ds_from_era5["z"].values[time_index, :, :] / rg
        t_field = ds_from_era5["t"].values[time_index, :, :, :]
        q_field = ds_from_era5["q"].values[time_index, :, :, :]

        height_h, height_f, p_h, p_f = calculate_heights_and_pressures(
            p_surf, height_surf, a_coeffs_137, b_coeffs_137, t_field, q_field,
        )
        ds_from_era5["height_h"][time_index] = height_h
        ds_from_era5["height_f"][time_index] = height_f
        ds_from_era5["p_h"][time_index] = p_h
        ds_from_era5["p_f"][time_index] = p_f
    return ds_from_era5


def era5_on_height_levels(ds_pressure_levels, heights_array):
    """Converts ERA5 model level data to data on height levels
    using Steffen interpolation"""
    if isinstance(heights_array[0], numbers.Integral):
        raise Exception("Heights need to be floating numbers, rather than integers")
    heights_coord = {
        "lev": ("lev", heights_array, {"long_name": "altitude", "units": "metres"},)
    }
    ds_height_levels = xr.Dataset(
        coords={
            "time": ds_pressure_levels.time,
            **heights_coord,
            "latitude": ds_pressure_levels.latitude,
            "longitude": ds_pressure_levels.longitude,
        }
    )
    time_steps = len(ds_pressure_levels["height_f"])
    shape_p_levels = np.shape(ds_pressure_levels["height_f"])
    shape_h_levels = (shape_p_levels[0],) + (len(heights_array),) + shape_p_levels[2:]
    for variable in ds_pressure_levels.variables:
        if ds_pressure_levels[variable].dims == (
            "time",
            "level",
            "latitude",
            "longitude",
        ):
            ds_height_levels[variable] = (
                ("time", "lev", "latitude", "longitude"),
                np.empty(shape_h_levels),
                ds_pressure_levels[variable].attrs,
            )
        elif "level" not in ds_pressure_levels[variable].dims:
            ds_height_levels[variable] = (
                ds_pressure_levels[variable].dims,
                ds_pressure_levels[variable],
                ds_pressure_levels[variable].attrs,
            )
    for time_index in range(time_steps):
        h_f_inverse = ds_pressure_levels["height_f"][time_index, ::-1, :, :].values
        h_h_inverse = ds_pressure_levels["height_h"][time_index, ::-1, :, :].values
        sea_mask = (
            (ds_pressure_levels["height_h"][time_index, -1, :, :].values < 5.0)
            * (ds_pressure_levels["height_h"][time_index, -1, :, :].values > 1.0e-6)
            * (ds_pressure_levels["lsm"][time_index, :, :].values < 0.2)
        )
        lower_extrapolation_with_mask = xr.where(
            sea_mask, -1.0e-6, ds_pressure_levels["height_h"][time_index, -1, :, :]
        ).values
        for variable in ds_pressure_levels.variables:
            if np.shape(ds_pressure_levels[variable]) == shape_p_levels:
                if variable in ["height_h", "p_h"]:
                    h_inverse = h_h_inverse
                else:
                    h_inverse = h_f_inverse
                field_p_levels = ds_pressure_levels[variable][
                    time_index, ::-1, :, :
                ].values
                if variable in ["p_h", "p_f", "height_h", "height_f"]:
                    ds_height_levels[variable][time_index] = steffen_3d(
                        field_p_levels,
                        h_inverse,
                        heights_array,
                        lower_extrapolation_with_mask,
                        lower_extrapolation_with_gradient=True,
                    )
                else:
                    ds_height_levels[variable][time_index] = steffen_3d(
                        field_p_levels,
                        h_inverse,
                        heights_array,
                        lower_extrapolation_with_mask,
                    )
    return ds_height_levels


def add_auxiliary_variable(ds_level_2, var):
    """Adds auxiliary variables to arrays.
    Alternatively, the equations could be separated out to another utility
    I think this may be adding a 'black box layer' though
    To be discussed"""
    if var == "theta":
        attr_dict = {"units": "K", "long_name": "potential temperature"}
        ds_level_2[var] = (
            ds_level_2["t"] * (ds_level_2["p_f"] * p_ref_inv) ** rd_over_cp
        )
    else:
        raise NotImplementedError("Variable not implemented")
    ds_level_2[var] = ds_level_2[var].assign_attrs(**attr_dict)
    return ds_level_2


def add_auxiliary_variables(ds_level_1, list_of_vars):
    """Wrapper for auxiliary variable calculation"""
    for var in list_of_vars:
        add_auxiliary_variable(ds_level_1, var)
    return ds_level_1


def era_5_normalise_longitude(ds_to_normalise):
    """Normalise longitudes to be between 0 and 360 degrees
    This is needed because these are stored differently in the surface
    and model level data. Rounding up to 4 decimals seems to work for now,
    with more decimals misalignment has happenend. Would be good to sort
    out why this is the case.
    """
    ds_to_normalise.coords["longitude"] = (
        "longitude",
        np.round(ds_to_normalise.coords["longitude"] % 360.0, decimals=4),
        ds_to_normalise.coords["longitude"].attrs,
    )
    return ds_to_normalise


def era_5_subset(ds_full, dictionary):
    """Utility to select era5 data by latitude and longitude
    Note: data order is North to South"""
    ds_subset = ds_full.sel(
        latitude=slice(dictionary["lat_max"], dictionary["lat_min"]),
        longitude=slice(dictionary["lon_min"] % 360, dictionary["lon_max"] % 360),
    )
    return ds_subset


def era5_single_point(ds_domain, dictionary):
    """Extracts a local profile at the nearest point"""
    ds_at_location = ds_domain.sel(
        latitude=dictionary["lat"], longitude=dictionary["lon"] % 360, method="nearest"
    )
    return ds_at_location


def era5_interp_column(ds_domain, lat_to_interp, lon_to_interp):
    """Returns the dataset interpolated to given latitude and longitude
    with latitude and longitude dimensions retained"""
    ds_at_location = ds_domain.interp(
        latitude=[lat_to_interp], longitude=[lon_to_interp % 360]
    )
    return ds_at_location


def era5_mask(ds_to_mask, dictionary):
    """Returns a lat-lon mask"""
    # Only use ocean points, ensure it can be used after before or after array extensions
    if dictionary["mask"] == "ocean":
        mask = (ds_to_mask["z"] < 5.0 * rg) * (ds_to_mask["lsm"].values < 0.2)
    return mask


def era5_weighted(ds_to_weigh, dictionary):
    """Adds weights to dictionary"""
    if "weights" in dictionary:
        if dictionary["weights"] == "area":
            ds_to_weigh.weigths = np.cos(np.deg2rad(ds_to_weigh.lat_meshgrid))
        else:
            raise Exception("weight strategy not implemented")
    return ds_to_weigh


def era5_box_mean(ds_box, dictionary):
    """
    Calculates mean over a data_set.
    - Only use columns where the first level is higher than the local first level
    in the location of interest
    - Option to weight by box size?
    """
    era5_weighted(ds_box, dictionary)
    if "mask" in dictionary:
        mask = era5_mask(ds_box, dictionary)
        ds_mean = ds_box.where(mask).mean(("latitude", "longitude"), keep_attrs=True)
    else:
        ds_mean = ds_box.mean(("latitude", "longitude"), keep_attrs=True)
    return ds_mean


def era5_add_lat_lon_meshgrid(ds_to_extend):
    """Adds a [lat, lon] meshgrid to a dataset, useful for gradients in order to work
    around nan values on edge"""
    lon_mesh, lat_mesh = np.meshgrid(ds_to_extend.longitude, ds_to_extend.latitude)
    ds_to_extend["lon_meshgrid"] = (
        ("latitude", "longitude"),
        lon_mesh,
        {
            "long_name": "longitude on meshgrid",
            "units": ds_to_extend["longitude"].units,
        },
    )
    ds_to_extend["lat_meshgrid"] = (
        ("latitude", "longitude"),
        lat_mesh,
        {"long_name": "latitude on meshgrid", "units": ds_to_extend["latitude"].units},
    )
    return ds_to_extend


def calc_haver_dist(lat1, lon1, lat2, lon2):
    """Calculates distance given pairs of latitude and longitude in radians"""
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    haver = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    arc_dist = 2 * np.arctan2(np.sqrt(haver), np.sqrt(1.0 - haver))
    haver_dist = r_earth * arc_dist
    return haver_dist


def calc_lat_lon_angle(lat1, lon1, lat2, lon2):
    """Calculates angle given pairs of latitude and longitude in radians"""
    dlon = lon2 - lon1
    lat_lon_angle = np.arctan2(
        np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon),
        np.sin(dlon) * np.cos(lat2),
    )
    return lat_lon_angle


def dist_from_meshgrids(ds_1, ds_2):
    """Calculates distances between two datasets of the same shape"""
    # convert to degrees
    lat1_mg = ds_1["lat_meshgrid"].values * (2 * pi / 360)
    lon1_mg = ds_1["lon_meshgrid"].values * (2 * pi / 360)
    lat2_mg = ds_2["lat_meshgrid"].values * (2 * pi / 360)
    lon2_mg = ds_2["lon_meshgrid"].values * (2 * pi / 360)
    dist_from_mg = calc_haver_dist(lat1_mg, lon1_mg, lat2_mg, lon2_mg)
    return dist_from_mg.flatten()


def era5_boundary_gradients(ds_box, variable, dictionary):
    """ Calculate gradients from boundary values
    using haversine function
    Weight by box size?"""
    # left = left.where(left.longitude > [dictionary["lon_min"] % 360])
    # left = left.sel(latitude=slice(dictionary["lat_max"], dictionary["lat_min"]))
    ds_filtered = ds_box
    if "mask" in dictionary:
        mask = era5_mask(ds_box, dictionary)
        ds_filtered = ds_filtered.where(mask)
    left = ds_box.min("longitude", skipna=True)
    right = ds_box.max("longitude", skipna=True)
    top = ds_filtered.max("latitude", skipna=True)
    bottom = ds_filtered.min("latitude", skipna=True)
    x_gradient = np.mean(
        (right[variable].values - left[variable].values)
        / dist_from_meshgrids(left, right),
        axis=2,
    )
    y_gradient = np.mean(
        (top[variable].values - bottom[variable].values)
        / dist_from_meshgrids(top, bottom),
        axis=2,
    )
    return x_gradient, y_gradient


def era5_regression_gradients(ds_box, variable, dictionary):
    """ Calculate gradients using haversine function
    From regression, using the normal equation"""
    ds_filtered = ds_box
    if "mask" in dictionary:
        mask = era5_mask(ds_box, dictionary)
        ds_filtered = ds_filtered.where(mask)
    lat1_point = dictionary["lat"] * (2 * pi / 360)
    lon1_point = dictionary["lon"] * (2 * pi / 360)
    lat2_mg = ds_filtered["lat_meshgrid"].values * (2 * pi / 360)
    lon2_mg = ds_filtered["lon_meshgrid"].values * (2 * pi / 360)
    dist_array = calc_haver_dist(lat1_point, lon1_point, lat2_mg, lon2_mg)
    theta_array = calc_lat_lon_angle(lat1_point, lon1_point, lat2_mg, lon2_mg)
    x_array = dist_array * np.cos(theta_array)
    y_array = dist_array * np.sin(theta_array)
    x_flat = x_array.flatten()
    y_flat = y_array.flatten()
    ones_flat = np.ones(np.shape(x_flat))
    len_temp = np.shape(ds_box[variable])[0]
    len_levels = np.shape(ds_box[variable])[1]
    x_gradient_array = np.empty((len_temp, len_levels))
    y_gradient_array = np.empty((len_temp, len_levels))
    for this_time in range(len_temp):
        for this_level in range(len_levels):
            data_flat = ds_filtered[variable][
                this_time, this_level, :, :
            ].values.flatten()
            data_flat_filter = data_flat[~np.isnan(data_flat)][:, None]
            x_flat_filter = x_flat[~np.isnan(data_flat)][:, None]
            y_flat_filter = y_flat[~np.isnan(data_flat)][:, None]
            ones_flat_filter = ones_flat[~np.isnan(data_flat)][:, None]
            oxy_mat = np.hstack((ones_flat_filter, x_flat_filter, y_flat_filter))
            theta = np.dot(
                np.dot(
                    np.linalg.pinv(np.dot(oxy_mat.transpose(), oxy_mat)),
                    oxy_mat.transpose(),
                ),
                data_flat_filter,
            )
            x_gradient_array[this_time, this_level] = theta[1]
            y_gradient_array[this_time, this_level] = theta[2]
    return x_gradient_array, y_gradient_array


def era5_gradients(ds_level_1, list_of_vars, dictionary):
    """Add variables defined in list to dictionary"""
    ds_out = xr.Dataset(coords={"time": ds_level_1.time, "lev": ds_level_1.lev})
    for variable in list_of_vars:
        if dictionary["gradients_strategy"] in ["regression", "both"]:
            x_gradient_array, y_gradient_array = era5_regression_gradients(
                ds_level_1, variable, dictionary
            )
        elif dictionary["gradients_strategy"] == "boundary":
            x_gradient_array, y_gradient_array = era5_boundary_gradients(
                ds_level_1, variable, dictionary
            )
        else:
            raise NotImplementedError("Gradients strategy not implemented")
        ds_out["d" + variable + "dx"] = (
            ("time", "lev"),
            x_gradient_array,
            {
                "long_name": ds_level_1[variable].long_name + " x-gradient",
                "units": ds_level_1[variable].units + " m**-1",
            },
        )
        ds_out["d" + variable + "dy"] = (
            ("time", "lev"),
            y_gradient_array,
            {
                "long_name": ds_level_1[variable].long_name + " y-gradient",
                "units": ds_level_1[variable].units + " m**-1",
            },
        )
        if dictionary["gradients_strategy"] == "both":
            x_gradient_array, y_gradient_array = era5_boundary_gradients(
                ds_level_1, variable, dictionary
            )
            ds_out["d" + variable + "dx_bound"] = (
                ("time", "lev"),
                x_gradient_array,
                {
                    "long_name": ds_level_1[variable].long_name
                    + " x-gradient (boundaries)",
                    "units": ds_level_1[variable].units + " m**-1",
                },
            )
            ds_out["d" + variable + "dy_bound"] = (
                ("time", "lev"),
                y_gradient_array,
                {
                    "long_name": ds_level_1[variable].long_name
                    + " y-gradient (boundaries)",
                    "units": ds_level_1[variable].units + " m**-1",
                },
            )
    return ds_out


def era5_adv_tendencies(ds_level_1, list_of_vars, dictionary):
    """Add variables defined in list to dictionary"""
    ds_out = xr.Dataset(coords={"time": ds_level_1.time, "lev": ds_level_1.lev})
    for variable in list_of_vars:
        tendency_array = (
            (ds_level_1["u"].values - dictionary["u_traj"])
            * ds_level_1["d" + variable + "dx"].values
            + (ds_level_1["v"].values - dictionary["v_traj"])
            * ds_level_1["d" + variable + "dy"].values
        )
        ds_out[variable + "_advtend"] = (
            ("time", "lev"),
            tendency_array,
            {
                "long_name": ds_level_1[variable].long_name + " tendency (advection)",
                "units": ds_level_1[variable].units + " s**-1",
            },
        )
        if dictionary["gradients_strategy"] == "both":
            tendency_array = (
                (ds_level_1["u"].values - dictionary["u_traj"])
                * ds_level_1["d" + variable + "dx_bound"].values
                + (ds_level_1["v"].values - dictionary["v_traj"])
                * ds_level_1["d" + variable + "dy_bound"].values
            )
            ds_out[variable + "_advtend_bound"] = (
                ("time", "lev"),
                tendency_array,
                {
                    "long_name": ds_level_1[variable].long_name
                    + " tendency (advection, boundaries)",
                    "units": ds_level_1[variable].units + " s**-1",
                },
            )

    return ds_out


def trace_back(lat, lon, u, v, dt):
    """calculates previous position given lat,lon,u,v, and dt"""
    if dt < 0.0:
        raise Exception("Expecting positive dt in back-tracing")
    # Angle corresponds to opposite (as back-tracing) direction of velocity bearing
    theta = np.arctan2(v, u) % (2 * pi) - (pi / 2)
    dist = np.sqrt(u ** 2 + v ** 2) * dt
    lat_rad = lat * (2 * pi / 360.0)
    lon_rad = lon * (2 * pi / 360.0)
    previous_lat_rad = np.arcsin(
        np.sin(lat_rad) * np.cos(dist / r_earth)
        + np.cos(lat_rad) * np.sin(dist / r_earth) * np.cos(theta)
    )
    previous_lon_rad = lon_rad + np.arctan2(
        np.sin(theta) * np.sin(dist / r_earth) * np.cos(lat_rad),
        np.cos(dist / r_earth) - np.sin(lat_rad) * np.sin(previous_lat_rad),
    )
    previous_lat = previous_lat_rad * (360.0 / (2 * pi))
    previous_lon = previous_lon_rad * (360.0 / (2 * pi))
    return previous_lat, previous_lon


def cos_transition(absolute_input, transition_start, transition_end):
    """function that smoothly transitions from 1 to 0 using a cosine-shaped
    transition between start and end"""
    normalised_input = (absolute_input - transition_start) / (
        transition_end - transition_start
    )
    weight_factor = 1.0 * (normalised_input < 0.0) + (
        0.5 + 0.5 * np.cos(normalised_input * pi)
    ) * (1.0 - (normalised_input < 0.0) - (normalised_input > 1.0))
    return weight_factor


def weighted_velocity(ds_for_vel):
    """weighted velociy: needs more work"""
    pres_cutoff_start = 60000.0
    pres_cutoff_end = 50000.0
    height_factor = cos_transition(
        ds_for_vel["p_f"][:, 1:, :, :].values, pres_cutoff_start, pres_cutoff_end
    )
    weights = (
        (ds_for_vel["p_h"][:, :-1, :, :].values - ds_for_vel["p_h"][:, 1:, :, :].values)
        * ds_for_vel["q"][:, 1:, :, :].values
        * height_factor
    )
    u_weighted = np.sum(ds_for_vel["u"][:, 1:, :, :].values * weights) / np.sum(weights)
    v_weighted = np.sum(ds_for_vel["v"][:, 1:, :, :].values * weights) / np.sum(weights)
    return u_weighted, v_weighted


def add_globals_attrs_to_ds(ds_to_add_to):
    """Adds global attributes to datasets"""
    global_attrs = {
        r"Conventions": r"CF-1.7",
        r"ERA5 reference": r"Hersbach, H., Bell, B., Berrisford, P., Hirahara, S., Horányi, A., Muñoz‐Sabater, J., ... & Simmons, A. (2020). The ERA5 global reanalysis. Quarterly Journal of the Royal Meteorological Society.",
        r"Created": datetime.datetime.now().isoformat(),
        r"Created with": r"https://github.com/EUREC4A-UK/lagtraj",
    }
    for attribute in global_attrs:
        ds_to_add_to.attrs[attribute] = global_attrs[attribute]


def fix_units(ds_to_fix):
    """Changes units of ERA5 data to make them compatible with the cf-checker"""
    units_dict = {
        "(0 - 1)": "-",
        "m of equivalent water": "m",
        "~": "-",
    }
    for variable in ds_to_fix.variables:
        if hasattr(variable, "units"):
            these_units = ds_to_fix[variable].units
            if these_units in units_dict:
                ds_to_fix[variable].units = units_dict[these_units]


def dummy_trajectory(ds_trajectory):
    """Trajectory example
    Needs to use dictionary input instead"""
    ds_traj = xr.Dataset()
    this_lat = 13.3
    this_lon = -57.717
    nr_iterations_traj = 10
    for this_time in range(30, 27, -1):
        print(this_lat, this_lon)
        ds_time = ds_trajectory.isel(time=[this_time])
        dt_traj = (
            ds_trajectory["time"][this_time].values
            - ds_trajectory["time"][this_time - 1].values
        ) / np.timedelta64(1, "s")
        ds_local = era5_interp_column(ds_time, this_lat, this_lon)
        add_heights_and_pressures(ds_local)
        u_end, v_end = weighted_velocity(ds_local)
        previous_lat, previous_lon = trace_back(
            this_lat, this_lon, u_end, v_end, dt_traj
        )
        # iteratively find previous point
        for _ in range(nr_iterations_traj):
            ds_time = ds_trajectory.isel(time=[this_time - 1])
            ds_local = era5_interp_column(ds_time, previous_lat, previous_lon)
            add_heights_and_pressures(ds_local)
            u_begin, v_begin = weighted_velocity(ds_local)
            # estimate of mean velocity over hour
            u_mean = (u_begin + u_end) / 2.0
            v_mean = (v_begin + v_end) / 2.0
            previous_lat, previous_lon = trace_back(
                this_lat, this_lon, u_mean, v_mean, dt_traj
            )
        this_lat = previous_lat
        this_lon = previous_lon
    ds_traj.to_netcdf("ds_out.nc")


def dummy_forcings(ds_forcing):
    """Forcings example"""
    ds_out = xr.Dataset()
    for this_time in range(18, 30):
        ds_time = ds_forcing.isel(time=[this_time])
        lats_lons_dictionary = {
            "lat_min": 11.3,
            "lat_max": 15.3,
            "lon_min": -59.717,
            "lon_max": -55.717,
            "lat": 13.3,
            "lon": -57.717,
            "gradients_strategy": "both",
            "mask": "ocean",
            "u_traj": -6,
            "v_traj": 0,
        }
        out_levels = np.arange(0, 10000.0, 40.0)
        ds_smaller = era_5_subset(ds_time, lats_lons_dictionary)
        add_heights_and_pressures(ds_smaller)
        add_auxiliary_variables(ds_smaller, ["theta"])
        ds_time_height = era5_on_height_levels(ds_smaller, out_levels)
        era5_add_lat_lon_meshgrid(ds_time_height)
        ds_profiles = era5_single_point(ds_time_height, lats_lons_dictionary)
        ds_era5_mean = era5_box_mean(ds_time_height, lats_lons_dictionary)
        for variable in ds_era5_mean.variables:
            if variable not in ["time", "lev"]:
                ds_profiles[variable + "_mean"] = ds_era5_mean[variable]
        ds_gradients = era5_gradients(
            ds_time_height, ["u", "v", "p_f", "theta"], lats_lons_dictionary
        )
        ds_time_step = xr.merge((ds_gradients, ds_profiles))
        ds_tendencies = era5_adv_tendencies(
            ds_time_step, ["u", "v", "p_f", "theta"], lats_lons_dictionary
        )
        ds_time_step = xr.merge((ds_time_step, ds_tendencies))
        ds_out = xr.merge((ds_out, ds_time_step))
    fix_units(ds_out)
    add_globals_attrs_to_ds(ds_out)
    ds_out.to_netcdf("ds_out.nc")


def main():
    """Dummy implementations for trajectory tool"""
    files_model_an = "output_domains/model_an_*_eurec4a_circle_eul_domain.nc"
    files_single_an = "output_domains/single_an_*_eurec4a_circle_eul_domain.nc"
    files_model_fc = "output_domains/model_fc_*_eurec4a_circle_eul_domain.nc"
    files_single_fc = "output_domains/single_fc_*_eurec4a_circle_eul_domain.nc"
    ds_model_an = xr.open_mfdataset(files_model_an, combine="by_coords")
    ds_model_an = ds_model_an.drop_vars(["z", "lnsp"])
    ds_single_an = xr.open_mfdataset(files_single_an, combine="by_coords")
    ds_model_fc = xr.open_mfdataset(files_model_fc, combine="by_coords")
    ds_single_fc = xr.open_mfdataset(files_single_fc, combine="by_coords")
    ds_list = [ds_model_an, ds_single_an, ds_model_fc, ds_single_fc]
    for this_ds in ds_list:
        era_5_normalise_longitude(this_ds)
    ds_merged = xr.merge(ds_list)
    dummy_trajectory(ds_merged)
    dummy_forcings(ds_merged)


if __name__ == "__main__":
    main()
