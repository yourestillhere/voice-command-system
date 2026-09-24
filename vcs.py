"""
JARVIS - personal voice-controlled Windows assistant.

Install:  pip install dearpygui sounddevice numpy SpeechRecognition pyttsx3
Run:      python jarvis.py
(Speech recognition uses Google's free web recognizer, so it needs internet.)
"""
import json, os, sys, platform, threading, time, webbrowser, urllib.parse
from datetime import datetime

import numpy as np
import sounddevice as sd
import speech_recognition as sr
import pyttsx3
import dearpygui.dearpygui as dpg

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")

DEFAULTS = {
    "name": "Jarvis", "voice": "JARVIS", "rate": 175, "volume": 0.9,
    "mic": "", "output": "", "require_name": False, "continuous": False,
    "apps": {},
}
LINKS = [
    ("Discord", "https://discord.gg/ggjTbWpQn4"),
    ("YouTube", "https://youtube.com/@priesteleven"),
    ("GitHub", "https://github.com/yourestillhere"),
    ("Email Me", "mailto:soul@priest.com"),
    ("Text me in Telegram", "https://t.me/oncethen"),
    ("X", "https://x.com/openyourledger"),
]
SITES = {"youtube": "https://www.youtube.com", "discord": "https://discord.com/app",
         "github": "https://github.com"}
# UI voice label -> (Windows voice name fragment, speed offset)
VOICES = {"JARVIS": ("David", 0), "Male 1": ("David", 0), "Male 2": ("David", -25),
          "Female 1": ("Zira", 0), "Female 2": ("Zira", -25)}

cfg = dict(DEFAULTS)
history = []
speak_lock = threading.Lock()
listening = False


# ---------- config ----------
def load_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def save_cfg():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------- speech out ----------
def speak(text, blocking=False):
    def run():
        with speak_lock:
            try:
                try:  # SAPI5 needs COM initialised in every non-main thread
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass
                eng = pyttsx3.init()  # fresh engine per call avoids threading hangs
                frag, off = VOICES.get(cfg["voice"], ("David", 0))
                for v in eng.getProperty("voices"):
                    if frag.lower() in v.name.lower():
                        eng.setProperty("voice", v.id)
                        break
                eng.setProperty("rate", int(cfg["rate"]) + off)
                eng.setProperty("volume", float(cfg["volume"]))
                eng.say(text)
                eng.runAndWait()
            except Exception as e:
                print("TTS error:", e)
                dpg.set_value("response", f"[Voice error] {e}")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    if blocking:
        t.join()

def respond(text):
    dpg.set_value("response", text)
    speak(text)


# ---------- speech in ----------
def device_index(name, kind):
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d[f"max_{kind}_channels"] > 0:
            return i
    return None

def record_phrase(max_wait=8, max_len=10):
    """Calibrate noise, wait for speech, record until ~1s of silence.
    Returns (int16 bytes, samplerate) or (None, rate)."""
    dev = device_index(cfg["mic"], "input")
    rate = int(sd.query_devices(dev, "input")["default_samplerate"])
    block = int(rate * 0.1)
    frames, started, silent, waited, noise = [], False, 0, 0, []
    with sd.InputStream(samplerate=rate, channels=1, dtype="int16", device=dev, blocksize=block) as s:
        while True:
            data, _ = s.read(block)
            level = float(np.abs(data).mean())
            if len(noise) < 5:            # first 0.5s = background noise
                noise.append(level)
                continue
            thresh = max(np.mean(noise) * 2.5, 120)
            if level > thresh:
                started, silent = True, 0
            elif started:
                silent += 1
            else:
                waited += 1
            if started:
                frames.append(data.copy())
            if (not started and waited > max_wait * 10) or silent > 10 or len(frames) > max_len * 10:
                break
    if not frames:
        return None, rate
    return np.concatenate(frames).tobytes(), rate

def hear_once():
    dpg.set_value("status", "Listening...")
    try:
        raw, rate = record_phrase()
        if raw is None:
            dpg.set_value("heard", "(no speech detected - check the microphone in Settings)")
            return None
        dpg.set_value("status", "Thinking...")
        return sr.Recognizer().recognize_google(sr.AudioData(raw, rate, 2))
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        dpg.set_value("response", f"[Recognition error] Google speech service unreachable: {e}")
        return None
    except Exception as e:
        dpg.set_value("response", f"[Microphone error] {e}")
        return None
    finally:
        dpg.set_value("status", "Ready")


