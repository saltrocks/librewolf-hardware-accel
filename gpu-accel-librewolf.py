#!/usr/bin/env python3
"""Enable and verify NVIDIA hardware video decoding (VA-API) in LibreWolf.

    ./gpu-accel-librewolf.py                 apply config
    ./gpu-accel-librewolf.py --verify        apply, then test decoding with a throwaway profile
    ./gpu-accel-librewolf.py --verify-only   only test, change nothing
    ./gpu-accel-librewolf.py --undo          remove everything this script added
    ./gpu-accel-librewolf.py --dry-run       show what would change

Changes live between "gpu-accel BEGIN/END" marker comments, so re-running is
idempotent and --undo removes exactly what was added. Restart LibreWolf after
applying. Requires libva-nvidia-driver; --verify also needs ffmpeg and nvidia-smi.

What it sets and why:
  <profile>/user.js
      media.hardware-video-decoding.force-enabled = true  (NVIDIA is blocklisted).
      Read once at startup, so it cannot go in librewolf.overrides.cfg, which
      LibreWolf loads too late for it.
  ~/.config/librewolf/librewolf/librewolf.overrides.cfg
      setEnv("MOZ_DISABLE_RDD_SANDBOX", "1") -- the NVIDIA VA-API driver cannot
      initialise inside the media-decoder (RDD) sandbox. Security trade-off:
      video decoders then run in a less-restricted process.
"""
import argparse, configparser, os, re, shutil, signal, subprocess, sys, tempfile, time
from pathlib import Path

XDG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
LW_BIN = "/usr/lib/librewolf/librewolf"
LW_EXE_PREFIX = "/usr/lib/librewolf/"
LW_ROOT = XDG / "librewolf" / "librewolf"
LW_OVERRIDES = LW_ROOT / "librewolf.overrides.cfg"
PREFS = ['user_pref("media.hardware-video-decoding.force-enabled", true);']
ENV = ['setEnv("MOZ_DISABLE_RDD_SANDBOX", "1");']
VAAPI_DRIVER = "/usr/lib/dri/nvidia_drv_video.so"

BEGIN = "// >>> gpu-accel BEGIN (managed, do not edit inside)"
END = "// <<< gpu-accel END"
# also recognise blocks written by the older combined gpu-accel.py
BLOCK_RE = re.compile(r"\n?// >>> gpu-accel(?:\.py)? BEGIN[^\n]*\n.*?// <<< gpu-accel(?:\.py)? END\n?", re.S)


def say(msg, kind="•"):
    print(f"{kind} {msg}")


def running():
    return subprocess.run(["pgrep", "-x", "librewolf"], capture_output=True).returncode == 0


# ---------------------------------------------------------------- configuration

def write_if_changed(path, new, dry):
    old = path.read_text() if path.exists() else None
    new = new.strip("\n") + "\n" if new.strip() else ""
    if (old or "") == new:
        say(f"{path}: already up to date")
        return
    if dry:
        say(f"{path}: would change", "~")
        return
    if not new and old is not None:
        path.unlink()
        say(f"{path}: removed (was only managed content)", "✓")
        return
    bak = path.with_name(path.name + ".gpu-accel.bak")
    if old is not None and not bak.exists():  # keep the oldest backup: the pre-script original
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    say(f"{path}: {'updated' if old is not None else 'created'}", "✓")


def set_block(path, lines, dry, undo):
    text = BLOCK_RE.sub("\n", path.read_text() if path.exists() else "").strip("\n")
    if not undo:
        text = "\n".join(filter(None, [text, BEGIN, *lines, END]))
    write_if_changed(path, text, dry)


def default_profile():
    ini = LW_ROOT / "profiles.ini"
    if not ini.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(ini)
    rel = next((cp[s]["Default"] for s in cp.sections() if s.startswith("Install") and "Default" in cp[s]), None)
    if rel is None:  # no install-specific default: fall back to the Default=1 profile
        sec = next((s for s in cp.sections() if cp[s].get("Default") == "1"), None)
        if sec is None:
            return None
        rel = cp[sec]["Path"]
        if cp[sec].get("IsRelative", "1") != "1":
            return Path(rel)
    return LW_ROOT / rel


def configure(dry, undo):
    prof = default_profile()
    if prof is None:
        say("no LibreWolf profile yet; start LibreWolf once, then re-run", "!")
        return False
    say(f"LibreWolf profile: {prof.name}")
    set_block(prof / "user.js", PREFS, dry, undo)
    set_block(LW_OVERRIDES, ENV, dry, undo)
    return True


def preflight():
    ok = True
    if not Path(LW_BIN).exists():
        say(f"{LW_BIN} not found; is LibreWolf installed?", "!")
        ok = False
    if not Path("/sys/module/nvidia_drm").exists():
        say("nvidia_drm kernel module not loaded; this script targets the NVIDIA driver", "!")
        ok = False
    if not Path(VAAPI_DRIVER).exists():
        say(f"{VAAPI_DRIVER} missing; install it with: sudo pacman -S libva-nvidia-driver", "!")
        ok = False
    return ok


