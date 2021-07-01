import math
from abc import ABC, abstractmethod

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from ..utils.preprocess.events import find_event_channel

BP_ORDER = 2
_BUFFER_DURATION = 30  # seconds


class _Scope(ABC):
    """
    Class representing a base scope.

    Parameters
    ----------
    stream_receiver : neurodecode.stream_receiver.StreamReceiver
        The connected stream receiver.
    stream_name : str
        The stream to connect to.
    """

    # ---------------------------- INIT ----------------------------
    @abstractmethod
    def __init__(self, stream_receiver, stream_name):
        assert stream_name in stream_receiver.streams.keys()
        self._sr = stream_receiver
        self._stream_name = stream_name
        self._init_infos()
        self._init_buffer(_BUFFER_DURATION)

    @abstractmethod
    def _init_infos(self):
        """
        Extract basic stream informations.
        """
        self._sample_rate = int(
            self._sr.streams[self._stream_name].sample_rate)

    @abstractmethod
    def _init_buffer(self, duration_buffer):
        """
        Initialize buffer(s).
        """
        self._duration_buffer = duration_buffer
        self._n_samples_buffer = math.ceil(duration_buffer * self._sample_rate)
        self.ts_list = list()

    # -------------------------- Main Loop -------------------------
    @abstractmethod
    def update_loop(self):
        """
        Main update loop acquiring data from the LSL stream and filling the
        scope's buffer.
        """
        self._read_lsl_stream()

    @abstractmethod
    def _read_lsl_stream(self):
        """
        Acquires data from the connected LSL stream.
        """
        self._sr.acquire()
        self._data_acquired, self.ts_list = self._sr.get_buffer()
        self._sr.reset_buffer()

        if len(self.ts_list) == 0:
            return

    # --------------------------------------------------------------------
    @property
    def stream_name(self):
        """
        The name of the connected stream.
        """
        return self._stream_name

    @property
    def sample_rate(self):
        """
        The sample rate of the connected stream.
        """
        return self._sample_rate

    @property
    def duration_buffer(self):
        """
        The duration of the scope's buffer.
        """
        return self._duration_buffer


class ScopeEEG(_Scope):
    """
    Class representing an EEG scope.

    Parameters
    ----------
    stream_receiver : neurodecode.stream_receiver.StreamReceiver
        The connected stream receiver.
    stream_name : str
        The stream to connect to.
    """

    # ---------------------------- INIT ----------------------------
    def __init__(self, stream_receiver, stream_name):
        super().__init__(stream_receiver, stream_name)
        self._init_signal_y_scales()
        self._init_variables()

    def _init_infos(self):
        """
        Extract basic stream informations.
        """
        super()._init_infos()
        tch = find_event_channel(
            ch_names=self._sr.streams[self._stream_name].ch_list)
        if tch is None:
            self._channels_labels = \
                self._sr.streams[self._stream_name].ch_list
        else:
            self._channels_labels = \
                [channel for k, channel in enumerate(
                    self._sr.streams[self._stream_name].ch_list) if k != tch]
        self._n_channels = len(self._channels_labels)

    def _init_signal_y_scales(self):
        """
        The available signal scale/range values as a dictionnary {key: value}
        with key a representative string and value in uV.
        """
        self._signal_y_scales = {'1uV': 1, '10uV': 10, '25uV': 25,
                                 '50uV': 50, '100uV': 100, '250uV': 250,
                                 '500uV': 500, '1mV': 1000, '2.5mV': 2500,
                                 '100mV': 100000}

    def _init_variables(self):
        """
        Initialize variables.
        """
        self._apply_car = False
        self._apply_bandpass = False
        self.channels_to_show_idx = list(range(self._n_channels))

    def _init_buffer(self, duration_buffer):
        """
        Initialize buffer(s).
        """
        super()._init_buffer(duration_buffer)
        self.trigger_buffer = np.zeros(self._n_samples_buffer)
        self.data_buffer = np.zeros((self._n_channels, self._n_samples_buffer),
                                    dtype=np.float32)

    def init_bandpass_filter(self, low, high):
        """
        Initialize the bandpass filter. The filter is a butter filter of order
        neurodecode.stream_viewer._scope.BP_ORDER

        Parameters
        ----------
        low : int | float
            The frequency at which the signal is high-passed.
        high : int | float
            The frequency at which the signal is low-passed.
        """
        bp_low = low / (0.5 * self._sample_rate)
        bp_high = high / (0.5 * self._sample_rate)
        self._sos = butter(BP_ORDER, [bp_low, bp_high],
                           btype='band', output='sos')
        self._zi_coeff = sosfilt_zi(
            self._sos).reshape((self._sos.shape[0], 2, 1))
        self._zi = None

    # -------------------------- Main Loop -------------------------
    def update_loop(self):
        """
        Main update loop acquiring data from the LSL stream and filling the
        scope's buffer.
        """
        super().update_loop()
        if len(self.ts_list) > 0:
            self._filter_signal()
            self._filter_trigger()
            # shape (channels, samples)
            self.data_buffer = np.roll(self.data_buffer, -len(self.ts_list),
                                       axis=1)
            self.data_buffer[:, -len(self.ts_list):] = self._data_acquired.T
            # shape (samples, )
            self.trigger_buffer = np.roll(
                self.trigger_buffer, -len(self.ts_list))
            self.trigger_buffer[-len(self.ts_list):] = self._trigger_acquired

    def _read_lsl_stream(self):
        """
        Acquires data from the connected LSL stream. The acquired data is
        splitted between the trigger channel and the data channels.
        """
        super()._read_lsl_stream()
        # Remove trigger ch - shapes (samples, ) and (samples, channels)
        self._trigger_acquired = self._data_acquired[:, 0]
        self._data_acquired = self._data_acquired[:, 1:].reshape(
            (-1, self._n_channels))

    def _filter_signal(self):
        """
        Apply bandpass and CAR filter to the signal acquired if needed.
        """
        if self._apply_bandpass:
            if self._zi is None:
                # Multiply by DC offset
                self._zi = self._zi_coeff*np.mean(self._data_acquired, axis=0)
            self._data_acquired, self._zi = sosfilt(
                self._sos, self._data_acquired, 0, self._zi)

        if self._apply_car and len(self.channels_to_show_idx) >= 2:
            car_ch = np.mean(
                self._data_acquired[:, self.channels_to_show_idx], axis=1)
            self._data_acquired -= car_ch.reshape((-1, 1))

    def _filter_trigger(self, tol=0.05):
        """
        Cleans up the trigger signal by removing successive duplicates of a
        trigger value.
        """
        self._trigger_acquired[
            np.abs(np.diff(self._trigger_acquired, prepend=[0])) <= tol] = 0

    # --------------------------------------------------------------------
    @property
    def apply_car(self):
        """
        Boolean. Applies CAR if True.
        """
        return self._apply_car

    @apply_car.setter
    def apply_car(self, apply_car):
        self._apply_car = bool(apply_car)

    @property
    def apply_bandpass(self):
        """
        Boolean. Applies bandpass filter if True.
        """
        return self._apply_bandpass

    @apply_bandpass.setter
    def apply_bandpass(self, apply_bandpass):
        self._apply_bandpass = bool(apply_bandpass)

    @property
    def channels_labels(self):
        """
        List of the channel labels present in the connected stream.
        The TRIGGER channel is removed.
        """
        return self._channels_labels

    @property
    def n_channels(self):
        """
        Number of channels present in the connected stream.
        The TRIGGER channel is removed.
        """
        return self._n_channels

    @property
    def signal_y_scales(self):
        return self._signal_y_scales