# ---------- commands ----------
def process(text):
    dpg.set_value("heard", text)
    low = text.lower().strip()
    name = cfg["name"].lower()
    if low.startswith(name):
        low = low[len(name):].lstrip(" ,.")
    elif cfg["require_name"]:
        return  # ignore commands that don't start with the assistant's name
    log(text)

    if low.startswith("search youtube for "):
        q = low[len("search youtube for "):]
        webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
        return respond(f"Searching YouTube for {q}.")
    if low.startswith(("open ", "launch ", "start ")):
        target = low.split(" ", 1)[1].strip()
        for app, path in cfg["apps"].items():
            if app.lower() == target:
                return launch(app)
        if target in SITES:
            webbrowser.open(SITES[target])
            return respond(f"Opening {target.title()}.")
        return respond(f"I don't know an application called {target}.")
    if "time" in low:
        return respond("It is " + datetime.now().strftime("%I:%M %p").lstrip("0"))
    if "date" in low or "day is it" in low:
        return respond("Today is " + datetime.now().strftime("%A, %B %d"))
    if low in ("hello", "hi", "hey"):
        return respond("Hello. How can I help?")
    respond("Sorry, I didn't understand that.")

def launch(app):
    path = cfg["apps"].get(app)
    if not path or not os.path.exists(path):
        return respond(f"The path for {app} is missing or invalid.")
    try:
        os.startfile(path)
        respond(f"Opening {app}.")
    except Exception as e:
        respond(f"Could not open {app}: {e}")

def log(text):
    history.append(f"{datetime.now():%H:%M:%S}  {text}")
    dpg.set_value("history_box", "\n".join(reversed(history)))

def listen_worker():
    global listening
    while True:
        text = hear_once()
        if text:
            process(text)
        elif text == "":
            dpg.set_value("heard", "(couldn't understand)")
        if not cfg["continuous"] or not listening:
            break
    listening = False

def on_listen():
    global listening
    if listening:
        listening = False
        return
    listening = True
    threading.Thread(target=listen_worker, daemon=True).start()


# ---------- UI callbacks ----------
def set_cfg(key, cast=lambda v: v):
    def cb(_, value):
        cfg[key] = cast(value)
        save_cfg()
    return cb

def refresh_apps():
    dpg.configure_item("app_list", items=list(cfg["apps"].keys()))

def add_app():
    n, p = dpg.get_value("app_name").strip(), dpg.get_value("app_path").strip().strip('"')
    if not n or not p:
        return dpg.set_value("app_msg", "Enter both a name and an EXE path.")
    cfg["apps"][n] = p
    save_cfg(); refresh_apps()
    dpg.set_value("app_msg", f"Saved {n}.")

def selected_app():
    return dpg.get_value("app_list") or None

def del_app():
    a = selected_app()
    if a in cfg["apps"]:
        del cfg["apps"][a]; save_cfg(); refresh_apps()

def launch_selected():
    if selected_app():
        launch(selected_app())

def on_name_change(_, value):
    cfg["name"] = value.strip() or "Jarvis"
    save_cfg()
    dpg.set_value("brand", cfg["name"].upper())

PAGES = ["Home", "Applications", "Community", "History", "System", "Settings"]
def show_page(name):
    for p in PAGES:
        dpg.configure_item(f"page_{p}", show=(p == name))


