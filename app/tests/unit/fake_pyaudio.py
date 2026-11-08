"""A scripted stand-in for the `pyaudiowpatch` module (no audio hardware).

Device dictionaries mirror PyAudioWPatch's `_make_device_info_dictionary` keys. Streams
record how they were opened and let a test invoke the native callback directly.
"""

from __future__ import annotations

from types import SimpleNamespace

paWASAPI = 13
paFloat32 = 1
paContinue = 0
paComplete = 1
paInputOverflow = 2


def device(index, name, *, inputs=0, outputs=0, rate=48_000.0, loopback=False, host_api=2):
    return {
        "index": index, "structVersion": 2, "name": name, "hostApi": host_api,
        "maxInputChannels": inputs, "maxOutputChannels": outputs,
        "defaultLowInputLatency": 0.003, "defaultLowOutputLatency": 0.003,
        "defaultHighInputLatency": 0.01, "defaultHighOutputLatency": 0.01,
        "defaultSampleRate": rate, "isLoopbackDevice": loopback,
    }


# Shaped like a real Windows laptop: an MME duplicate, a Bluetooth headset with a
# 16 kHz mono mic, a 2-channel array mic, speakers and their loopback analogues.
DEFAULT_DEVICES = [
    device(0, "Microphone Array (AMD Audio De", inputs=2, host_api=0),  # MME duplicate
    device(14, "Speakers (Realtek(R) Audio)", outputs=2),
    device(15, "Headset Earphone (Poly BT700)", outputs=2),
    device(16, "Headset Microphone (Poly BT700)", inputs=1, rate=16_000.0),
    device(17, "Microphone Array (AMD Audio Device)", inputs=2),
    device(18, "Speakers (Realtek(R) Audio) [Loopback]", inputs=2, loopback=True),
    device(19, "Headset Earphone (Poly BT700) [Loopback]", inputs=2, loopback=True),
]


class FakeStream:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self.active = False

    @property
    def callback(self):
        return self.kwargs["stream_callback"]

    def start_stream(self):
        self.started = self.active = True

    def stop_stream(self):
        self.stopped = True
        self.active = False

    def close(self):
        self.closed = True
        self.active = False

    def is_active(self):
        return self.active


class FakePyAudio:
    def __init__(self, module):
        self.module = module
        self.terminated = False
        module.instances.append(self)

    def get_host_api_info_by_type(self, host_api_type):
        if not self.module.wasapi_available:
            raise OSError("WASAPI unavailable")
        return {"index": 2, "deviceCount": len(self.module.devices),
                "defaultInputDevice": self.module.default_input, "defaultOutputDevice": self.module.default_output}

    def get_device_info_generator_by_host_api(self, *, host_api_index=None, host_api_type=None):
        return iter([d for d in self.module.devices if d["hostApi"] == 2])

    def is_format_supported(self, rate, input_device=None, input_channels=None, input_format=None, **_):
        self.module.format_checks.append((rate, input_device, input_channels, input_format))
        if input_device in self.module.refuse_format:
            raise ValueError(("Invalid sample rate", -9997))
        return True

    def open(self, **kwargs):
        if kwargs.get("input_device_index") in self.module.refuse_open:
            raise OSError(-9985, "Device unavailable")
        stream = FakeStream(kwargs)
        self.module.streams.append(stream)
        return stream

    def terminate(self):
        self.terminated = True


def make_module(devices=None, *, default_input=17, default_output=14):
    module = SimpleNamespace(
        paWASAPI=paWASAPI, paFloat32=paFloat32, paContinue=paContinue, paComplete=paComplete,
        paInputOverflow=paInputOverflow,
        devices=list(DEFAULT_DEVICES if devices is None else devices),
        default_input=default_input, default_output=default_output,
        wasapi_available=True, refuse_format=set(), refuse_open=set(),
        instances=[], streams=[], format_checks=[],
    )
    module.PyAudio = lambda: FakePyAudio(module)
    return module
