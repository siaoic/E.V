import requests
import json

PROMPT_AUDIO_URL = "https://image2url.com/r2/default/audio/1774185734751-4e9d391e-03eb-4370-9c33-b7d08082356c.mp3"
SPEAKER_AUDIO = "E:/wm/tool/GSV-TTS-Lite/examples/laffey.mp3"

print("=" * 60)
print("测试外链音频 API")
print("=" * 60)

print(f"\n外链 prompt_audio: {PROMPT_AUDIO_URL}")
print(f"本地 speaker_audio: {SPEAKER_AUDIO}")

print("\n发送请求到 http://localhost:8000/tts/single ...")

response = requests.post(
    "http://localhost:8000/tts/single",
    json={
        "text": "你好，这是外链音频测试。",
        "speaker_audio": SPEAKER_AUDIO,
        "prompt_audio": PROMPT_AUDIO_URL,
    },
    timeout=120
)

print(f"\n状态码: {response.status_code}")
print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")

if response.status_code == 200:
    data = response.json()
    if data.get("success"):
        print(f"\n✅ 成功！")
        print(f"   音频时长: {data.get('audio_len')}秒")
        print(f"   文件名: {data.get('filename')}")
        print(f"   ASR识别文本: {data.get('prompt_text_used')}")
        print(f"   下载地址: http://localhost:8000/audio/{data.get('filename')}")
else:
    print(f"\n❌ 失败: {response.text}")
