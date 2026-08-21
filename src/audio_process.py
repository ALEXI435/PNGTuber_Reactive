import collections
import logging
import math
import threading
import time

import librosa
import numpy as np
import scipy.signal
import sounddevice as sd

logger = logging.getLogger("audio_process")


def detect_audio_settings(device_name):
    """Determines optimal audio device parameters (samplerate, blocksize)."""
    device_info = sd.query_devices(device_name)

    max_channels = device_info["max_input_channels"]
    if max_channels == 0:
        raise RuntimeError(
            f"Устройство '{device_info['name']}' не имеет микрофонных входов."
        )

    samplerate = int(device_info["default_samplerate"])
    latency = device_info["default_low_input_latency"]

    calculated_blocksize = int(latency * samplerate)

    if calculated_blocksize < 32:
        blocksize = 32
    else:
        power = int(np.ceil(np.log2(calculated_blocksize)))
        blocksize = 2**power

    blocksize = int(np.clip(blocksize, 32, 4096))

    return samplerate, blocksize, max_channels


def get_loudest_channel(audio_block):
    """Selects the loudest channel from multi-channel audio."""
    rms_per_channel = np.sqrt(np.mean(audio_block**2, axis=0))

    if audio_block.ndim > 1:
        channel = np.argmax(rms_per_channel)
        return audio_block[:, channel], rms_per_channel[channel]

    return audio_block, rms_per_channel


# ==== Voice detector and pitch parameters ====
# Custom voice detector replaces WebRTC VAD (doesn't work on Windows with Russian locale)
# Uses combination of features: RMS, zero-crossing rate, spectral centroid, energy variance
# Pitch is determined via librosa.piptrack (autocorrelation)

VOICE_SAMPLERATE = 16000  # Sampling rate for voice detector
VOICE_FRAME_MS = 30       # Analysis frame size in milliseconds
VOICE_FRAME_SAMPLES = VOICE_SAMPLERATE * VOICE_FRAME_MS // 1000

PITCH_MIN_FREQ = 60   # Minimum human voice frequency (Hz)
PITCH_MAX_FREQ = 500  # Maximum human voice frequency (Hz)
PITCH_BUF_SIZE = 2048  # Buffer size for autocorrelation
PITCH_CONFIDENCE_THRESHOLD = 0.5  # Minimum pitch detection confidence
PITCH_SMOOTHING_FRAMES = 4  # Number of frames for moving average
PYIN_FMIN = PITCH_MIN_FREQ
PYIN_FMAX = PITCH_MAX_FREQ
PYIN_FRAME_LENGTH = 2048
PYIN_HOP_LENGTH = 512

# Voice detector thresholds (tuned for maximum sensitivity)
RMS_THRESHOLD = 0.003         # Minimum loudness for analysis
ZCR_MIN = 0.02                 # Minimum zero-crossing rate (human speech)
ZCR_MAX = 0.7                  # Maximum zero-crossing rate (white noise higher)
SPECTRAL_CENTROID_MIN = 150   # Minimum spectral centroid (Hz)
SPECTRAL_CENTROID_MAX = 4500  # Maximum spectral centroid (Hz)
ENERGY_VARIANCE_MAX = 1.0     # Maximum energy variance

VOICE_CONFIRM_FRAMES = 2      # Consecutive frames to confirm voice
SILENCE_CONFIRM_FRAMES = 1    # Consecutive frames to reset voice

EMA_ALPHA = 0.3  # Exponential moving average coefficient
DECAY_TIME_MS = 300  # Pitch decay time to 0 when no voice (ms)


def compute_zero_crossing_rate(signal):
    """Computes zero-crossing rate - sign change speed of signal.
    
    Human speech has characteristic values (0.02-0.7).
    """
    if len(signal) < 2:
        return 0.0
    sign_changes = np.sum(np.diff(np.sign(signal)) != 0)
    return sign_changes / len(signal)


def compute_spectral_centroid(signal, samplerate):
    """Computes spectral centroid - center of mass of spectrum.
    
    Human voice typically in range 150-4500 Hz.
    """
    if len(signal) < 2:
        return 0.0
    fft = np.fft.rfft(signal)
    mag = np.abs(fft)
    if np.sum(mag) == 0:
        return 0.0
    freqs = np.fft.rfftfreq(len(signal), 1.0 / samplerate)
    return np.sum(freqs * mag) / np.sum(mag)


def compute_energy_variance(signal, window_size=10):
    """Computes energy variance across windows.
    
    Speech has smooth energy changes, sharp jumps indicate noise.
    """
    if len(signal) < window_size:
        return 0.0
    energy = signal ** 2
    n_windows = len(energy) // window_size
    if n_windows < 2:
        return 0.0
    windowed_energy = energy[:n_windows * window_size].reshape(n_windows, window_size)
    window_means = np.mean(windowed_energy, axis=1)
    return np.var(window_means) if len(window_means) > 1 else 0.0


