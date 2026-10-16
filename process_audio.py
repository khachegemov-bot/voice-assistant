"""Запись звука одной программы (и её дочерних процессов) — Windows 10 2004+ / Windows 11.

Используется «process loopback» из WASAPI: ActivateAudioInterfaceAsync с устройством
VAD\\Process_Loopback. Звук берётся до смешивания с остальными программами, поэтому
в запись попадает только выбранное приложение.
"""
import ctypes
import threading
from ctypes import POINTER, byref, c_int, c_uint32, c_uint64, c_void_p, sizeof
from ctypes import wintypes

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, COMObject

# ─── COM-интерфейсы ───


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(POINTER(ctypes.c_byte)), "data"),
                  (["out"], POINTER(c_uint32), "frames"), (["out"], POINTER(c_uint32), "flags"),
                  (["out"], POINTER(c_uint64), "devpos"), (["out"], POINTER(c_uint64), "qpcpos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_int, "mode"), (["in"], wintypes.DWORD, "flags"),
                  (["in"], ctypes.c_longlong, "buffer"), (["in"], ctypes.c_longlong, "period"),
                  (["in"], POINTER(WAVEFORMATEX), "fmt"), (["in"], POINTER(GUID), "session")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "frames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "lat")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pad")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "mode"), (["in"], POINTER(WAVEFORMATEX), "fmt"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "closest")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "fmt")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong), "d"),
                  (["out"], POINTER(ctypes.c_longlong), "m")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "h")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "iid"), (["out"], POINTER(c_void_p), "obj")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(HRESULT), "hr"),
                  (["out"], POINTER(POINTER(IUnknown)), "iface")),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class _Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()
        self.result = None
        self.error = None

    def ActivateCompleted(self, op):
        try:
            hr, iface = op.GetActivateResult()
            if hr < 0:
                self.error = f"HRESULT {hr & 0xFFFFFFFF:#010x}"
            else:
                self.result = iface.QueryInterface(IAudioClient)
        except Exception as e:      # noqa: BLE001
            self.error = str(e)
        self.done.set()
        return 0


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                ("blob", _BLOB)]


class _ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", c_int), ("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


_mmdev = ctypes.WinDLL("Mmdevapi.dll")
_mmdev.ActivateAudioInterfaceAsync.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(_PROPVARIANT),
                                                POINTER(IActivateAudioInterfaceCompletionHandler),
                                                POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
_mmdev.ActivateAudioInterfaceAsync.restype = HRESULT


_kernel32 = ctypes.windll.kernel32


class ProcessCapture:
    """with ProcessCapture(pid) as cap:  cap.read() -> np.float32 моно 16 кГц (может быть пустым).

    Windows держит для этого режима всего ~10 мс звука, поэтому звук забирает отдельный поток,
    который просыпается по сигналу Windows, как только порция готова (иначе куски теряются).
    """
    RATE = 16000

    def __init__(self, pid: int):
        self.pid = pid
        self.client = self.capture = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._running = False
        self._ready = threading.Event()
        self._error: Exception | None = None
        self.lost = 0                                     # сколько раз Windows сообщила о потере звука

    def __enter__(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="process-audio")
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise self._error
        if not self.client:
            raise RuntimeError("Windows не ответила на запрос записи звука программы")
        return self

    def _loop(self):
        try:
            self._open()
        except Exception as e:      # noqa: BLE001
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        while self._running:
            _kernel32.WaitForSingleObject(self._event, 200)   # ждём сигнала, не держа Python
            try:
                self._drain()
            except Exception:       # noqa: BLE001 — устройство пропало и т.п.
                break
        try:
            self.client.Stop()
        except Exception:           # noqa: BLE001
            pass

    def _drain(self):
        while True:
            n = self.capture.GetNextPacketSize()
            if not n:
                return
            data, frames, flags, _, _ = self.capture.GetBuffer()
            if frames:
                if flags & 0x1:                           # DATA_DISCONTINUITY
                    self.lost += 1
                if flags & 0x2:                           # SILENT
                    block = np.zeros(frames, np.float32)
                else:
                    block = np.frombuffer(ctypes.string_at(data, frames * 2), np.int16).astype(np.float32) / 32768
                with self._lock:
                    self._chunks.append(block)
            self.capture.ReleaseBuffer(frames)

    def _open(self):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass                                          # поток уже подготовлен к COM — это нормально
        params = _ACTIVATION_PARAMS(1, self.pid, 0)       # PROCESS_LOOPBACK, включая дочерние процессы
        pv = _PROPVARIANT()
        pv.vt = 65                                        # VT_BLOB
        pv.blob.cbSize = sizeof(params)
        pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)
        handler = _Handler()
        op = POINTER(IActivateAudioInterfaceAsyncOperation)()
        _mmdev.ActivateAudioInterfaceAsync("VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(pv),
                                           handler.QueryInterface(IActivateAudioInterfaceCompletionHandler),
                                           byref(op))
        if not handler.done.wait(5):
            raise RuntimeError("Windows не ответила на запрос записи звука программы")
        if handler.error:
            raise RuntimeError(f"Не удалось подключиться к звуку программы: {handler.error}")
        client = handler.result
        fmt = WAVEFORMATEX(1, 1, self.RATE, self.RATE * 2, 2, 16, 0)      # PCM 16 бит, моно, 16 кГц
        # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY — Windows сама приведёт звук к формату
        client.Initialize(0, 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000, 2_000_000, 0, byref(fmt), None)
        self._event = _kernel32.CreateEventW(None, False, False, None)
        client.SetEventHandle(self._event)
        ptr = client.GetService(byref(IAudioCaptureClient._iid_))
        self.capture = ctypes.cast(ptr, POINTER(IAudioCaptureClient))
        client.Start()
        self.client = client

    def read(self) -> np.ndarray:
        with self._lock:
            parts, self._chunks = self._chunks, []
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def alive(self) -> bool:
        return self._thread.is_alive()

    def __exit__(self, *exc):
        self._running = False
        self._thread.join(2)
        self.capture = self.client = None


