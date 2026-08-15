import os
import json
import queue
import numpy as np
import soundfile as sf
import threading
try:
    import sounddevice as sd
except:
    pass


class AudioQueue:
    def __init__(self, samplerate):
        self.samplerate = samplerate
        self.q = queue.Queue()
        self.t = None
        self.playback_finished = threading.Event()
        self.playback_finished.set()

        try:
            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=1,
                dtype='float32'
            )
            self.stream.start()
        except:
            self.stream = None

    def put(self, data):
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        
        self.q.put(data)
        
        if self.t is None or not self.t.is_alive():
            self.playback_finished.clear()
            self.t = threading.Thread(target=self._run_playback, daemon=True)
            self.t.start()

    def _run_playback(self):
        while not self.q.empty():
            data = self.q.get()
            if self.stream:
                self.stream.write(data)
        
        self.playback_finished.set()

    def stop(self):
        """
        Immediately stops playback and clears all audio data in the queue.
        """
        with self.q.mutex:
            self.q.queue.clear()

        if self.stream:
            self.stream.stop()
            self.stream.start()
        
        self.playback_finished.set()

    def wait(self):
        """
        Waits until all audio currently in the queue has finished playing.
        """
        self.playback_finished.wait()


class AudioClip:
    def __init__(self, audio_queue, audio_data, samplerate, audio_len_s, subtitles, orig_text):
        self.audio_queue: AudioQueue = audio_queue
        self.audio_data = audio_data
        self.samplerate = samplerate
        self.audio_len_s = audio_len_s
        self.subtitles = subtitles
        self.orig_text = orig_text
    
    def play(self, volume: float = 1.0):
        """
        Adds the audio data to the playback queue for sequential output.
        """

        if volume != 1.0:
            self.audio_data = self.audio_data * volume
            self.audio_data = np.clip(self.audio_data, -1.0, 1.0)

        self.audio_queue.put(self.audio_data)
    
    def save(self, save_path: str, is_save_subtitles: bool = False):
        """
        Saves the audio data to a file and optionally exports subtitles as a JSON file.
        """
        sf.write(save_path, self.audio_data, self.samplerate)

        if is_save_subtitles:
            subtitles_path, _ = os.path.splitext(save_path)
            subtitles_path = subtitles_path + ".json"
            with open(subtitles_path, 'w', encoding='utf-8') as f:
                json.dump({"orig_text":self.orig_text, "subtitles":self.subtitles}, f, indent=4, ensure_ascii=False)