def autocorrelation_pitch_detection(signal, samplerate, min_freq=PYIN_FMIN, max_freq=PYIN_FMAX):
    """Determines fundamental frequency via autocorrelation (librosa.piptrack).
    
    Returns frequency in Hz and confidence (0-1). Works on both speech
    signals and pure tones.
    """
    if len(signal) < PYIN_FRAME_LENGTH:
        padded = np.zeros(PYIN_FRAME_LENGTH)
        padded[:len(signal)] = signal
        signal = padded
    
    try:
        pitches, magnitudes = librosa.piptrack(
            y=signal,
            sr=samplerate,
            threshold=0.1,
            fmin=min_freq,
            fmax=max_freq
        )
        
        if magnitudes.size > 0:
            pitch_values = []
            for t in range(magnitudes.shape[1]):
                index = magnitudes[:, t].argmax()
                pitch = pitches[index, t]
                if pitch > 0:
                    pitch_values.append(pitch)
            
            if pitch_values:
                median_pitch = np.median(pitch_values)
                confidence = len(pitch_values) / magnitudes.shape[1]
                
                if not (min_freq <= median_pitch <= max_freq):
                    return 0.0, 0.0
                
                return float(median_pitch), float(confidence)
        
        return 0.0, 0.0
        
    except Exception as e:
        logging.warning(f"Error in autocorrelation_pitch_detection: {e}")
        return 0.0, 0.0


class PitchTracker:
    """Voice detector and pitch tracker for determining voice pitch.
    
    Uses custom heuristic to distinguish voice from random sounds
    (RMS, zero-crossing rate, spectral centroid, energy variance).
    Pitch is determined via librosa.piptrack (autocorrelation).
    Includes EMA smoothing and decay system (300ms) for smoothness.
    """

    def __init__(self, device_samplerate):
        self.device_samplerate = device_samplerate

        gcd = math.gcd(device_samplerate, VOICE_SAMPLERATE)
        self._resample_up = VOICE_SAMPLERATE // gcd
        self._resample_down = device_samplerate // gcd

        self._leftover = np.zeros(0, dtype=np.float32)
        self._smoothing = collections.deque(maxlen=PITCH_SMOOTHING_FRAMES)
        self._voice_frame_streak = 0
        self._silence_frame_streak = 0
        
        self._ema_pitch = 0.0
        self._ema_initialized = False
        
        self._last_nonzero_pitch = 0.0
        self._last_nonzero_time = 0.0
        self._decay_start_time = None

    def process(self, mono_signal):
        """Processes audio signal and returns smoothed pitch in Hz or 0.0.
        
        mono_signal — float array in device native samplerate.
        Returns current pitch value with EMA smoothing and decay.
        """
        now = time.monotonic()
        
        if self.device_samplerate == VOICE_SAMPLERATE:
            resampled = np.asarray(mono_signal, dtype=np.float32)
        else:
            resampled = scipy.signal.resample_poly(
                mono_signal, self._resample_up, self._resample_down
            ).astype(np.float32)

        buffer = np.concatenate([self._leftover, resampled])

        n_frames = len(buffer) // VOICE_FRAME_SAMPLES
        for i in range(n_frames):
            frame = buffer[i * VOICE_FRAME_SAMPLES : (i + 1) * VOICE_FRAME_SAMPLES]
            self._process_frame(frame)

        self._leftover = buffer[n_frames * VOICE_FRAME_SAMPLES :]

        if self._smoothing:
            moving_avg = sum(self._smoothing) / len(self._smoothing)
            
            # EMA smoothing to reduce pitch jumps
            if not self._ema_initialized:
                self._ema_pitch = moving_avg
                self._ema_initialized = True
            else:
                self._ema_pitch = EMA_ALPHA * moving_avg + (1 - EMA_ALPHA) * self._ema_pitch
            
            # Decay system for smooth fall to 0
            if self._ema_pitch > 0:
                self._last_nonzero_pitch = self._ema_pitch
                self._last_nonzero_time = now
                self._decay_start_time = None
                return self._ema_pitch
            else:
                if self._last_nonzero_pitch > 0:
                    if self._decay_start_time is None:
                        self._decay_start_time = now
                    
                    elapsed_ms = (now - self._decay_start_time) * 1000
                    if elapsed_ms < DECAY_TIME_MS:
                        decay_factor = 1.0 - (elapsed_ms / DECAY_TIME_MS)
                        decayed_pitch = self._last_nonzero_pitch * decay_factor
                        return decayed_pitch
                    else:
                        self._last_nonzero_pitch = 0.0
                        self._decay_start_time = None
                        return 0.0
                else:
                    return 0.0
        else:
            self._ema_initialized = False
            self._last_nonzero_pitch = 0.0
            self._decay_start_time = None
            return 0.0

    def _process_frame(self, frame):
        """Analyzes one frame and determines if it's speech.
        
        Uses combination of features to distinguish human voice
        from random sounds (creaks, crackling, clicks).
        """
        rms = np.sqrt(np.mean(frame ** 2))
        zcr = compute_zero_crossing_rate(frame)
        spectral_centroid = compute_spectral_centroid(frame, VOICE_SAMPLERATE)
        energy_variance = compute_energy_variance(frame)

        is_voice = (
            rms >= RMS_THRESHOLD and
            ZCR_MIN <= zcr <= ZCR_MAX and
            SPECTRAL_CENTROID_MIN <= spectral_centroid <= SPECTRAL_CENTROID_MAX and
            energy_variance <= ENERGY_VARIANCE_MAX
        )

        if not is_voice:
            # Require multiple consecutive silence frames before reset
            self._silence_frame_streak += 1
            self._voice_frame_streak = 0
            if self._silence_frame_streak >= SILENCE_CONFIRM_FRAMES:
                self._smoothing.clear()
            return

        # Require multiple consecutive voice frames for confirmation
        self._voice_frame_streak += 1
        self._silence_frame_streak = 0
        if self._voice_frame_streak < VOICE_CONFIRM_FRAMES:
            return

        raw_pitch, confidence = autocorrelation_pitch_detection(
            frame, VOICE_SAMPLERATE,
            min_freq=PITCH_MIN_FREQ,
            max_freq=PITCH_MAX_FREQ
        )

        if confidence < PITCH_CONFIDENCE_THRESHOLD:
            return

        if not (PITCH_MIN_FREQ <= raw_pitch <= PITCH_MAX_FREQ):
            return

        self._smoothing.append(raw_pitch)


