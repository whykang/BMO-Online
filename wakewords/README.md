# 唤醒词模型库

把 `.onnx` 唤醒词模型扔进这个文件夹，在 Web 控制台的「唤醒词」标签里就能下拉切换。

## 默认会下载的几个

`setup_pi.sh` 会自动下载这几个 OpenWakeWord 官方模型：

| 文件 | 唤醒词 |
|------|--------|
| `hey_jarvis.onnx` | "Hey Jarvis" |
| `alexa.onnx` | "Alexa" |
| `hey_mycroft.onnx` | "Hey Mycroft" |

## 训练自己的唤醒词

中文 / "嘿 BMO" 等自定义唤醒词需要自己训练：

1. 打开 OpenWakeWord 官方 Colab：
   https://github.com/dscripka/openWakeWord
2. 填入你的唤醒词文字
3. Colab 用 TTS 自动合成几千条训练样本（不用自己录音）
4. 训 30~60 分钟
5. 下载生成的 `.onnx`
6. 在 BMO Web 控制台「唤醒词」标签里上传

完整教程：https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb
