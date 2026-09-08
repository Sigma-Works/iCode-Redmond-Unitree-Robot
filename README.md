@Sigma-Works
@fgtrzah

# Unitree R1 ("Adam") — iCode Setup Notes

Blunt, step-by-step record of getting a Mac talking to a Unitree R1 over
Ethernet, fixing its Chinese-only TTS, and wiring up a remote-triggered
greeting that runs standalone on the robot. Written after actually doing
this — every step here is something that broke first.

## The one thing that matters most: the robot's IP is `192.168.123.164`

Not `.161`. Unitree's own generic docs and most third-party mirrors use
`.161` as the example IP (that's the convention on Go1/B1, where it's the
bottom Nano). **On this R1 unit, the onboard computer is at `192.168.123.164`.**
Every dead-end in this setup that looked like a hardware or network problem
was actually just pinging/sshing the wrong address. Confirm your own
robot's real IP before assuming anything else is broken — check with
`ifconfig` on the robot itself once you're in, or ask Unitree support if
in doubt.

---

## 1. Physical + network setup (Mac side)

1. Connect a USB-to-Ethernet adapter (e.g. AX88179B chipset) to your Mac,
   and run an Ethernet cable from it directly to the RJ45 port on the
   robot's back.
2. The robot does **not** run DHCP. Your Mac will never get an IP
   automatically — it'll sit at "Self-assigned IP" (macOS's fallback,
   `169.254.x.x`) forever. You must set a static IP by hand.
3. Find your adapter's service name:
   ```
   networksetup -listallnetworkservices
   ```
4. Set a static IP on it, same subnet as the robot (`192.168.123.0/24`):
   ```
   sudo networksetup -setmanual "AX88179B" 192.168.123.222 255.255.255.0
   ```
   (Swap `"AX88179B"` for whatever your adapter is actually named. Any
   free address in `.2`–`.254` works, just don't collide with the robot.)
5. Verify it actually took — check System Settings → Network → your
   adapter. It should say **Manually** / show `192.168.123.222`, not
   "Self-assigned IP."
6. Find the macOS interface name (`enX`) for that adapter — you'll need
   it later for the SDK:
   ```
   ifconfig | grep -B4 "192.168.123.222"
   ```

### If the link won't come up at all
Before touching IP config, confirm the physical link is even active:
```
ifconfig | grep -A6 "^en"
```
Look for `status: active` on the interface with your `.222` address. If
it says `inactive` even with the cable plugged in and the robot powered
on, that's a hardware problem, not a config problem — check for bent
RJ45 pins (flashlight on the jack), try a different cable, and if
possible test the same cable/adapter against a different Ethernet
device to isolate whether it's the cable/adapter or the robot's port.

## 2. Confirm connectivity

```
ping -c 3 192.168.123.164
```

If this doesn't reply, don't move on — nothing past this point will work.
Recheck: (a) your static IP actually applied, (b) link status is
`active`, (c) you're pinging `.164` and not `.161` or `.1`.

## 3. SSH in

```
ssh unitree@192.168.123.164
```
Default password: `123` (change it if it's still default).

Note: SSH access is just for setup/debugging. It is **not** required for
the greeting scripts to run (see §6) — those run as a background service
on the robot itself.

## 4. Install the SDK (on your Mac, for developing/testing from there)

```
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
pip install -e .
```

Any script using the SDK needs the network interface name from step 1.6
passed to `ChannelFactoryInitialize(0, "enX")`.

## 5. The TTS Chinese-language bug

`AudioClient.TtsMaker(text, speaker_id)` speaks **everything in Chinese**
on this firmware, regardless of the `speaker_id` argument or the language
of the input text. This is not a config issue — don't waste time trying
different `speaker_id` values.

**Workaround:** synthesize the audio yourself (English, any voice,
including male voices) and stream raw PCM to the robot via
`AudioClient.PlayStream()` instead of `TtsMaker()`.

On macOS:
```python
# 1. Text-to-speech locally with a real voice (e.g. "Alex" = male)
subprocess.run(["say", "-v", "Alex", "-o", "/tmp/greeting.aiff", TEXT])

# 2. Convert to 16kHz mono PCM — the format the robot's speaker expects
subprocess.run(["afconvert", "/tmp/greeting.aiff", "out.wav", "-d", "LEI16@16000", "-c", "1"])

# 3. Stream it in real-time-paced chunks (1 second of audio per 1 second
#    of wall-clock time — sending faster than real-time overruns the
#    robot's playback buffer and truncates the audio)
client.PlayStream(stream_name, stream_id, chunk_bytes)
# ... sleep(chunk_seconds) between chunks ...
client.PlayStop(stream_name)
```
`say`/`afconvert` are macOS-only and must run in an actual Mac terminal —
they don't exist inside remote/sandboxed shells.

## 6. Standalone operation — no computer needed

Goal: the robot greets people on its own, with nothing plugged in, when
someone double-presses a specific remote button.

### 6.1 Pick a button the firmware doesn't already own
The remote's physical buttons largely map directly to built-in canned
motions at the firmware level (Handshake, Wave, Clap, Dance modes, etc.
— see the sticker on the back of the remote). Reusing one of those for a
custom action isn't possible: the firmware intercepts the signal before
your code ever sees it, so pressing it would just fire the existing
gesture.

**`F1` and `F3` are not listed in the remote's built-in gesture table at
all** — they're free to repurpose. We used `F1`, double-pressed (to avoid
accidental triggers from a resting hand).

### 6.2 Reading the remote independently of the firmware
The robot broadcasts full raw remote-button state on the `rt/lowstate`
DDS topic (`LowState_.wireless_remote`, a byte array), separate from
whatever the firmware does with it. Subscribing to this topic and
watching for a specific bit is how you add a new trigger without
touching or conflicting with existing gestures:
```python
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
from unitree_sdk2py.core.channel import ChannelSubscriber

def on_low_state(msg: LowState_):
    wr = msg.wireless_remote
    f1_down = bool((wr[2] >> 6) & 1)   # F1 = byte 2, bit 6
    # detect a rising edge, then a second one within ~0.6s = double-press
```

### 6.3 Run it as a systemd service on the robot
This is what makes it survive without a laptop attached:

```ini
# /etc/systemd/system/icode-greeter.service
[Unit]
Description=iCode remote-triggered greeter
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/unitree/icode_auto_greeter
ExecStart=/usr/bin/python3 /home/unitree/icode_auto_greeter/icode_remote_greeter.py eth10
Restart=always
RestartSec=3
User=unitree

[Install]
WantedBy=multi-user.target
```
Install and enable it (on the robot, via ssh, one-time setup):
```
sudo cp icode-greeter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now icode-greeter
sudo systemctl status icode-greeter    # should say "active (running)"
```

The `eth10` here is the robot's **own internal** network interface name
— confirm yours with `ifconfig` while ssh'd into the robot; it's not
necessarily the same name as your Mac's adapter. This interface is the
robot's internal comms bus between its own onboard modules (audio,
motion controller, etc.) — it is not the same thing as "is a cable
plugged into the external port." Once the service is enabled, you can
disconnect the Ethernet cable entirely. SSH access requires the cable;
the service itself does not.

