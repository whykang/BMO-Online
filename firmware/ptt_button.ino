// =========================================================================
//  BMO PTT 按钮固件
//  适配：Arduino Pro Micro / Adafruit Feather 32u4 / Arduino Leonardo
//        （任何 ATmega32u4 板子）
//
//  功能：
//    - 短按（<400ms）→ 发送回车键    → BMO 切换录音状态（PTT toggle）
//    - 长按（>=400ms 按住）→ 发送空格 → 打断 BMO 说话
//
//  接线：
//    开关一端 → D9 引脚
//    开关另一端 → GND
//    （不需要外接电阻，启用了内部上拉）
//
//  烧录：Arduino IDE → 选 "Adafruit Feather 32u4" 或 "Arduino Leonardo"
// =========================================================================

#include <Keyboard.h>

const int BUTTON_PIN = 9;
const unsigned long DEBOUNCE_MS = 30;
const unsigned long LONG_PRESS_MS = 400;

bool lastStableState = HIGH;       // 上一个稳定状态
bool lastReadState = HIGH;
unsigned long lastChangeTime = 0;
unsigned long pressStartTime = 0;
bool longPressFired = false;       // 长按触发后只发一次

void setup() {
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);
  Keyboard.begin();
}

void loop() {
  bool raw = digitalRead(BUTTON_PIN);

  // 消抖：状态变化时记录时间
  if (raw != lastReadState) {
    lastChangeTime = millis();
    lastReadState = raw;
  }

  // 稳定时间够了，更新稳定状态
  if (millis() - lastChangeTime > DEBOUNCE_MS) {
    if (raw != lastStableState) {
      lastStableState = raw;

      if (lastStableState == LOW) {
        // 刚按下
        pressStartTime = millis();
        longPressFired = false;
        digitalWrite(LED_BUILTIN, HIGH);
      } else {
        // 刚松开
        digitalWrite(LED_BUILTIN, LOW);
        unsigned long dur = millis() - pressStartTime;
        if (!longPressFired && dur < LONG_PRESS_MS) {
          // 短按 → 回车
          Keyboard.write(KEY_RETURN);
        }
      }
    }
  }

  // 长按检测：按住超过 LONG_PRESS_MS，立刻发空格（不等松开）
  if (lastStableState == LOW && !longPressFired) {
    if (millis() - pressStartTime >= LONG_PRESS_MS) {
      Keyboard.write(' ');
      longPressFired = true;
      // 长按时 LED 闪一下提示
      digitalWrite(LED_BUILTIN, LOW);
      delay(50);
      digitalWrite(LED_BUILTIN, HIGH);
    }
  }

  delay(5);
}