# ---------------------------------------------------------------- verification helpers

def make_clips(d):
    clips = {"h264": ("h264.mp4", ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]),
             "vp9": ("vp9.webm", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "3M"]),
             "av1": ("av1.mp4", ["-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p"])}
    for name, (fn, enc) in clips.items():
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
                        "-t", "15", *enc, str(d / fn)], check=True, stderr=subprocess.DEVNULL)
        (d / f"{name}.html").write_text(f'<!doctype html><video id=v src="{fn}" autoplay muted loop></video>')
    return list(clips)


def nvdec_sampler(secs):
    return subprocess.Popen(["nvidia-smi", "dmon", "-s", "u", "-c", str(secs)], stdout=subprocess.PIPE, text=True)


def nvdec_max(proc):
    rows = [l.split() for l in proc.communicate()[0].splitlines() if l.strip()[:1].isdigit()]
    return max((int(r[4]) for r in rows if len(r) > 4 and r[4].isdigit()), default=-1)


def kill_tree(proc, prof):
    """Stop the browser, then sweep any process still using our temp profile."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    except Exception:
        pass
    time.sleep(1)
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cl = Path(f"/proc/{pid}/cmdline").read_bytes()
            if int(pid) != os.getpid() and cl.startswith(LW_EXE_PREFIX.encode()) and str(prof).encode() in cl:
                os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------- verification

def test_decode(media, codecs):
    real = default_profile()
    prof = Path(tempfile.mkdtemp(prefix="gpu-accel-lw-"))
    # copy the real user.js so the test uses your actual prefs; overrides.cfg applies globally
    userjs = (real / "user.js").read_text() if real and (real / "user.js").exists() else ""
    (prof / "user.js").write_text(userjs + '\nuser_pref("media.autoplay.default", 0);\n'
                                  'user_pref("media.block-autoplay-until-in-foreground", false);\n'
                                  'user_pref("browser.shell.checkDefaultBrowser", false);\n')
    results = {}
    try:
        for c in codecs:  # one browser run per codec, so each log maps to one clip
            log = prof / f"{c}.log"
            env = dict(os.environ, MOZ_LOG="FFmpegVideo:5", MOZ_LOG_FILE=str(log))
            proc = subprocess.Popen([LW_BIN, "--profile", str(prof), "--no-remote", f"file://{media}/{c}.html"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(5)
            smp = nvdec_sampler(5)
            time.sleep(5)
            kill_tree(proc, prof)
            text = "".join(p.read_text(errors="replace") for p in prof.glob(f"{c}.log*"))
            hw = text.count("IsHardwareAccelerated=true")
            sw = text.count("IsHardwareAccelerated=false")
            name = "VA-API (hardware)" if hw and not sw else "software" if sw and not hw else f"mixed hw={hw} sw={sw}" if hw else "?"
            if "vaInitialize failed" in text:
                name += ", vaInitialize failed (RDD sandbox still on?)"
            results[c] = (name, hw > 0 and sw == 0, nvdec_max(smp))
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    return results


def verify():
    for tool in ("ffmpeg", "nvidia-smi"):
        if not shutil.which(tool):
            say(f"--verify needs {tool}", "!")
            return False
    if running():
        say("note: LibreWolf is running; the test uses a separate throwaway profile and won't touch it")
    say("generating test clips and launching LibreWolf with a throwaway profile (windows will pop up briefly)…")
    media = Path(tempfile.mkdtemp(prefix="gpu-accel-media-"))
    try:
        results = test_decode(media, make_clips(media))
    finally:
        shutil.rmtree(media, ignore_errors=True)
    print("\nLibreWolf:")
    for c, (dec, hw, nv) in results.items():
        print(f"  {'✓' if hw else '✗'} {c:5} decoder={dec:28} NVDEC peak={nv}%")
    ok = bool(results) and all(hw for _, hw, _ in results.values())
    print()
    say("all codecs hardware-decoded" if ok else "some codecs are NOT hardware-decoded", "✓" if ok else "✗")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--undo", action="store_true", help="remove everything this script added")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--verify", action="store_true", help="after applying, test decoding in a throwaway profile")
    ap.add_argument("--verify-only", action="store_true", help="only test, change nothing")
    a = ap.parse_args()

    if not a.verify_only:
        if not a.undo and not preflight():
            sys.exit(1)
        if not configure(a.dry_run, a.undo):
            sys.exit(1)
        if not a.dry_run and running():
            say("LibreWolf is running: restart it for the change to take effect", "!")
    if (a.verify or a.verify_only) and not a.undo and not a.dry_run:
        sys.exit(0 if verify() else 2)


if __name__ == "__main__":
    main()
