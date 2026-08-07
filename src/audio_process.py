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
    """Определяет оптимальные параметры аудио устройства (частота, размер буфера)."""
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
    """Выбирает самый громкий канал из многоканального аудио."""
    rms_per_channel = np.sqrt(np.mean(audio_block**2, axis=0))

    if audio_block.ndim > 1:
        channel = np.argmax(rms_per_channel)
        return audio_block[:, channel], rms_per_channel[channel]

    return audio_block, rms_per_channel


# ==== Параметры детектора речи и pitch ====
# Собственный детектор речи заменяет WebRTC VAD (не работает на Windows с русской локалью)
# Использует комбинацию признаков: RMS, zero-crossing rate, спектральный центроид, дисперсия энергии
# Pitch определяется через librosa.piptrack (автокорреляция)

VOICE_SAMPLERATE = 16000  # Частота дискретизации для детектора речи
VOICE_FRAME_MS = 30       # Размер кадра анализа в миллисекундах
VOICE_FRAME_SAMPLES = VOICE_SAMPLERATE * VOICE_FRAME_MS // 1000

PITCH_MIN_FREQ = 60   # Минимальная частота человеческого голоса (Гц)
PITCH_MAX_FREQ = 500  # Максимальная частота человеческого голоса (Гц)
PITCH_BUF_SIZE = 2048  # Размер буфера для autocorrelation
PITCH_CONFIDENCE_THRESHOLD = 0.5  # Минимальная уверенность pitch detection
PITCH_SMOOTHING_FRAMES = 4  # Количество кадров для скользящего среднего
PYIN_FMIN = PITCH_MIN_FREQ
PYIN_FMAX = PITCH_MAX_FREQ
PYIN_FRAME_LENGTH = 2048
PYIN_HOP_LENGTH = 512

# Пороги детектора речи (настроены для максимальной чувствительности)
RMS_THRESHOLD = 0.003         # Минимальная громкость для анализа
ZCR_MIN = 0.02                 # Минимальный zero-crossing rate (человеческая речь)
ZCR_MAX = 0.7                  # Максимальный zero-crossing rate (белый шум выше)
SPECTRAL_CENTROID_MIN = 150   # Минимальный спектральный центроид (Гц)
SPECTRAL_CENTROID_MAX = 4500  # Максимальный спектральный центроид (Гц)
ENERGY_VARIANCE_MAX = 1.0     # Максимальная дисперсия энергии

VOICE_CONFIRM_FRAMES = 2      # Кадров подряд для подтверждения речи
SILENCE_CONFIRM_FRAMES = 1    # Кадров подряд для сброса речи

EMA_ALPHA = 0.3  # Коэффициент экспоненциального скользящего среднего
DECAY_TIME_MS = 300  # Время затухания pitch до 0 при отсутствии речи (мс)


def compute_zero_crossing_rate(signal):
    """Вычисляет zero-crossing rate - скорость изменения знака сигнала.
    
    Человеческая речь имеет характерные значения (0.02-0.7).
    """
    if len(signal) < 2:
        return 0.0
    sign_changes = np.sum(np.diff(np.sign(signal)) != 0)
    return sign_changes / len(signal)


def compute_spectral_centroid(signal, samplerate):
    """Вычисляет спектральный центроид - центр масс спектра.
    
    Человеческий голос обычно находится в диапазоне 150-4500 Гц.
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
    """Вычисляет дисперсию энергии по окнам.
    
    Речь имеет плавные изменения энергии, резкие скачки указывают на шум.
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
    """Определяет основную частоту через автокорреляцию (librosa.piptrack).
    
    Возвращает частоту в Гц и confidence (0-1). Работает как на речевых
    сигналах, так и на чистых тонах.
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
        logging.warning(f"Ошибка в autocorrelation_pitch_detection: {e}")
        return 0.0, 0.0


class PitchTracker:
    """Детектор речи и pitch для определения тона голоса.
    
    Использует собственную эвристику для отличия речи от случайных звуков
    (RMS, zero-crossing rate, спектральный центроид, дисперсия энергии).
    Pitch определяется через librosa.piptrack (автокорреляция).
    Включает EMA сглаживание и систему затухания (300мс) для плавности.
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
        """Обрабатывает аудио сигнал и возвращает сглаженный pitch в Гц или 0.0.
        
        mono_signal — float-массив в родной частоте устройства.
        Возвращает текущее значение pitch с EMA сглаживанием и decay.
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
            
            # EMA сглаживание для уменьшения скачков тона
            if not self._ema_initialized:
                self._ema_pitch = moving_avg
                self._ema_initialized = True
            else:
                self._ema_pitch = EMA_ALPHA * moving_avg + (1 - EMA_ALPHA) * self._ema_pitch
            
            # Система затухания (decay) для плавного падения до 0
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
        """Анализирует один кадр и определяет, является ли он речью.
        
        Использует комбинацию признаков для отличия человеческого голоса
        от случайных звуков (скрипы, треск, щелчки).
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
            # Требуем несколько кадров тишины подряд перед сбросом
            self._silence_frame_streak += 1
            self._voice_frame_streak = 0
            if self._silence_frame_streak >= SILENCE_CONFIRM_FRAMES:
                self._smoothing.clear()
            return

        # Требуем несколько кадров речи подряд для подтверждения
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
    """Аудио-поток для реального времени определения RMS и pitch."""

    def __init__(self, device_name):
        self.device_name = device_name

        self.samplerate, self.blocksize, self.max_channels = detect_audio_settings(
            device_name
        )
        self.channels = min(self.max_channels, 2)

        logger.info(
            "AudioEngine создан: device=%s, samplerate=%d, blocksize=%d, channels=%d",
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
        """Callback функции sounddevice для каждого аудио блока."""
        self._last_callback_at = time.monotonic()
        now = time.monotonic()

        if status:
            logger.warning("Audio stream status: %s", status)

        try:
            signal, rms = get_loudest_channel(indata)
            pitch = self._pitch_tracker.process(signal.astype(np.float32))
        except Exception:
            logger.exception("Ошибка при анализе аудио-блока")
            return

        # Система затухания (decay) для RMS
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
        """Запускает аудио поток."""
        if self._stream is not None:
            return

        logger.info("Открываю InputStream для устройства %s...", self.device_name)
        self._stream = sd.InputStream(
            device=self.device_name,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            channels=self.channels,
            callback=self._callback,
        )
        self._stream.start()
        self._started_at = time.monotonic()
        logger.info("Поток запущен для устройства %s", self.device_name)

    def stop(self):
        """Останавливает аудио поток."""
        if self._stream is None:
            return

        logger.info("Останавливаю поток для устройства %s", self.device_name)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        
        self._last_nonzero_rms = 0.0
        self._last_nonzero_rms_time = 0.0
        self._rms_decay_start_time = None

    def is_alive(self, timeout=2.5):
        """Проверяет, активен ли аудио поток.
        
        Callback вызывается постоянно (в т.ч. на тишине), поэтому
        долгое отсутствие вызовов - признак мёртвого потока.
        """
        if self._stream is None or self._started_at is None:
            return False

        now = time.monotonic()

        if self._last_callback_at is None:
            return (now - self._started_at) < timeout

        return (now - self._last_callback_at) < timeout

    def get_latest(self):
        """Потокобезопасно возвращает (rms, pitch) — вызывать из GUI-потока."""
        with self._lock:
            return self._latest_rms, self._latest_pitch

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