# ---------- build UI ----------
def build():
    mics = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    outs = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
    ex = f'"{cfg["name"]}, open Minecraft"   "{cfg["name"]}, search YouTube for Minecraft"   "{cfg["name"]}, open GitHub"'

    with dpg.window(tag="main"):
        with dpg.group(horizontal=True):
            with dpg.child_window(width=200, border=True):
                dpg.add_text(cfg["name"].upper(), tag="brand", color=(80, 200, 255))
                dpg.add_text("PERSONAL ASSISTANT", color=(140, 140, 140))
                dpg.add_spacer(height=10)
                for p in PAGES:
                    dpg.add_button(label=p, width=-1, height=32, callback=lambda s, a, u: show_page(u), user_data=p)
                dpg.add_spacer(height=20)
                dpg.add_text("STATUS", color=(140, 140, 140))
                dpg.add_text("Ready", tag="status")
                dpg.add_spacer(height=10)
                dpg.add_button(label="Listen##side", width=-1, height=36, callback=on_listen)

            with dpg.group():
                # Home
                with dpg.child_window(tag="page_Home", border=False):
                    dpg.add_text("JARVIS", tag="home_title", color=(80, 200, 255))
                    dpg.add_text("Your personal voice-controlled Windows assistant.")
                    dpg.add_spacer(height=10)
                    dpg.add_text("Heard"); dpg.add_text("Nothing yet.", tag="heard", wrap=650)
                    dpg.add_spacer(height=6)
                    dpg.add_text("Response"); dpg.add_text("Waiting for a command.", tag="response", wrap=650)
                    dpg.add_spacer(height=10)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Listen##home", callback=on_listen)
                        dpg.add_button(label="Test Voice##home", callback=lambda: respond("Voice test successful."))
                    dpg.add_spacer(height=14)
                    dpg.add_text("Example commands", color=(140, 140, 140))
                    dpg.add_text(ex, wrap=650)

                # Applications
                with dpg.child_window(tag="page_Applications", border=False, show=False):
                    dpg.add_text("Applications")
                    dpg.add_input_text(tag="app_name", hint="Application Name", width=400)
                    dpg.add_input_text(tag="app_path", hint=r"EXE Path  e.g. C:\Path\To\MinecraftLauncher.exe", width=400)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Add application", callback=add_app)
                        dpg.add_button(label="Launch application", callback=launch_selected)
                        dpg.add_button(label="Delete application", callback=del_app)
                    dpg.add_text("", tag="app_msg")
                    dpg.add_listbox(list(cfg["apps"].keys()), tag="app_list", width=400, num_items=10)

                # Community
                with dpg.child_window(tag="page_Community", border=False, show=False):
                    dpg.add_text("Community")
                    for label, url in LINKS:
                        with dpg.group(horizontal=True):
                            dpg.add_button(label=f"Open##{label}", callback=lambda s, a, u: webbrowser.open(u), user_data=url)
                            dpg.add_text(f"{label}:  {url}")

                # History
                with dpg.child_window(tag="page_History", border=False, show=False):
                    dpg.add_text("History")
                    dpg.add_input_text(tag="history_box", multiline=True, readonly=True, width=-1, height=-1)

                # System
                with dpg.child_window(tag="page_System", border=False, show=False):
                    dpg.add_text("System")
                    dpg.add_text(f"Operating system: {platform.system()}")
                    dpg.add_text(f"Windows version/platform: {platform.platform()}")
                    dpg.add_text(f"Python version: {sys.version.split()[0]}")
                    dpg.add_text(f"Audio library: sounddevice {sd.__version__}")
                    dpg.add_text("Voice engine: pyttsx3 (Windows SAPI5)")

                # Settings
                with dpg.child_window(tag="page_Settings", border=False, show=False):
                    dpg.add_text("Settings")
                    dpg.add_input_text(label="Assistant name", default_value=cfg["name"], width=300,
                                       callback=on_name_change, on_enter=False)
                    dpg.add_combo(list(VOICES), label="Voice", default_value=cfg["voice"], width=300,
                                  callback=set_cfg("voice"))
                    dpg.add_slider_int(label="Voice speed", default_value=int(cfg["rate"]), min_value=100,
                                       max_value=300, width=300, callback=set_cfg("rate", int))
                    dpg.add_slider_float(label="Volume", default_value=float(cfg["volume"]), min_value=0,
                                         max_value=1, width=300, callback=set_cfg("volume", float))
                    dpg.add_combo(mics, label="Microphone", default_value=cfg["mic"], width=300,
                                  callback=set_cfg("mic"))
                    dpg.add_combo(outs, label="Audio output", default_value=cfg["output"], width=300,
                                  callback=set_cfg("output"))
                    dpg.add_checkbox(label="Require assistant name before commands",
                                     default_value=cfg["require_name"], callback=set_cfg("require_name"))
                    dpg.add_checkbox(label="Continuous listening", default_value=cfg["continuous"],
                                     callback=set_cfg("continuous"))
                    dpg.add_spacer(height=8)
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Test Voice##settings", callback=lambda: respond("Voice test successful."))
                        dpg.add_button(label="Test Microphone", callback=lambda: threading.Thread(
                            target=test_mic, daemon=True).start())

def test_mic():
    dpg.set_value("status", "Say something...")
    text = hear_once()
    dpg.set_value("heard", text if text else "(nothing heard)")
    respond(f"I heard: {text}" if text else "I didn't hear anything.")


def main():
    load_cfg()
    dpg.create_context()
    build()
    dpg.set_value("history_box", "")
    dpg.create_viewport(title="JARVIS", width=1000, height=640)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main", True)
    dpg.start_dearpygui()
    dpg.destroy_context()

if __name__ == "__main__":
    main()