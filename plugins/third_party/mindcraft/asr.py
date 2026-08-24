import numpy as np
import pyaudio
import wave
import requests
import json
import time
import threading
import io
import websocket


class VoiceToText:
    def __init__(self, vad_url='ws://127.0.0.1:1000/v1/ws/vad',
                 asr_url='http://127.0.0.1:1000/v1/upload_audio'):
        # 初始化URL
        self.vad_url = vad_url
        self.asr_url = asr_url

        # 音频参数
        self.SAMPLE_RATE = 16000
        self.WINDOW_SIZE = 512
        self.BUFFER_DURATION = 1000
        self.BUFFER_SIZE = int(self.SAMPLE_RATE * (self.BUFFER_DURATION / 1000))
        self.PRE_RECORD_TIME = 1
        self.PRE_RECORD_SAMPLES = self.SAMPLE_RATE * self.PRE_RECORD_TIME
        self.SILENCE_THRESHOLD = 500  # 毫秒

        # 状态变量
        self.is_recording = False
        self.continuous_buffer = []
        self.recording_start_index = 0
        self.last_speech_time = 0
        self.ws = None
        self.audio_stream = None
        self.p = None
        self.retry_count = 0
        self.MAX_RETRIES = 5
        self.silence_timer = None
        self.running = False
        self.ws_thread = None
        self.lock = threading.Lock()

        # 添加文本结果回调和存储
        self.text_result = None
        self.text_ready = threading.Event()
        self.callback_function = None

    def set_callback(self, callback_function):
        """设置回调函数，当识别到文本时会调用这个函数"""
        self.callback_function = callback_function

    def handle_speech(self):
        """处理检测到的语音"""
        self.last_speech_time = time.time()

        # 取消静音定时器
        if self.silence_timer:
            self.silence_timer.cancel()
            self.silence_timer = None

        if not self.is_recording:
            self.is_recording = True
            with self.lock:
                self.recording_start_index = len(self.continuous_buffer)

    def handle_silence(self):
        """处理静音"""
        if self.is_recording and not self.silence_timer:
            # 创建新的定时器，在静音后结束录音
            self.silence_timer = threading.Timer(
                self.SILENCE_THRESHOLD / 1000,
                self.finish_recording
            )
            self.silence_timer.daemon = True
            self.silence_timer.start()

    def on_vad_message(self, ws, message):
        """处理VAD服务返回的消息"""
        try:
            data = json.loads(message)
            is_speaking = data.get('is_speech', False)

            if is_speaking:
                self.handle_speech()
            else:
                self.handle_silence()
        except Exception as e:
            pass

    def on_ws_open(self, ws):
        """WebSocket连接成功回调"""
        self.retry_count = 0

    def on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket连接关闭回调"""
        if self.running and self.retry_count < self.MAX_RETRIES:
            self.retry_count += 1
            time.sleep(1)
            self.setup_websocket()

    def on_ws_error(self, ws, error):
        """WebSocket错误回调"""
        pass

    def setup_websocket(self):
        """设置WebSocket连接"""
        websocket.enableTrace(False)
        self.ws = websocket.WebSocketApp(
            self.vad_url,
            on_message=self.on_vad_message,
            on_open=self.on_ws_open,
            on_close=self.on_ws_close,
            on_error=self.on_ws_error
        )

        # 在单独的线程中运行WebSocket
        if self.ws_thread is None or not self.ws_thread.is_alive():
            self.ws_thread = threading.Thread(target=self.ws.run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()

    def audio_callback(self, in_data, frame_count, time_info, status):
        """PyAudio回调函数"""
        if not self.running:
            return (None, pyaudio.paComplete)

        # 将输入缓冲区转换为numpy数组
        audio_data = np.frombuffer(in_data, dtype=np.float32)

        # 添加到连续缓冲区
        with self.lock:
            self.continuous_buffer.extend(audio_data.tolist())

            # 删除了缓冲区大小限制
            # 不再限制缓冲区大小，允许无限增长

        # 如果WebSocket已连接，发送数据
        if self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(audio_data.tobytes(), opcode=websocket.ABNF.OPCODE_BINARY)
            except:
                pass

        return (in_data, pyaudio.paContinue)

    def float32_to_wav(self, samples):
        """将float32样本转换为WAV文件字节"""
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16位
            wf.setframerate(self.SAMPLE_RATE)

            # 将float32 [-1.0, 1.0]转换为int16
            samples_int16 = (samples * 32767).astype(np.int16)
            wf.writeframes(samples_int16.tobytes())

        return buffer.getvalue()

    def finish_recording(self):
        """完成录音并处理"""
        if not self.is_recording:
            return

        self.is_recording = False

        with self.lock:
            recording_end_index = len(self.continuous_buffer)
            actual_start_index = max(0, self.recording_start_index - self.PRE_RECORD_SAMPLES)
            recorded_samples = self.continuous_buffer[actual_start_index:recording_end_index]

        # 只处理大于0.5秒的录音
        if len(recorded_samples) > self.SAMPLE_RATE * 0.5:
            recorded_array = np.array(recorded_samples, dtype=np.float32)
            wav_data = self.float32_to_wav(recorded_array)
            threading.Thread(target=self.process_recording, args=(wav_data,)).start()
        else:
            self.text_result = ""
            self.text_ready.set()

        # 只保留缓冲区中最后的PRE_RECORD_SAMPLES
        with self.lock:
            self.continuous_buffer = self.continuous_buffer[-self.PRE_RECORD_SAMPLES:]

        # 重置静音定时器
        if self.silence_timer:
            self.silence_timer.cancel()
            self.silence_timer = None

    def process_recording(self, audio_blob):
        """将录制的音频发送到ASR服务"""
        try:
            files = {'file': ('recording.wav', audio_blob, 'audio/wav')}
            response = requests.post(self.asr_url, files=files)
            result = response.json()

            if result.get('status') == 'success' and result.get('text'):
                text = result['text']
                self.text_result = text

                # 如果设置了回调函数，调用它
                if self.callback_function:
                    self.callback_function(text)
            else:
                self.text_result = ""

            # 设置事件，表示结果已经准备好
            self.text_ready.set()

        except Exception as e:
            self.text_result = ""
            self.text_ready.set()

    def start(self):
        """开始录音和处理"""
        self.running = True
        self.text_ready.clear()
        self.text_result = None

        # 初始化PyAudio
        self.p = pyaudio.PyAudio()

        # 设置音频流
        self.audio_stream = self.p.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.SAMPLE_RATE,
            input=True,
            frames_per_buffer=self.WINDOW_SIZE,
            stream_callback=self.audio_callback
        )

        # 连接到VAD WebSocket
        self.setup_websocket()

    def stop(self):
        """停止录音并清理资源"""
        self.running = False

        # 关闭WebSocket
        if self.ws:
            self.ws.close()

        # 停止并关闭音频流
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()

        # 终止PyAudio
        if self.p:
            self.p.terminate()

    def get_text(self, timeout=None):
        """等待并返回识别的文本结果"""
        # 清除之前的结果并等待新结果
        self.text_ready.clear()
        self.text_result = None

        # 等待结果准备好或超时
        if self.text_ready.wait(timeout):
            return self.text_result
        else:
            return None


# 简单封装成函数，方便外部调用
def speech_to_text():
    """
    开始录音并返回识别的文本

    返回:
    str: 识别的文本结果，如果识别失败返回None
    """
    vad_url = 'ws://127.0.0.1:1000/v1/ws/vad'
    asr_url = 'http://127.0.0.1:1000/v1/upload_audio'

    asr = VoiceToText(vad_url, asr_url)

    try:
        # 开始录音
        asr.start()

        # 等待结果，无时间限制
        result = asr.get_text(None)

        return result

    finally:
        # 确保资源被清理
        asr.stop()


# 示例用法
if __name__ == "__main__":
    print('请开始说话...')
    text = speech_to_text()
    print(text)