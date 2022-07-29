import os
import numpy as np
import xarray as xr
import matplotlib.pyplot as plt


def generate_avg_corrector(corrected_bscatter: xr.DataArray, beam_angles_degrees: xr.DataArray, bin_size_degree: float = 1.0,
                           reference_angle: float = 45):
    """
    Compute a new angle varying gain corrector for the provided processed backscatter dataset.  This corrector is then used during
    backscatter surface generation to remove the angle dependance in the processed backscatter.

    Parameters
    ----------
    corrected_bscatter
        processed backscatter returned from one of the Bscatter classes below, 2d array
    beam_angles_degrees
        corrected beam angles for the beams in degrees, 2d array
    bin_size_degree
        size of the bin used to generate the corrector, in degrees
    reference_angle
        angle used to determine the reference backscatter level

    Returns
    -------
    dict
        dictionary of {angles (degrees): avg correctors (dB)}
    """

    bins = np.arange(-90, 90 + bin_size_degree, bin_size_degree)
    # get the mean bscatter value in each angle bin
    meanvals = corrected_bscatter.groupby_bins(beam_angles_degrees, bins, right=True).mean().values
    msk = np.isnan(meanvals)
    # figure out which angle bin is closest to the desired reference angle to use as the reference value
    refval_idx = np.max([np.argmin(np.abs(bins - reference_angle)), 1])
    refval = meanvals[refval_idx - 1]
    # final avg correction is the difference between the angle-bin-mean value and the reference value.  Angles that are
    #  unused in the provided dataset are left as zero
    lookup = {bins[idx]: meanvals[idx] - refval if not msk[idx] else 0 for idx in range(len(meanvals))}
    return lookup


def avg_correct(beam_angles_degrees: xr.DataArray, avg_corrector: dict):
    """
    Return the backscatter corrector for the provided beamangle and avg_corrector dataset

    Use by subtracting from the processed backscatter to correctly avg correct.

    Parameters
    ----------
    beam_angles_degrees
        corrected beam angles for the beams in degrees, 2d array
    avg_corrector
        dictionary of {angles (degrees): avg correctors (dB)}

    Returns
    -------
    np.ndarray
        avg backscatter corrector in dB
    """

    flat_angles = beam_angles_degrees.values.flatten()
    sort_idx = flat_angles.argsort()
    avg_angle, avg_value = list(avg_corrector.keys()), list(avg_corrector.values())
    bins = avg_angle + [avg_angle[-1] + avg_angle[-1] - avg_angle[-2]]
    bin_idx = np.digitize(flat_angles[sort_idx], bins, right=True)
    corrector = np.array(avg_value)[bin_idx]
    revsort_idx = np.argsort(sort_idx)
    corrector = corrector[revsort_idx].reshape(beam_angles_degrees.shape)
    return corrector


