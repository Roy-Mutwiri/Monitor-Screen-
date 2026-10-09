"""Windows process loopback capture (ActivateAudioInterfaceAsync with
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK), implemented with comtypes.

Captures only the audio rendered by the target process tree (what Studio
plays locally). It never captures other apps, never touches routing and
never records to disk; buffers are handed to the analyzer in ~100 ms
chunks as 16 kHz mono PCM16. Requires Windows 10 build 20348+."""
from __future__ import annotations

import ctypes
import platform
import re
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

import numpy as np

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
VT_BLOB = 65
REFTIMES_PER_SEC = 10_000_000


def supported() -> bool:
    m = re.search(r"\d+\.\d+\.(\d+)", platform.version())
    return platform.system() == "Windows" and bool(m) and int(m.group(1)) >= 20348


class _ProcessLoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class _ActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", _ProcessLoopbackParams)]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("wReserved1", wintypes.WORD), ("wReserved2", wintypes.WORD), ("wReserved3", wintypes.WORD),
                ("blob", _BLOB), ("_pad", ctypes.c_ulonglong)]


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]




def _com_interfaces():
    """COM interfaces missing from pycaw: the activation callback/operation pair and IAudioCaptureClient."""
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_void_p
    from ctypes.wintypes import DWORD, UINT

    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(HRESULT), "activateResult"),
                               (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"))]

    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"))]

    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(c_void_p), "ppData"), (["out"], POINTER(UINT), "pNumFramesToRead"),
                      (["out"], POINTER(DWORD), "pdwFlags"), (["out"], POINTER(ctypes.c_ulonglong), "pu64DevicePosition"),
                      (["out"], POINTER(ctypes.c_ulonglong), "pu64QPCPosition")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], UINT, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(UINT), "pNumFramesInNextPacket")),
        ]

    class IAgileObject(IUnknown):
        """Marker interface: the completion handler must be agile or activation fails with E_NOT_VALID_STATE."""
        _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _methods_ = []

    return IActivateAudioInterfaceAsyncOperation, IActivateAudioInterfaceCompletionHandler, IAudioCaptureClient, IAgileObject


class ProcessLoopbackCapture:
    """Minimal capture loop. ``on_pcm(bytes)`` receives 16 kHz mono PCM16 chunks."""

    def __init__(self, pid: int, on_pcm: Callable[[bytes], None], chunk_ms: int = 100, sample_rate: int = 16000) -> None:
        self.pid = pid
        self.on_pcm = on_pcm
        self.chunk_ms = chunk_ms
        self.sr = sample_rate
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""
        self.started = False
        self.frames_total = 0

    # ------------------------------------------------------------------
    def _activate(self):
        import comtypes
        from comtypes import COMObject, GUID
        from pycaw.api.audioclient import IAudioClient
        IActivateAudioInterfaceAsyncOperation, IActivateAudioInterfaceCompletionHandler, _, IAgileObject = _com_interfaces()

        done = threading.Event()
        holder: dict = {}

        class Handler(COMObject):
            _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

            def ActivateCompleted(self, this, op):
                try:
                    holder["op"] = op
                finally:
                    done.set()
                return 0

        params = _ActivationParams()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = self.pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        pv = _PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)
        mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
        fn = mmdevapi.ActivateAudioInterfaceAsync
        fn.restype = ctypes.HRESULT
        handler = Handler()
        handler_ptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
        op = ctypes.POINTER(IActivateAudioInterfaceAsyncOperation)()
        iid = GUID(str(IAudioClient._iid_))
        fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID), ctypes.POINTER(_PROPVARIANT),
                       ctypes.POINTER(IActivateAudioInterfaceCompletionHandler), ctypes.POINTER(ctypes.POINTER(IActivateAudioInterfaceAsyncOperation))]
        hr = fn(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(iid), ctypes.byref(pv), handler_ptr, ctypes.byref(op))
        if hr != 0:
            raise OSError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08X}")
        if not done.wait(5.0):
            raise TimeoutError("ActivateAudioInterfaceAsync did not complete")
        async_op = holder["op"]
        hr_act, unk = async_op.GetActivateResult()
        if int(hr_act) != 0:
            raise OSError(f"activation result 0x{int(hr_act) & 0xFFFFFFFF:08X}")
        return unk.QueryInterface(IAudioClient)

    def _run(self) -> None:
        import comtypes
        try:
            comtypes.CoInitializeEx(0)      # MTA, like the Microsoft sample
        except OSError:
            comtypes.CoInitialize()         # thread already initialised in another mode
        try:
            _, _, IAudioCaptureClient, _agile = _com_interfaces()
            client = self._activate()
            from pycaw.api.audioclient import WAVEFORMATEX
            fmt = WAVEFORMATEX()
            fmt.wFormatTag, fmt.nChannels, fmt.nSamplesPerSec = 1, 1, self.sr          # PCM16 mono, auto-converted by WASAPI
            fmt.nAvgBytesPerSec, fmt.nBlockAlign, fmt.wBitsPerSample, fmt.cbSize = self.sr * 2, 2, 16, 0
            flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, REFTIMES_PER_SEC, 0, ctypes.pointer(fmt), None)
            cap = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
            client.Start()
            self.started = True
            chunk = bytearray()
            chunk_bytes = int(self.sr * self.chunk_ms / 1000) * 2
            while not self._stop.is_set():
                time.sleep(self.chunk_ms / 2000)
                while True:
                    n = cap.GetNextPacketSize()
                    if not n:
                        break
                    data_ptr, frames, dflags, _dp, _qp = cap.GetBuffer()
                    count = int(frames)
                    if count and data_ptr and not (int(dflags) & 2):            # AUDCLNT_BUFFERFLAGS_SILENT == 2
                        chunk += ctypes.string_at(data_ptr, count * 2)
                    elif count:
                        chunk += bytes(count * 2)
                    cap.ReleaseBuffer(count)
                    self.frames_total += count
                while len(chunk) >= chunk_bytes:
                    self.on_pcm(bytes(chunk[:chunk_bytes]))
                    del chunk[:chunk_bytes]
            client.Stop()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            comtypes.CoUninitialize()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="studio-audio-loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


def probe(pid: int, seconds: float = 1.0) -> tuple[bool, str]:
    """Try to activate process loopback for ``pid`` briefly. ``ok`` only when the stream actually delivers frames
    (a process without a render stream activates fine but delivers nothing). (ok, note)."""
    if not supported():
        return False, "process loopback needs Windows 10 build 20348+"
    got = {"chunks": 0}
    cap = ProcessLoopbackCapture(pid, lambda b: got.__setitem__("chunks", got["chunks"] + 1))
    cap.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds + 2 and not cap.started and not cap.error:
        time.sleep(0.05)
    time.sleep(seconds)
    cap.stop()
    if cap.error:
        return False, cap.error
    if not cap.started:
        return False, "activation did not complete"
    note = f"activated; {got['chunks']} chunks / {cap.frames_total} frames in {seconds:.0f} s"
    return cap.frames_total > 0, note + ("" if cap.frames_total else " (no render stream in this process)")