class AudioEngine:
    """Real-time audio stream for RMS and pitch detection."""

    def __init__(self, device_name):
        self.device_name = device_name

        self.samplerate, self.blocksize, self.max_channels = detect_audio_settings(
            device_name
        )
        self.channels = min(self.max_channels, 2)

        logger.info(
            "AudioEngine created: device=%s, samplerate=%d, blocksize=%d, channels=%d",
            device_name, self.samplerate, self.blocksize, self.channels,
        )

        self._pitch_tracker = PitchTracker(self.samplerate)

        self._lock = threading.Lock()
        self._latest_rms = 0.0
        self._latest_pitch = 0.0
        
        self._last_nonzero_rms = 0.0
        self._last_nonzero_rms_time = 0.0
        self._rms_decay_start_time = None

        self._last_callback_at = None
        self._started_at = None

        self._stream = None

    def _callback(self, indata, frames, time_info, status):
        """sounddevice callback for each audio block."""
        self._last_callback_at = time.monotonic()
        now = time.monotonic()

        if status:
            logger.warning("Audio stream status: %s", status)

        try:
            signal, rms = get_loudest_channel(indata)
            pitch = self._pitch_tracker.process(signal.astype(np.float32))
        except Exception:
            logger.exception("Error during audio block analysis")
            return

        # RMS decay system
        if rms > 0:
            self._last_nonzero_rms = rms
            self._last_nonzero_rms_time = now
            self._rms_decay_start_time = None
            final_rms = rms
        else:
            if self._last_nonzero_rms > 0:
                if self._rms_decay_start_time is None:
                    self._rms_decay_start_time = now
                
                elapsed_ms = (now - self._rms_decay_start_time) * 1000
                if elapsed_ms < DECAY_TIME_MS:
                    decay_factor = 1.0 - (elapsed_ms / DECAY_TIME_MS)
                    final_rms = self._last_nonzero_rms * decay_factor
                else:
                    self._last_nonzero_rms = 0.0
                    self._rms_decay_start_time = None
                    final_rms = 0.0
            else:
                final_rms = 0.0

        with self._lock:
            self._latest_rms = float(final_rms)
            self._latest_pitch = float(pitch)

    def start(self):
        """Starts audio stream."""
        if self._stream is not None:
            return

        logger.info("Opening InputStream for device %s...", self.device_name)
        self._stream = sd.InputStream(
            device=self.device_name,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            channels=self.channels,
            callback=self._callback,
        )
        self._stream.start()
        self._started_at = time.monotonic()
        logger.info("Stream started for device %s", self.device_name)

    def stop(self):
        """Stops audio stream."""
        if self._stream is None:
            return

        logger.info("Stopping stream for device %s", self.device_name)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        
        self._last_nonzero_rms = 0.0
        self._last_nonzero_rms_time = 0.0
        self._rms_decay_start_time = None

    def is_alive(self, timeout=2.5):
        """Checks if audio stream is active.
        
        Callback is called constantly (including on silence), so
        long absence of calls indicates dead stream.
        """
        if self._stream is None or self._started_at is None:
            return False

        now = time.monotonic()

        if self._last_callback_at is None:
            return (now - self._started_at) < timeout

        return (now - self._last_callback_at) < timeout

    def get_latest(self):
        """Thread-safely returns (rms, pitch) — call from GUI thread."""
        with self._lock:
            return self._latest_rms, self._latest_pitch

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