class BScatter:
    def __init__(self, raw_intensity: xr.DataArray, slant_range: xr.DataArray, surface_sound_speed: xr.DataArray,
                 beam_angle: xr.DataArray, plot_backscatter: bool):
        self.raw_intensity = raw_intensity
        self.slant_range = slant_range
        self.surface_sound_speed = surface_sound_speed
        self.beam_angle = beam_angle

        # if plotting is enabled, will save the components to this dict
        self.plot_backscatter = plot_backscatter
        self.plot_components = {}

    @property
    def spherical_spreading(self):
        return 40 * np.log10(self.slant_range)

    @property
    def transmission_loss(self):
        return self.spherical_spreading + self.attenuation

    @property
    def fixed_gain(self):
        raise NotImplementedError('Expected a sonar specific backscatter class to inherit this class to process')

    @property
    def attenuation(self):
        raise NotImplementedError('Expected a sonar specific backscatter class to inherit this class to process')

    @property
    def tvg(self):
        raise NotImplementedError('Expected a sonar specific backscatter class to inherit this class to process')

    @property
    def area_correction(self):
        raise NotImplementedError('Expected a sonar specific backscatter class to inherit this class to process')

    def _add_plot_component(self, pc_tag: str, data):
        if self.plot_backscatter:
            try:
                self.plot_components[pc_tag] = data.isel(time=0).values
            except AttributeError:
                self.plot_components[pc_tag] = data

    def _plot_backscatter_components(self):
        drive_plots_to_file = isinstance(self.plot_backscatter, str)
        if drive_plots_to_file:
            plt.ioff()  # turn off interactive plotting
            if os.path.isdir(self.plot_backscatter):
                bscat_fname = os.path.join(self.plot_backscatter, 'backscatter_firstping_sample.png')
            elif os.path.isfile(self.plot_backscatter):
                bscat_fname = os.path.join(os.path.splitext(self.plot_backscatter)[0] + '_sample.png')

        bscat_figure = plt.figure(figsize=(12, 9))
        plt.title('backscatter components of first ping')
        plt.ylabel('dB')
        plt.xlabel('beam')
        for comp in self.plot_components.keys():
            if isinstance(self.plot_components[comp], (float, int)):
                plt.axhline(y=self.plot_components[comp], linestyle='dashed', label=comp)
            else:
                plt.plot(self.plot_components[comp], label=comp)
        plt.legend()
        if drive_plots_to_file:
            plt.savefig(bscat_fname)
        plt.close(bscat_figure)

    def process(self, fixed_gain_corrected: bool = True, tvg_corrected: bool = True,
                transmission_loss_corrected: bool = True, area_corrected: bool = True):
        out_intensity = self.raw_intensity
        self._add_plot_component('raw_intensity', out_intensity)
        if fixed_gain_corrected:
            corrector = self.fixed_gain
            out_intensity -= corrector
            self._add_plot_component('fixed_gain', corrector)
        if tvg_corrected:
            corrector = self.tvg
            out_intensity += corrector
            self._add_plot_component('tvg', corrector)
        if transmission_loss_corrected:
            corrector = self.transmission_loss
            out_intensity -= corrector
            self._add_plot_component('transmission_loss', corrector)
        if area_corrected:
            corrector = self.area_correction
            out_intensity -= corrector
            self._add_plot_component('area_correction', corrector)
        self._add_plot_component('final_intensity', out_intensity)
        if self.plot_backscatter:
            self._plot_backscatter_components()
        return out_intensity


class S7kscatter(BScatter):
    def __init__(self, runtime_parameters: dict, raw_intensity: xr.DataArray, slant_range: xr.DataArray, surface_sound_speed: xr.DataArray,
                 beam_angle: xr.DataArray, tx_beam_width: float, rx_beam_width: float, plot_backscatter: bool = True):
        super().__init__(raw_intensity, slant_range, surface_sound_speed, beam_angle, plot_backscatter)
        self.runtime_parameters = runtime_parameters
        self.absorption_db_m = float(self.runtime_parameters['absorption_db_km']) / 1000
        self.spreading_loss_db = float(self.runtime_parameters['spreading_loss_db'])
        self.power_selection_db_re_1micropascal = float(self.runtime_parameters['power_selection_db_re_1micropascal'])
        self.tx_beam_width = tx_beam_width
        self.rx_beam_width = rx_beam_width
        self.pulse_length = float(self.runtime_parameters['tx_pulse_width_seconds'])
        self.gain_selection_db = float(self.runtime_parameters['gain_selection_db'])

    @property
    def fixed_gain(self):
        return self.gain_selection_db + self.power_selection_db_re_1micropascal

    @property
    def attenuation(self):
        return 2 * self.absorption_db_m * self.slant_range

    @property
    def tvg(self):
        return (self.spreading_loss_db * np.log10(self.slant_range)) + self.attenuation

    @property
    def area_correction(self):
        area_beam_limited = self.tx_beam_width * self.rx_beam_width * ((self.slant_range * np.pi/180) ** 2)
        area_pulse_limited = (self.surface_sound_speed * self.pulse_length * self.tx_beam_width * self.slant_range * (np.pi / 180)) / (2 * np.sin(np.abs(self.beam_angle)))
        return 10 * np.log10(np.minimum(area_beam_limited, area_pulse_limited))


