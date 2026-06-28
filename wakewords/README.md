# 唤醒词模型库

把 `.onnx` 唤醒词模型扔进这个文件夹，在 Web 控制台的「唤醒词」标签里就能下拉切换。

## 自带的模型

仓库只自带一个 OpenWakeWord 模型：

| 文件 | 唤醒词 |
|------|--------|
| `hey_bmo.onnx` | "Hey BMO" |

想要更多英文唤醒词（如 Alexa / Hey Mycroft 等），到 OpenWakeWord 官方
[releases](https://github.com/dscripka/openWakeWord/releases) 下载 `.onnx` 丢进本目录，
或在 Web 控制台「唤醒词」标签里上传即可。默认中文唤醒走 Sherpa-ONNX（见 `sherpa-kws-zh/`）。

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
