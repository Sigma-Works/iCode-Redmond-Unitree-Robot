"""
iCode Open House -- remote-triggered greeter for Adam (Unitree R1).

Runs entirely on the robot's own onboard computer. Listens to the
wireless remote's raw button state (via the robot's own LowState_
broadcast on "rt/lowstate") and plays the pre-rendered greeting
(icode_greeting_16k_mono.wav) through the onboard speaker whenever F1
is double-pressed.

F1 was chosen because it does NOT appear anywhere in the robot's
built-in canned-gesture table (Handshake, Wave, Clap, Dance modes,
etc. -- see the printed legend on the back of the remote), so this
trigger can never collide with an existing motion demo. Reading the
remote state this way (rt/lowstate -> wireless_remote bytes) is
independent of the robot's firmware gesture dispatch, so it's purely
additive.

Requires: icode_greeting_16k_mono.wav in the same directory (generate
it with generate_greeting_wav.py on a Mac, then copy it over).

Run:
    python3 icode_remote_greeter.py <networkInterface>
    e.g. python3 icode_remote_greeter.py eth10

To run this permanently, without any computer attached, install it as
a systemd service -- see icode-greeter.service / SETUP.md alongside
this file.
"""

import os
import struct
import sys
import time

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GREETING_WAV = os.path.join(SCRIPT_DIR, "icode_greeting_16k_mono.wav")

# F1 bit lives in wireless_remote byte index 2, bit 6
# (see unitreeRemoteController.parse_botton in example/wireless_controller/wireless_controller.py)
F1_BYTE_INDEX = 2
F1_BIT = 6

DOUBLE_PRESS_WINDOW = 0.6     # seconds between the two presses to count as a double-press
TRIGGER_COOLDOWN = 8.0        # don't retrigger again for this long after a successful greet
SPEAK_VOLUME = 80


def read_wav(filename):
    with open(filename, "rb") as f:
        def read(fmt):
            return struct.unpack(fmt, f.read(struct.calcsize(fmt)))

        assert read("<I")[0] == 0x46464952  # RIFF
        read("<I")
        assert read("<I")[0] == 0x45564157  # WAVE

        subchunk1_id, subchunk1_size = read("<II")
        if subchunk1_id == 0x4B4E554A:  # JUNK
            f.seek(subchunk1_size, 1)
            subchunk1_id, subchunk1_size = read("<II")
        assert subchunk1_id == 0x20746D66  # "fmt "

        _audio_format, num_channels = read("<HH")
        sample_rate = read("<I")[0]
        read("<I")   # byte_rate
        read("<H")   # block_align
        bits_per_sample = read("<H")[0]
        if subchunk1_size == 18:
            read("<H")

        while True:
            subchunk2_id, subchunk2_size = read("<II")
            if subchunk2_id == 0x61746164:  # "data"
                break
            f.seek(subchunk2_size, 1)

        raw_pcm = f.read(subchunk2_size)
        return list(raw_pcm), sample_rate, num_channels, bits_per_sample


class RemoteGreeter:
    def __init__(self, audio_client, greeting_pcm, sample_rate, num_channels, bits_per_sample):
        self.audio_client = audio_client
        self.greeting_pcm = greeting_pcm
        self.bytes_per_second = sample_rate * num_channels * (bits_per_sample // 8)

        self._f1_was_down = False
        self._first_press_time = 0.0
        self._last_trigger_time = 0.0

    def on_low_state(self, msg: LowState_):
        wr = msg.wireless_remote
        if len(wr) <= F1_BYTE_INDEX:
            return

        f1_down = bool((wr[F1_BYTE_INDEX] >> F1_BIT) & 1)
        now = time.time()

        # Rising edge only (ignore held-down state)
        if f1_down and not self._f1_was_down:
            if now - self._first_press_time <= DOUBLE_PRESS_WINDOW:
                # second press within the window -> double-press detected
                self._first_press_time = 0.0
                self._maybe_greet(now)
            else:
                self._first_press_time = now

        self._f1_was_down = f1_down

    def _maybe_greet(self, now):
        if now - self._last_trigger_time < TRIGGER_COOLDOWN:
            print(f"[{time.strftime('%H:%M:%S')}] F1 double-press seen, but still cooling down -- ignored.")
            return
        self._last_trigger_time = now
        print(f"[{time.strftime('%H:%M:%S')}] F1 double-press detected -- greeting.")
        self.play_greeting()

    def play_greeting(self):
        try:
            self.audio_client.SetVolume(SPEAK_VOLUME)
        except Exception as e:
            print(f"Warning: couldn't set volume ({e})")

        pcm_data = bytes(self.greeting_pcm)
        stream_id = str(int(time.time() * 1000))
        chunk_seconds = 1.0
        chunk_size = int(self.bytes_per_second * chunk_seconds)
        offset, total = 0, len(pcm_data)

        while offset < total:
            chunk = pcm_data[offset: offset + chunk_size]
            code, _ = self.audio_client.PlayStream("icode_remote_greeter", stream_id, chunk)
            if code != 0:
                print(f"[ERROR] PlayStream chunk failed, code={code}")
                break
            offset += len(chunk)
            time.sleep(chunk_seconds)

        self.audio_client.PlayStop("icode_remote_greeter")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <networkInterface>")
        sys.exit(1)
    iface = sys.argv[1]

    print("Loading greeting audio...")
    pcm_list, sample_rate, num_channels, bits = read_wav(GREETING_WAV)
    print(f"Greeting: {sample_rate} Hz, {num_channels} ch, {bits}-bit, {len(pcm_list)} bytes")
    if sample_rate != 16000 or num_channels != 1:
        print("[ERROR] greeting WAV must be 16kHz mono.")
        sys.exit(1)

    print(f"Initializing channel factory on interface: {iface}")
    ChannelFactoryInitialize(0, iface)

    audio_client = AudioClient()
    audio_client.SetTimeout(10.0)
    audio_client.Init()

    greeter = RemoteGreeter(audio_client, pcm_list, sample_rate, num_channels, bits)

    lowstate_subscriber = ChannelSubscriber("rt/lowstate", LowState_)
    lowstate_subscriber.Init(greeter.on_low_state, 10)

    print("Listening for F1 double-press on the remote... (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