class Allscatter(BScatter):
    def __init__(self, runtime_parameters: dict, raw_intensity: xr.DataArray, slant_range: xr.DataArray, surface_sound_speed: xr.DataArray,
                 beam_angle: xr.DataArray, tx_beam_width: float, rx_beam_width: float, near_normal_corrector: xr.DataArray,
                 pulse_length: xr.DataArray, plot_backscatter: bool = True):
        super().__init__(raw_intensity, slant_range, surface_sound_speed, beam_angle, plot_backscatter)
        self.runtime_parameters = runtime_parameters
        self.absorption_db_m = float(self.runtime_parameters['AbsorptionCoefficent']) / 1000
        self.tx_beam_width = tx_beam_width
        self.rx_beam_width = rx_beam_width
        self.near_normal_corrector = near_normal_corrector
        self.pulse_length = pulse_length

    @property
    def fixed_gain(self):
        # there is a ReceiverFixedGain runtime parameter but it contains Mode2 for most sonar.  It appears to mostly be a legacy thing.
        # from the docs "Receiver fixed gain setting in dB (only valid for) EM 2000, EM 1002, EM 3000, EM 3002, EM300, EM 120"
        return 0.0

    @property
    def attenuation(self):
        return 2 * self.absorption_db_m * self.slant_range

    @property
    def tvg(self):
        return (40 * np.log10(self.slant_range)) + self.attenuation - self.near_normal_corrector

    @property
    def area_correction(self):
        area_beam_limited = self.tx_beam_width * self.rx_beam_width * ((self.slant_range * np.pi/180) ** 2)
        area_pulse_limited = (self.surface_sound_speed * self.pulse_length * self.tx_beam_width * self.slant_range * (np.pi / 180)) / (2 * np.sin(np.abs(self.beam_angle)))
        return 10 * np.log10(np.minimum(area_beam_limited, area_pulse_limited))


class Kmallscatter(BScatter):
    def __init__(self, runtime_parameters: dict, raw_intensity: xr.DataArray, slant_range: xr.DataArray, surface_sound_speed: xr.DataArray,
                 beam_angle: xr.DataArray, tx_beam_width: float, rx_beam_width: float, pulse_length: xr.DataArray, tvg: xr.DataArray,
                 fixedgain: xr.DataArray, plot_backscatter: bool = True):
        super().__init__(raw_intensity, slant_range, surface_sound_speed, beam_angle, plot_backscatter)
        self.runtime_parameters = runtime_parameters
        self.fixedgain = fixedgain
        self.tx_beam_width = tx_beam_width
        self.rx_beam_width = rx_beam_width
        self.pulse_length = pulse_length
        self.tvg_arr = tvg

    @property
    def fixed_gain(self):
        return self.fixedgain

    @property
    def transmission_loss(self):
        return 0.0

    @property
    def tvg(self):
        return self.tvg_arr

    @property
    def area_correction(self):
        area_beam_limited = self.tx_beam_width * self.rx_beam_width * ((self.slant_range * np.pi/180) ** 2)
        area_pulse_limited = (self.surface_sound_speed * self.pulse_length * self.tx_beam_width * self.slant_range * (np.pi / 180)) / (2 * np.sin(np.abs(self.beam_angle)))
        return 10 * np.log10(np.minimum(area_beam_limited, area_pulse_limited))


def distrib_run_process_backscatter(worker_dat: list):
    multibeam_extension = worker_dat[-1]
    backscatter_settings = worker_dat[-2]
    if multibeam_extension == '.all':
        bclass = Allscatter(worker_dat[0], worker_dat[1], worker_dat[2], worker_dat[3], worker_dat[4], worker_dat[5],
                            worker_dat[6], worker_dat[7], worker_dat[8], plot_backscatter=worker_dat[9])
    elif multibeam_extension == '.s7k':
        bclass = S7kscatter(worker_dat[0], worker_dat[1], worker_dat[2], worker_dat[3], worker_dat[4], worker_dat[5],
                            worker_dat[6], plot_backscatter=worker_dat[7])
    elif multibeam_extension == '.kmall':
        bclass = Kmallscatter(worker_dat[0], worker_dat[1], worker_dat[2], worker_dat[3], worker_dat[4], worker_dat[5],
                              worker_dat[6], worker_dat[7], worker_dat[8], worker_dat[9], plot_backscatter=worker_dat[10])
    else:
        raise NotImplementedError(f'distrib_run_process_backscatter: filetype {multibeam_extension} is not currently supported for backscatter processing')
    pscatter = bclass.process(**backscatter_settings)
    return pscatter
