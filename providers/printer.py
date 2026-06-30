"""热敏打印机 provider —— ESC/POS over GPIO UART。

达普热敏打印机：树莓派 5 通过 /dev/ttyAMA0，9600 波特率、GB2312 编码、Raw 模式。
支持打印中文文字、图片（光栅位图 GS v 0）、ESC/POS 指令。
默认 58mm 纸（384 点宽）；80mm 纸把 config.printer.width 改成 576。
"""
import os
import time

try:
    import serial  # pip install pyserial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

import numpy as np
from PIL import Image

ESC = b"\x1b"
GS = b"\x1d"


class ThermalPrinter:
    def __init__(self, config: dict):
        if not SERIAL_AVAILABLE:
            raise RuntimeError("缺少 pyserial：pip install pyserial")
        # Pi 5 的 GPIO 排针(8/10脚) UART 是 /dev/ttyAMA0（注意 serial0 在 Pi 5 上
        # 指向调试口 ttyAMA10，不是排针）。找不到再在候选里试别的。
        self.device = config.get("device", "/dev/ttyAMA0")
        self.baudrate = int(config.get("baudrate", 9600))
        self.encoding = config.get("encoding", "gb2312")
        # 点宽取 8 的整数倍（光栅按字节打包）；58mm=384，80mm=576
        self.width = (int(config.get("width", 384)) // 8) * 8
        self.ser = None

    def _resolve_device(self) -> str | None:
        """配置的设备不存在时，在常见 UART 候选里挑第一个存在的。
        覆盖 Pi 5 / Pi 4 / USB 转串口几种情况。"""
        # ttyAMA0 在前(Pi 5 GPIO 排针)，serial0/ttyS0 兜底(Pi 4 等)
        candidates = [self.device, "/dev/ttyAMA0", "/dev/serial0",
                      "/dev/ttyS0", "/dev/ttyUSB0", "/dev/ttyAMA10"]
        seen, ordered = set(), []
        for d in candidates:
            if d and d not in seen:
                seen.add(d)
                ordered.append(d)
        for d in ordered:
            if os.path.exists(d):
                return d
        return None

    # ---- 连接 ----
    def _open(self):
        if self.ser is None or not self.ser.is_open:
            dev = self._resolve_device()
            if not dev:
                raise RuntimeError(
                    "找不到串口设备（/dev/serial0、/dev/ttyAMA0 等都不存在）。"
                    "多半是 UART 没启用：在树莓派上跑 sudo raspi-config → "
                    "Interface Options → Serial Port → 登录shell选「否」、硬件串口选「是」，"
                    "然后 sudo reboot；或者打印机没接好。不用打印可在网页把打印功能关掉。"
                )
            # write_timeout 给足：9600 波特下大图也要十几秒
            self.ser = serial.Serial(dev, self.baudrate,
                                     timeout=5, write_timeout=120)
            self.ser.write(ESC + b"@")   # 初始化
            self.ser.flush()
            time.sleep(0.05)
        return self.ser

    def close(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _write_chunked(self, ser, data: bytes, chunk: int = 512):
        for i in range(0, len(data), chunk):
            ser.write(data[i:i + chunk])
            ser.flush()

    # ---- 文字 ----
    def text(self, s: str):
        ser = self._open()
        data = (s.rstrip("\n") + "\n").encode(self.encoding, errors="replace")
        self._write_chunked(ser, data)

    def feed(self, lines: int = 3):
        ser = self._open()
        ser.write(b"\n" * lines)
        ser.flush()

    # ---- 图片（光栅位图 GS v 0）----
    def image(self, path: str):
        ser = self._open()
        img = Image.open(path).convert("L")
        w = self.width
        h = max(1, int(round(img.height * (w / img.width))))
        img = img.resize((w, h))
        # 抖动成 1-bit，再回到 0/255 灰度判黑白（< 128 = 黑点 = bit 1）
        arr = np.array(img.convert("1").convert("L"))
        bits = (arr < 128).astype(np.uint8)
        packed = np.packbits(bits, axis=1)          # MSB 在左，正好对 ESC/POS
        bytes_per_row = packed.shape[1]
        raster = packed.tobytes()

        xL, xH = bytes_per_row & 0xFF, (bytes_per_row >> 8) & 0xFF
        yL, yH = h & 0xFF, (h >> 8) & 0xFF
        ser.write(GS + b"v0" + bytes([0, xL, xH, yL, yH]))
        ser.flush()
        self._write_chunked(ser, raster)

    # ---- 二维码（部分机型支持 ESC/POS 原生 QR；不支持时可改成图片方式）----
    def qrcode(self, data: str, size: int = 6):
        ser = self._open()
        d = data.encode(self.encoding, errors="replace")
        n = len(d) + 3
        pL, pH = n & 0xFF, (n >> 8) & 0xFF
        ser.write(GS + b"(k\x04\x00\x31\x41\x32\x00")          # model 2
        ser.write(GS + b"(k\x03\x00\x31\x43" + bytes([size]))  # 模块大小
        ser.write(GS + b"(k\x03\x00\x31\x45\x30")              # 纠错
        ser.write(GS + b"(k" + bytes([pL, pH]) + b"\x31\x50\x30" + d)  # 存数据
        ser.write(GS + b"(k\x03\x00\x31\x51\x30")              # 打印
        ser.flush()

    @staticmethod
    def is_available() -> bool:
        return SERIAL_AVAILABLE
