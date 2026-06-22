# BMO PTT 按钮固件

烧录到 Arduino Pro Micro / Adafruit Feather 32u4 / Arduino Leonardo
（任何 ATmega32u4 芯片的板子）。

## 接线
| 开关引脚 | Arduino 引脚 |
|---------|-------------|
| 一端 | **D9** |
| 另一端 | **GND** |

不需要外接电阻（代码用了 `INPUT_PULLUP`）。

## 按键映射

| 操作 | 模拟键盘 | BMO 行为 |
|------|---------|---------|
| **短按**（<400ms） | 回车 `Enter` | 开始录音 / 停止录音（toggle） |
| **长按**（按住 ≥400ms） | 空格 `Space` | 打断 BMO 说话 |

## 烧录步骤

1. 装 Arduino IDE：<https://arduino.cc/en/software>
2. 加 Adafruit 开发板源（如果用 Feather 32u4）：
   - `File → Preferences → Additional Boards Manager URLs`
   - 填：`https://adafruit.github.io/arduino-board-index/package_adafruit_index.json`
3. `Tools → Board → Boards Manager` 搜 **Adafruit AVR Boards** → Install
4. 接线并通过 USB 接到电脑
5. `Tools → Board` 选 `Adafruit Feather 32u4` 或 `Arduino Leonardo`
6. `Tools → Port` 选板子对应的端口
7. 打开 `ptt_button.ino` → 点 Upload

## 救命指南：烧坏了怎么办

如果代码写错导致板子狂发按键，电脑没法操作：

1. 拔下 USB
2. **按住板子上的 RESET 按钮不放**
3. 插回 USB，**继续按住 5 秒**
4. 这 5 秒内板子停在 bootloader，不发键盘事件
5. 用 Arduino IDE 上传正确的代码