def _capture_worker(pid: int, out, stop):
    """Выполняется в ОТДЕЛЬНОМ процессе: там свой Python, и тяжёлая работа главной программы
    (загрузка моделей, распознавание) не мешает вовремя забирать звук из 10-мс буфера Windows."""
    import time
    try:
        with ProcessCapture(pid) as cap:
            out.put(("ok", None))
            while not stop.is_set() and cap.alive():
                time.sleep(0.05)
                block = cap.read()
                if len(block):
                    out.put(("audio", block.tobytes()))
            out.put(("lost", cap.lost))
    except Exception as e:          # noqa: BLE001
        out.put(("error", str(e)))
    out.put(("end", None))


class IsolatedProcessCapture:
    """То же, что ProcessCapture, но запись идёт в отдельном процессе.
    with IsolatedProcessCapture(pid) as cap: cap.read() -> np.float32; cap.alive(); cap.lost"""

    def __init__(self, pid: int):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        self._q = ctx.Queue()
        self._stop = ctx.Event()
        self._proc = ctx.Process(target=_capture_worker, args=(pid, self._q, self._stop), daemon=True,
                                 name="process-audio")
        self._ended = False
        self.lost = 0
        self._pending: list[np.ndarray] = []

    def __enter__(self):
        import queue as _queue
        self._proc.start()
        try:
            kind, val = self._q.get(timeout=20)
        except _queue.Empty:
            self._proc.terminate()
            raise RuntimeError("процесс записи звука не запустился") from None
        if kind == "error":
            raise RuntimeError(val)
        return self

    def _pump(self):
        import queue as _queue
        while True:
            try:
                kind, val = self._q.get_nowait()
            except _queue.Empty:
                return
            if kind == "audio":
                self._pending.append(np.frombuffer(val, np.float32))
            elif kind == "lost":
                self.lost = val
            elif kind == "error":
                self._ended = True
                raise RuntimeError(val)
            elif kind == "end":
                self._ended = True

    def read(self) -> np.ndarray:
        self._pump()
        parts, self._pending = self._pending, []
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def alive(self) -> bool:
        return not self._ended and self._proc.is_alive()

    def __exit__(self, *exc):
        self._stop.set()
        self._proc.join(3)
        if self._proc.is_alive():
            self._proc.terminate()
        try:
            self._pump()
        except RuntimeError:
            pass


def set_app_volume(pid: int, volume: float):
    """Громкость звукового сеанса программы (0..1). Возвращает прежнюю громкость или None."""
    from pycaw.pycaw import AudioUtilities
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    for s in AudioUtilities.GetAllSessions():
        if s.Process and s.Process.pid == pid:
            old = s.SimpleAudioVolume.GetMasterVolume()
            s.SimpleAudioVolume.SetMasterVolume(float(volume), None)
            return old
    return None


def restore_app_volume(name: str, volume: float) -> bool:
    """Вернуть громкость всем звуковым сеансам программы с таким именем (после аварийного выхода)."""
    from pycaw.pycaw import AudioUtilities
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    done = False
    for s in AudioUtilities.GetAllSessions():
        if s.Process and s.Process.name().lower() == name.lower():
            s.SimpleAudioVolume.SetMasterVolume(float(volume), None)
            done = True
    return done


def audio_programs() -> list[tuple[str, int]]:
    """Программы, у которых сейчас есть звуковой сеанс: [(имя, pid)], без повторов."""
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    seen, out = set(), []
    for s in AudioUtilities.GetAllSessions():
        p = s.Process
        if not p:
            continue
        name = p.name()
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append((name, p.pid))
    return sorted(out, key=lambda x: x[0].lower())
