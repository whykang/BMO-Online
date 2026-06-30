"""手柄 → 键盘 转换。

检测到游戏手柄时，把手柄按钮映射成 FCEUX 已配置的那套键盘键
（config.game.keymap），这样不用碰 FCEUX 的手柄设置就能用手柄玩。
没检测到手柄就什么都不做，键盘照旧。

实现：python-evdev 读手柄事件 → uinput 注入对应键盘按键。
"""
import threading

try:
    import evdev
    from evdev import ecodes, InputDevice, UInput, list_devices
    EVDEV_AVAILABLE = True
except Exception:
    EVDEV_AVAILABLE = False


def _log(msg):
    print(f"[GAMEPAD] {msg}", flush=True)


def _fceux_key_to_evdev(name):
    """FCEUX 键名(kSpace/kShift/kS/kReturn/kUp...) → evdev KEY 码。不认识返回 None。"""
    if not name or not EVDEV_AVAILABLE:
        return None
    s = str(name).strip()
    if s[:1] in ("k", "K"):
        s = s[1:]
    low = s.lower()
    special = {
        "space": "KEY_SPACE", "shift": "KEY_LEFTSHIFT", "lshift": "KEY_LEFTSHIFT",
        "rshift": "KEY_RIGHTSHIFT", "return": "KEY_ENTER", "enter": "KEY_ENTER",
        "tab": "KEY_TAB", "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT",
        "right": "KEY_RIGHT", "ctrl": "KEY_LEFTCTRL", "lctrl": "KEY_LEFTCTRL",
        "alt": "KEY_LEFTALT", "esc": "KEY_ESC", "escape": "KEY_ESC",
        "backspace": "KEY_BACKSPACE",
    }
    keyname = special.get(low)
    if keyname is None and len(low) == 1 and low.isalnum():
        keyname = f"KEY_{low.upper()}"
    if keyname is None:
        return None
    return getattr(ecodes, keyname, None)


class GamepadKeyboard:
    def __init__(self, keymap: dict):
        # NES 按钮 → evdev KEY 码（来自 config.game.keymap）
        self.nes_key = {}
        for nes, fk in (keymap or {}).items():
            code = _fceux_key_to_evdev(fk)
            if code is not None:
                self.nes_key[nes] = code
        self._dev = None
        self._ui = None
        self._thread = None
        self._running = False
        self._src = {}     # nes -> set(当前按住该 NES 键的物理来源)
        self._abs = {}

    @staticmethod
    def available():
        return EVDEV_AVAILABLE

    @staticmethod
    def find_gamepad():
        """找第一个像手柄的输入设备（有 BTN_GAMEPAD/BTN_SOUTH 等）。"""
        if not EVDEV_AVAILABLE:
            return None
        for path in list_devices():
            try:
                d = InputDevice(path)
            except Exception:
                continue
            try:
                keys = d.capabilities().get(ecodes.EV_KEY, [])
                if any(b in keys for b in (ecodes.BTN_GAMEPAD, ecodes.BTN_SOUTH, ecodes.BTN_A)):
                    return d
            except Exception:
                pass
            try:
                d.close()
            except Exception:
                pass
        return None

    def start(self) -> bool:
        """有手柄且能创建 uinput 才返回 True；否则 False（调用方据此回落键盘）。"""
        if not EVDEV_AVAILABLE or not self.nes_key:
            return False
        dev = self.find_gamepad()
        if dev is None:
            return False
        try:
            self._ui = UInput(events={ecodes.EV_KEY: sorted(set(self.nes_key.values()))})
        except Exception as e:
            _log(f"创建 uinput 失败(多半是 /dev/uinput 权限)：{e}")
            try:
                dev.close()
            except Exception:
                pass
            return False
        self._dev = dev
        try:
            for code in (ecodes.ABS_X, ecodes.ABS_Y):
                self._abs[code] = dev.absinfo(code)
        except Exception:
            pass
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log(f"已接管手柄：{dev.name}")
        return True

    def _set(self, nes, source, active):
        """把某个 NES 键的某个物理来源置为按下/松开；多来源用集合做 OR。"""
        code = self.nes_key.get(nes)
        if code is None:
            return
        s = self._src.setdefault(nes, set())
        was = bool(s)
        if active:
            s.add(source)
        else:
            s.discard(source)
        now = bool(s)
        if now != was:
            try:
                self._ui.write(ecodes.EV_KEY, code, 1 if now else 0)
                self._ui.syn()
            except Exception:
                pass

    def _loop(self):
        # 手柄物理按钮 → NES 键（A/X→A，B/Y→B；十字键和左摇杆→方向）
        btn = {
            ecodes.BTN_SOUTH: "a", ecodes.BTN_WEST: "a",
            ecodes.BTN_EAST: "b", ecodes.BTN_NORTH: "b",
            ecodes.BTN_START: "start", ecodes.BTN_SELECT: "select",
        }
        try:
            for ev in self._dev.read_loop():
                if not self._running:
                    break
                if ev.type == ecodes.EV_KEY:
                    nes = btn.get(ev.code)
                    if nes:
                        self._set(nes, ("btn", ev.code), ev.value != 0)
                elif ev.type == ecodes.EV_ABS:
                    if ev.code == ecodes.ABS_HAT0X:
                        self._set("left", "hatx", ev.value < 0)
                        self._set("right", "hatx", ev.value > 0)
                    elif ev.code == ecodes.ABS_HAT0Y:
                        self._set("up", "haty", ev.value < 0)
                        self._set("down", "haty", ev.value > 0)
                    elif ev.code in (ecodes.ABS_X, ecodes.ABS_Y):
                        self._stick(ev.code, ev.value)
        except Exception as e:
            if self._running:
                _log(f"读手柄结束：{e}")

    def _stick(self, code, value):
        info = self._abs.get(code)
        if not info or (info.max - info.min) <= 0:
            return
        center = (info.max + info.min) / 2
        dz = (info.max - info.min) * 0.35
        if code == ecodes.ABS_X:
            self._set("left", "stickx", value < center - dz)
            self._set("right", "stickx", value > center + dz)
        else:
            self._set("up", "sticky", value < center - dz)
            self._set("down", "sticky", value > center + dz)

    def stop(self):
        self._running = False
        # 松开所有按住的键，免得游戏里方向卡住
        for nes, code in self.nes_key.items():
            if self._src.get(nes):
                try:
                    self._ui.write(ecodes.EV_KEY, code, 0)
                    self._ui.syn()
                except Exception:
                    pass
        self._src.clear()
        try:
            if self._dev:
                self._dev.close()
        except Exception:
            pass
        try:
            if self._ui:
                self._ui.close()
        except Exception:
            pass
        self._dev = None
        self._ui = None
