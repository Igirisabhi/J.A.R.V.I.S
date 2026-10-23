"""Open the Windows default input device for all Jarvis microphone capture."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import numpy as np
import sounddevice as sd


LOGGER = logging.getLogger(__name__)


def list_input_devices() -> list[dict]:
    """Return safe, user-facing metadata for available input devices."""
    try:
        devices = sd.query_devices()
    except sd.PortAudioError as error:
        LOGGER.warning("Unable to enumerate microphones: %s", error)
        return []
    result = []
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) <= 0:
            continue
        result.append({
            "index": index,
            "name": str(device.get("name", "")).strip() or f"Input device {index}",
            "channels": int(device.get("max_input_channels", 0)),
            "default_samplerate": float(device.get("default_samplerate", 0) or 0),
        })
    return result


def _native_rate_options(kwargs: dict) -> dict | None:
    """Retry the system default at its native rate and resample for the caller."""
    callback = kwargs.get("callback")
    requested_rate = kwargs.get("samplerate")
    if callback is None or not requested_rate:
        return None
    try:
        native_rate = float(sd.query_devices(kind="input")["default_samplerate"])
    except (sd.PortAudioError, KeyError, TypeError, ValueError):
        return None
    if abs(native_rate - float(requested_rate)) < 1:
        return None

    options = dict(kwargs)
    options["samplerate"] = native_rate
    blocksize = options.get("blocksize", 0)
    if blocksize:
        options["blocksize"] = max(1, round(blocksize * native_rate / float(requested_rate)))

    def resample_callback(indata, _frames, time_info, status):
        source = np.asarray(indata)
        target_frames = max(1, round(source.shape[0] * float(requested_rate) / native_rate))
        positions = np.linspace(0, source.shape[0] - 1, target_frames)
        converted = np.column_stack([
            np.interp(positions, np.arange(source.shape[0]), source[:, channel])
            for channel in range(source.shape[1])
        ])
        if np.issubdtype(source.dtype, np.integer):
            limits = np.iinfo(source.dtype)
            converted = np.rint(converted).clip(limits.min, limits.max)
        converted = converted.astype(source.dtype, copy=False)
        callback(converted, target_frames, time_info, status)

    options["callback"] = resample_callback
    return options


def start_input_stream(**kwargs):
    """Open only the system default microphone, retrying at its native rate if needed."""
    # Callers from older versions may still pass a saved device index. Ignore it
    # so a stale preference can never override the Windows default microphone.
    kwargs.pop("device", None)
    stream = None
    try:
        from core.debug_logger import log_voice_pipeline
        log_voice_pipeline("MIC", "Stream opening", samplerate=kwargs.get("samplerate"), channels=kwargs.get("channels"))
    except ImportError:
        pass
    try:
        stream = sd.InputStream(device=None, **kwargs)
        stream.start()
        try:
            from core.debug_logger import log_voice_pipeline
            log_voice_pipeline("MIC", "Stream opened successfully", samplerate=kwargs.get("samplerate"), channels=kwargs.get("channels"))
        except ImportError:
            pass
        return stream
    except sd.PortAudioError as first_error:
        if stream is not None:
            try:
                stream.close()
            except sd.PortAudioError as close_error:
                LOGGER.debug("Could not close failed microphone stream: %s", close_error)

        native_options = _native_rate_options(kwargs)
        if native_options is None:
            raise first_error

        stream = sd.InputStream(device=None, **native_options)
        stream.start()
        LOGGER.info(
            "Using the Windows default microphone at its native sample rate: %s",
            native_options.get("samplerate"),
        )
        try:
            from core.debug_logger import log_voice_pipeline
            log_voice_pipeline("MIC", "Stream opened at native sample rate", native_rate=native_options.get("samplerate"))
        except ImportError:
            pass
        return stream


@contextmanager
def input_stream(**kwargs):
    """Open a default-input stream and release it when the caller is done."""
    stream = start_input_stream(**kwargs)
    try:
        yield stream
    finally:
        try:
            stream.stop()
        except sd.PortAudioError as error:
            LOGGER.debug("Could not stop microphone stream cleanly: %s", error)
        finally:
            try:
                stream.close()
                try:
                    from core.debug_logger import log_voice_pipeline
                    log_voice_pipeline("MIC", "Stream closed")
                except ImportError:
                    pass
            except sd.PortAudioError as error:
                LOGGER.debug("Could not close microphone stream cleanly: %s", error)