Useful commands going forward:
```
sudo systemctl status icode-greeter     # check it's alive
sudo journalctl -u icode-greeter -f     # live logs (watch this while testing the trigger)
sudo systemctl restart icode-greeter    # restart without rebooting
```

---

## Summary of failure modes we hit, for quick pattern-matching

| Symptom | Cause | Fix |
|---|---|---|
| `ping 192.168.1.1` succeeds but robot is unreachable | That's your home Wi-Fi router, a different subnet entirely — proves nothing about the robot | Ping `192.168.123.164` specifically |
| macOS shows "Self-assigned IP" on the adapter | Robot has no DHCP server | Set a static IP manually (`networksetup -setmanual`) |
| `ssh: connect ... Network is unreachable` | No route to `192.168.123.x` at all — static IP not set or link down | Fix static IP + confirm `status: active` in `ifconfig` |
| `ssh: connect ... Connection refused` | You're hitting the wrong device (e.g. your own router) | Double check the target IP is `.164` |
| Adapter shows `status: inactive` despite cable plugged in | Physical layer problem (cable, adapter, or robot's RJ45 port) | Inspect port for bent pins, try another cable, cross-test the adapter against a different device |
| Robot speaks, but always in Chinese no matter what | `TtsMaker()` is Chinese-only on this firmware, `speaker_id` doesn't change that | Use `say`/`afconvert` + `PlayStream()` instead |
| Want a custom remote-button action | Every printed button/combo is already a firmware canned motion | Use `F1`/`F3` (unlisted, unclaimed) via raw `rt/lowstate` reads |
| Want it to work without a laptop attached | Script was only running interactively over ssh | Install as a `systemd` service on the robot itself |
