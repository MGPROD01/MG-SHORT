#!/usr/bin/env python3
"""MG Shorts Studio - serveur local.

Habille une video verticale deja montee (switch cameras/micros + derush faits a la main) :
transcription Whisper, sous-titres mot par mot, accroche, zooms, bruitages, musique,
versions V1/V2/V3... Aucune dependance Python : uniquement la bibliotheque standard.

Lancer : python server.py   puis ouvrir http://localhost:8765
"""
import array, hashlib, hmac, math, json, secrets, os, re, shutil, subprocess, sys, threading, time, queue, unicodedata, uuid, mimetypes, urllib.parse, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP = os.path.dirname(os.path.abspath(__file__))
WIN = os.name == "nt"
NOWIN = 0x08000000 if WIN else 0

DEFAULT_CFG = {
    "port": 8765,
    "ffmpeg": "bin/ffmpeg.exe" if WIN else "ffmpeg",
    "ffprobe": "bin/ffprobe.exe" if WIN else "ffprobe",
    "whisper": "bin/whisper/whisper-cli.exe" if WIN else "whisper-cli",
    "model": "models/ggml-large-v3-turbo-q5_0.bin",
    "music_dir": "musiques",
    "fonts_dir": "fonts",
    "sfx_dir": "",
    "projects_dir": "projets",
    "language": "fr",
    "whisper_prompt": "Pierre, Rémi, Ben, Alexy, Anaële, Tyfène.",
    "word_fixes": {"Alexis": "Alexy", "Alex": "Alexy", "séviche": "ceviche"},
    # --- videos longues -> shorts
    "anthropic_api_key": "",          # cle API Claude (console.anthropic.com) ; vide = mode copier-coller
    "claude_model": "claude-sonnet-5-5",
    "episode_min_sec": 150,           # au-dela, une video deposee est traitee comme une video longue
    "clip_min": 60, "clip_max": 150, "clip_count": 8,   # formats de 1 min a 2 min 30
    "episode_context": "",            # ex. "Emission culinaire de Pierre Croce, ton humoristique"
    "auto_render": True,              # genere la V1 de chaque short tout seul
    "encoder": "auto",                # auto = carte graphique si elle sait encoder (Nvidia, Intel, AMD), sinon processeur ; "cpu" pour forcer
}


def load_cfg():
    p = os.path.join(APP, "config.json")
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(p):
        cfg.update(json.load(open(p, encoding="utf-8")))
    for k in ("ffmpeg", "ffprobe", "whisper", "model", "music_dir", "fonts_dir", "sfx_dir", "projects_dir"):
        v = os.path.expanduser(cfg.get(k) or "")
        # un nom nu (ex. "ffmpeg") = programme du PATH ; un chemin relatif = relatif au dossier de l'appli
        if v and not os.path.isabs(v) and ("/" in v or "\\" in v or k in ("model", "music_dir", "fonts_dir", "sfx_dir", "projects_dir")):
            v = os.path.join(APP, v)
        cfg[k] = v
    return cfg


CFG = load_cfg()


def save_cfg(**kv):
    """Enregistre quelques cles dans config.json (sans toucher au reste) et dans CFG."""
    path = os.path.join(APP, "config.json")
    try:
        raw = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    except ValueError:
        raw = {}
    raw.update(kv); CFG.update(kv)
    json.dump(raw, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- lien d'equipe (Cloudflare Tunnel)
# L'outil tourne sur ce PC ; un tunnel Cloudflare lui donne une adresse https accessible de partout.
# Tout acces par le lien demande le mot de passe d'equipe ; sur ce PC (localhost), pas de mot de passe.
TUNNEL = {"proc": None, "url": None, "error": None, "starting": False}


def auth_token():
    if not CFG.get("auth_secret"): save_cfg(auth_secret=secrets.token_hex(24))
    return hmac.new(CFG["auth_secret"].encode(), (CFG.get("team_password") or "").encode(), hashlib.sha256).hexdigest()


def cloudflared_path():
    exe = os.path.join(APP, "bin", "cloudflared.exe" if WIN else "cloudflared")
    if CFG.get("cloudflared") and os.path.exists(CFG["cloudflared"]): return CFG["cloudflared"]
    if os.path.exists(exe): return exe
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    name = "cloudflared-windows-amd64.exe" if WIN else "cloudflared-linux-amd64"
    urllib.request.urlretrieve(f"https://github.com/cloudflare/cloudflared/releases/latest/download/{name}", exe + ".tmp")
    os.replace(exe + ".tmp", exe)
    if not WIN: os.chmod(exe, 0o755)
    return exe


TUNNEL_PID = os.path.join(APP, "bin", "cloudflared.pid")


def kill_old_tunnel():
    """Fenetre fermee brutalement : l'ancien cloudflared peut tourner encore. On l'arrete avant d'en relancer un."""
    try:
        pid = int(open(TUNNEL_PID).read().strip())
    except (OSError, ValueError):
        return
    try:
        if WIN: subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NOWIN)
        else: os.kill(pid, 15)
    except OSError:
        pass
    try: os.remove(TUNNEL_PID)
    except OSError: pass


def start_tunnel():
    if TUNNEL["proc"] and TUNNEL["proc"].poll() is None: return
    kill_old_tunnel()
    TUNNEL.update(url=None, error=None, starting=True)
    def run_():
        try:
            exe = cloudflared_path()
            port = int(CFG.get("port", 8765))
            if CFG.get("tunnel_token"):  # tunnel nomme (adresse fixe, ex. shorts.mgprod.fr)
                cmd = [exe, "tunnel", "--no-autoupdate", "run", "--token", CFG["tunnel_token"]]
                TUNNEL["url"] = CFG.get("tunnel_url")
            else:  # tunnel rapide : adresse https://...trycloudflare.com, differente a chaque demarrage
                cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
            pr = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8", errors="replace", creationflags=NOWIN)
            TUNNEL["proc"] = pr; tail = []
            try: open(TUNNEL_PID, "w").write(str(pr.pid))
            except OSError: pass
            for line in pr.stderr:
                tail = (tail + [line.strip()])[-8:]
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not CFG.get("tunnel_token"): TUNNEL["url"] = m.group(0); TUNNEL["starting"] = False
                if "Registered tunnel connection" in line: TUNNEL["starting"] = False
            TUNNEL.update(url=None, starting=False, error="Le lien s'est arrêté : " + (tail[-1] if tail else "?"))
        except Exception as e:  # noqa
            TUNNEL.update(url=None, starting=False, error=f"Impossible de créer le lien : {e}")
    threading.Thread(target=run_, daemon=True).start()


def stop_tunnel():
    pr = TUNNEL.get("proc")
    if pr and pr.poll() is None:
        pr.terminate()
        try: pr.wait(timeout=5)
        except Exception: pass
    kill_old_tunnel()
    TUNNEL.update(proc=None, url=None, starting=False, error=None)


LOGIN_HTML = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MG Shorts Studio</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#09090B;color:#F4F4F5;font:15px/1.4 system-ui,sans-serif}
form{width:300px;display:grid;gap:12px;padding:28px;border:1px solid #27272A;border-radius:16px;background:#111114}
h1{margin:0 0 4px;font-size:18px}.m{color:#A1A1AA;font-size:13px;margin:0}input,button{font:inherit;padding:11px 12px;border-radius:10px;border:1px solid #3F3F46;background:#18181B;color:#fff}
button{background:#FF6B1A;border-color:#FF6B1A;color:#0B0B0D;font-weight:700;cursor:pointer}.e{color:#F87171;font-size:13px;margin:0}</style></head>
<body><form method="post" action="/login"><h1>MG Shorts Studio</h1><p class="m">Mot de passe de l'équipe</p>
<input type="password" name="password" autofocus autocomplete="current-password" aria-label="Mot de passe">%ERR%<button>Entrer</button></form></body></html>"""


try:
    VERSION = open(os.path.join(APP, "VERSION"), encoding="utf-8").read().strip()
except OSError:
    VERSION = "?"
PROJ = CFG["projects_dir"]
os.makedirs(PROJ, exist_ok=True)
UPLOADS = os.path.join(PROJ, "_envois")  # videos en cours d'envoi par morceaux
os.makedirs(UPLOADS, exist_ok=True)
for _u in os.listdir(UPLOADS):  # envois abandonnes depuis plus d'un jour
    if time.time() - os.path.getmtime(os.path.join(UPLOADS, _u)) > 86400: shutil.rmtree(os.path.join(UPLOADS, _u), ignore_errors=True)
W, H = 1080, 1920

DEFAULT_SETTINGS = {
    "hook": "", "hook_dur": 2.6, "hook_sfx": True,
    "hook_start": 0.0, "hook_font": "Montserrat Black", "hook_size": 124, "hook_y": 0.156, "hook_rot": 0,
    "hook_color": "#FFFFFF", "hook_hl": "#FFE100", "hook_outline": 10, "hook_outline_color": "#000000",
    "hook_shadow": 5, "hook_shadow_color": "#000000", "hook_box": False, "hook_box_color": "#FFE100",
    "hook_in": "pop", "hook_in_ms": 250, "hook_out": "fondu", "hook_out_ms": 150,
    "flash": False, "flash_dur": 0.5, "flash_color": "#FFFFFF", "flash_sfx": True, "flash_db": -6,
    "music": "", "music_db": -26, "music_offset": 0,
    "sub_size": 108, "sub_y": 0.67, "sub_words": 3, "sub_chars": 14, "sub_case": "upper",
    "punch_amount": 1.3, "punch_len": 1.2, "face_y": 0.30, "deghost": True,
    "cut_silences": True, "silence_gap": 0.5, "cut_fillers": True, "snap_cuts": True, "clean_joins": True,
    "sub_style": "karaoke", "sub_hl": "#FFE100", "sub_nopunct": True, "sub_pop": False,
}


# ---------------------------------------------------------------- utilitaires
def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN, **kw)


def probe(path):
    r = run([CFG["ffprobe"], "-v", "error", "-show_entries",
             "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,channels,channel_layout:format=duration", "-of", "json", path])
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    def rate(x):
        try:
            a, b = (x or "0/1").split("/"); return float(a) / float(b or 1)
        except (ValueError, ZeroDivisionError):
            return 0.0
    fps = rate(v.get("r_frame_rate")); avg = rate(v.get("avg_frame_rate"))
    if avg and (not fps or fps > 1.5 * avg): fps = avg  # cadence variable : la vraie moyenne
    fps = fps or 25.0
    return {"duration": float(d.get("format", {}).get("duration", 0) or 0), "width": v.get("width"),
            "height": v.get("height"), "fps": round(fps, 3),
            "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


# ---------------------------------------------------------------- encodeur video (carte graphique si possible)
_ENC = {}


def gpu_encoder():
    """Encodeur materiel utilisable sur ce PC (teste une fois) : h264_nvenc (Nvidia), h264_qsv (Intel), h264_amf (AMD) ou None."""
    if "name" in _ENC: return _ENC["name"]
    want = (CFG.get("encoder") or "auto").lower(); name = None
    cands = [] if want == "cpu" else ([want] if want.startswith("h264_") else ["h264_nvenc", "h264_qsv", "h264_amf"])
    for c in cands:
        pf = "nv12" if c == "h264_qsv" else "yuv420p"
        try:
            r = run([CFG["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=25:d=0.4",
                     "-pix_fmt", pf, "-c:v", c, "-f", "null", "-"], timeout=40)
        except Exception:
            continue
        if r.returncode == 0: name = c; break
    _ENC["name"] = name
    return name


def venc(kind, fps=25):
    """Reglages d'encodage. kind : 'final' (version livree), 'work' (fichier de travail, quasi sans perte), 'light' (export leger)."""
    g = str(max(1, int(round(fps * 2))))  # une image cle toutes les 2 s : lecture et recherche rapides
    q = {"final": 19, "work": 16, "light": 28}[kind]
    e = gpu_encoder()
    if e == "h264_nvenc":
        return ["-c:v", e, "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0", "-g", g, "-pix_fmt", "yuv420p"]
    if e == "h264_qsv":
        return ["-c:v", e, "-preset", "medium", "-global_quality", str(q + 1), "-g", g, "-pix_fmt", "nv12"]
    if e == "h264_amf":
        return ["-c:v", e, "-quality", "quality", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q + 2), "-g", g, "-pix_fmt", "yuv420p"]
    pre = {"final": "fast", "work": "veryfast", "light": "fast"}[kind]
    return ["-c:v", "libx264", "-preset", pre, "-crf", str(q - 1), "-g", g, "-pix_fmt", "yuv420p"]


def with_encoder(fn):
    """Lance fn() ; si la carte graphique echoue (pilote trop ancien...), recommence au processeur et s'en souvient."""
    try:
        return fn()
    except RuntimeError:
        if not _ENC.get("name"): raise
        _ENC["name"] = None
        return fn()


def audio_mix(path, out_ch):
    """Melange explicite de tous les canaux audio (exports Premiere multipistes : un micro par canal, sans
    disposition definie). Les ffmpeg recents refusent de les mixer seuls : 'Error binding filtergraph'."""
    try:
        d = json.loads(run([CFG["ffprobe"], "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=channels,channel_layout", "-of", "json", path]).stdout or "{}")
        a = (d.get("streams") or [{}])[0]
    except Exception:
        a = {}
    n = int(a.get("channels") or 0); lay = a.get("channel_layout") or ""
    if n == 0: return []
    if n == out_ch and lay in ("mono", "stereo"): return []
    if n == 1: return ["-af", "pan=mono|c0=c0" if out_ch == 1 else "pan=stereo|c0=c0|c1=c0"]
    if n == 2 and out_ch == 2: return ["-af", "pan=stereo|c0=c0|c1=c1"]
    g = 1 / n ** 0.5
    mix = "+".join(f"{g:.4f}*c{i}" for i in range(n))
    return ["-af", f"pan=mono|c0={mix}" if out_ch == 1 else f"pan=stereo|c0={mix}|c1={mix}"]


def slug(s):
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")
    return s[:60] or "projet"


def norm(w):
    w = unicodedata.normalize("NFD", w.lower())
    return re.sub(r"[^a-z0-9']", "", "".join(c for c in w if unicodedata.category(c) != "Mn"))


def inside(root, path):
    """Vrai si path est bien dans root (empeche les ../ dans les adresses)."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:  # disques differents sous Windows
        return False


def pdir(pid):
    d = os.path.join(PROJ, os.path.basename(pid))
    if not os.path.isdir(d):
        raise KeyError(pid)
    return d


LOCK = threading.RLock()


def load_project(pid):
    with LOCK:
        p = json.load(open(os.path.join(pdir(pid), "project.json"), encoding="utf-8"))
    s = dict(DEFAULT_SETTINGS); s.update(p.get("settings", {})); p["settings"] = s
    return p


def save_project(p):
    with LOCK:
        path = os.path.join(pdir(p["id"]), "project.json")
        tmp = path + ".tmp"
        json.dump(p, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def list_music():
    d = CFG["music_dir"]
    if not os.path.isdir(d): return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith((".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")))


_SFX = None


def list_sfx():
    global _SFX
    if _SFX is None:
        out, root = [], CFG.get("sfx_dir") or ""
        if root and os.path.isdir(root):
            for dp, _, fs in os.walk(root):
                for f in fs:
                    if f.lower().endswith((".wav", ".mp3", ".aif", ".aiff", ".ogg", ".m4a")):
                        out.append(os.path.relpath(os.path.join(dp, f), root).replace("\\", "/"))
        _SFX = sorted(out)
    return _SFX


def sfx_path(rel):
    root = CFG.get("sfx_dir") or ""
    p = os.path.normpath(os.path.join(root, rel))
    if not root or not inside(root, p): raise KeyError(rel)
    return p


# ---------------------------------------------------------------- jobs (un seul a la fois)
JOBS, Q = {}, queue.Queue()


def new_job(kind, pid, **extra):
    now = time.time()
    for k in [k for k, x in JOBS.items() if x["status"] in ("terminé", "erreur") and now - x.get("ended", now) > 900]:
        JOBS.pop(k, None)  # on ne garde pas indefiniment les taches finies
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "kind": kind, "project": pid, "status": "en attente", "progress": 0,
                 "message": "En attente…", "error": None, "created": time.time(), **extra}
    Q.put(jid)
    return JOBS[jid]


def worker():
    while True:
        jid = Q.get(); j = JOBS[jid]
        j["status"] = "en cours"
        try:
            {"transcribe": do_transcribe, "render": do_render, "light": do_light,
             "moments": do_moments, "clip": do_clip, "publish": do_publish}[j["kind"]](j)
            j["status"], j["progress"], j["message"], j["ended"] = "terminé", 100, "Terminé", time.time()
            try:
                p = load_project(j["project"])
                if p.pop("last_error", None) is not None: save_project(p)
            except Exception:
                pass
        except Exception as e:  # noqa
            j["status"], j["error"], j["ended"] = "erreur", str(e)[-3000:], time.time()
            lines = [l for l in str(e).splitlines() if l.strip()]
            j["message"] = "Erreur : " + (lines[-1][:220] if lines else "inconnue")
            try:
                p = load_project(j["project"])
                p["status"] = "erreur" if j["kind"] in ("transcribe", "moments", "clip") or not p.get("words") else "prêt"
                if j["kind"] == "publish": p["status"] = "prêt" if p.get("words") else p["status"]
                p["last_error"] = {"when": time.time(), "kind": j["kind"], "message": j["message"], "detail": j["error"]}
                save_project(p)
                open(os.path.join(pdir(p["id"]), "derniere_erreur.txt"), "w", encoding="utf-8").write(
                    time.strftime("%Y-%m-%d %H:%M:%S") + f"  [{j['kind']}]\n" + str(e))
            except Exception:
                pass


threading.Thread(target=worker, daemon=True).start()


def busy(pid, children=True):
    """Un traitement est en cours ou prevu pour ce projet (et, si children, pour un short de cette video longue)."""
    return any(x["status"] in ("en attente", "en cours") and (x["project"] == pid or (children and x.get("parent") == pid))
               for x in list(JOBS.values()))


# ---------------------------------------------------------------- passages retires (In / Out)
def norm_cuts(cuts, dur, fps):
    """Nettoie la liste des passages retires : tries, cales sur les images, fusionnes, dans la video."""
    fr = lambda t: round(round(float(t) * fps) / fps, 4)
    out = []
    for c in sorted((sorted([float(c[0]), float(c[1])]) for c in (cuts or []) if len(c) == 2), key=lambda c: c[0]):
        a, b = max(0.0, fr(c[0])), min(fr(dur), fr(c[1]))
        if b - a < 1.5 / fps: continue
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def kept_ranges(cuts, dur):
    """Morceaux gardes [(debut, fin, debut_dans_le_rendu)], dans l'ordre."""
    res, t, acc = [], 0.0, 0.0
    for a, b in cuts:
        if a > t:
            res.append((t, a, acc)); acc += a - t
        t = max(t, b)
    if dur - t > 1e-3: res.append((t, dur, acc))
    return res


def to_output(t, keep):
    """Temps de la source -> temps du rendu (None si ce moment est retire)."""
    for a, b, o in keep:
        if a <= t < b: return o + t - a
    return None


def remap_words(words, keep):
    """Mots gardes, recales sur la duree du rendu (un mot coupe en deux est rogne)."""
    out = []
    for w in words:
        mid = (w["s"] + w["e"]) / 2
        rng = next(((a, b, o) for a, b, o in keep if a <= mid < b), None)
        if not rng: continue
        a, b, o = rng
        out.append(dict(w, s=round(o + max(w["s"], a) - a, 3), e=round(o + min(w["e"], b) - a, 3)))
    return out


FILLER = re.compile(r"^(e+u+h*|h+e+u+h*|e+h+|h*u+m+|h+m+|m{2,}h*|m+h+)$")


def silence_cuts(p):
    """Blancs a couper : pauses plus longues que silence_gap (reperees dans le son, bornees par la transcription)
    et, si cut_fillers, les « euh ». On garde un peu d'air autour des mots pour ne pas les manger."""
    if p.get("kind") == "episode" or not p.get("words"): return []
    st = p.get("settings") or {}; dur = p["duration"]; fps = p.get("fps") or 25
    gap = max(0.2, float(st.get("silence_gap", 0.5))); pad_in, pad_out = 0.09, 0.12
    try:
        wv = waveform(p); rate = wv["rate"]; pk = wv["peaks"]
    except Exception:
        rate, pk = 50, []
    if pk:
        srt = sorted(pk); floor, loud = srt[len(srt) // 10], srt[int(len(srt) * 0.75)]
        thr = max(3, floor + 0.18 * (loud - floor))
    def quiet_runs(t0, t1):
        """Morceaux silencieux dans [t0, t1] d'apres le son (tout l'intervalle sans forme d'onde)."""
        if not pk: return [(t0, t1)]
        i0, i1 = max(0, int(t0 * rate)), min(len(pk), int(t1 * rate) + 1)
        out, st_ = [], None
        for i in range(i0, i1):
            if pk[i] < thr:
                if st_ is None: st_ = i
            elif st_ is not None:
                out.append((st_ / rate, i / rate)); st_ = None
        if st_ is not None: out.append((st_ / rate, i1 / rate))
        return [(max(a, t0), min(b, t1)) for a, b in out]
    fill = bool(st.get("cut_fillers", True))
    ws = [w for w in p["words"] if w["w"].strip()]
    isf = lambda w: fill and bool(FILLER.match(norm(w["w"])))
    speech = [w for w in ws if not isf(w)]
    if not speech: return []
    cuts = []
    # debut et fin du short
    for a, b in quiet_runs(0, speech[0]["s"] + 0.2):
        if a <= 0.05 and b - a > gap: cuts.append([0, min(b, speech[0]["s"]) - pad_in])
    for a, b in quiet_runs(speech[-1]["e"] - 0.2, dur):
        if b >= dur - 0.05 and b - a > gap: cuts.append([max(a, speech[-1]["e"]) + pad_out + 0.1, dur])
    # entre deux mots
    for w0, w1 in zip(speech, speech[1:]):
        t0, t1 = w0["e"], w1["s"]
        for f in (x for x in ws if isf(x) and t0 - 0.05 <= x["s"] < t1):  # un « euh » : on le retire
            cuts.append([max(t0 + 0.04, f["s"] - 0.04), min(t1 - 0.04, f["e"] + 0.06)])
        if t1 - t0 < gap * 0.6: continue
        for a, b in quiet_runs(t0 - 0.15, t1 + 0.15):  # la transcription se trompe un peu : on regarde le son autour
            a, b = max(a, t0 - 0.05), min(b, t1 + 0.05)
            if b - a > gap: cuts.append([a + pad_out, b - pad_in])
    return [c for c in norm_cuts([c for c in cuts if c[1] - c[0] > 0.05], dur, fps)]


def all_cuts(p, st=None):
    """Passages retires a la main + blancs (si la coupe des blancs est activee)."""
    st = st or p.get("settings") or {}
    auto = silence_cuts(p) if st.get("cut_silences", True) else []
    return norm_cuts((p.get("cuts") or []) + auto, p["duration"], p.get("fps") or 25)


def stream_progress(cmd, j, lo, hi, total, cwd=None):
    """Lance ffmpeg avec -progress pipe:1 et fait avancer la barre entre lo et hi."""
    pr = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          cwd=cwd, creationflags=NOWIN)
    err = []
    t = threading.Thread(target=lambda: err.extend(pr.stderr.readlines()), daemon=True); t.start()
    for line in pr.stdout:
        if line.startswith("out_time_us=") and total > 0:
            try:
                us = int(line.split("=")[1]); j["progress"] = lo + (hi - lo) * min(1, us / 1e6 / total)
            except ValueError:
                pass
    pr.wait(); t.join()
    if pr.returncode:
        raise RuntimeError("ffmpeg : " + "".join(err[-15:]))


# ---------------------------------------------------------------- transcription
def do_transcribe(j):
    p = load_project(j["project"]); d = pdir(p["id"])
    p["status"] = "transcription"; save_project(p)
    src = os.path.join(d, p["source"])
    j["message"] = "Extraction du son…"
    r = run([CFG["ffmpeg"], "-y", "-v", "error", "-i", src, "-vn", "-map", "0:a:0"] + audio_mix(src, 1) + ["-ar", "16000", os.path.join(d, "audio16k.wav")])
    if r.returncode: raise RuntimeError(r.stderr)
    # reperage des changements de camera en parallele de la transcription : on ne les attend pas
    sc = {}
    th = threading.Thread(target=lambda: sc.setdefault("v", detect_scenes(src)), daemon=True); th.start()
    j["progress"] = 8; j["message"] = "Transcription Whisper…"
    if not os.path.exists(CFG["model"]):
        raise RuntimeError(f"Modèle Whisper introuvable : {CFG['model']}")
    cmd = [CFG["whisper"], "-m", CFG["model"], "-f", os.path.join(d, "audio16k.wav"), "-l", CFG.get("language", "fr"),
           "-t", str(max(1, min(8, (os.cpu_count() or 4) - 1))), "-ml", "1", "-sow", "-ojf", "-pp",
           "-of", os.path.join(d, "whisper")]
    if CFG.get("whisper_prompt"): cmd += ["--prompt", CFG["whisper_prompt"]]
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", creationflags=NOWIN)
    tail = []
    for line in pr.stdout:
        tail = (tail + [line])[-20:]
        m = re.search(r"progress\s*=\s*(\d+)%", line)
        if m: j["progress"] = 8 + 0.9 * int(m.group(1)); j["message"] = f"Transcription Whisper… {m.group(1)} %"
    pr.wait()
    if pr.returncode: raise RuntimeError("whisper : " + "".join(tail))
    words = parse_whisper(os.path.join(d, "whisper.json"))
    j["message"] = "Calage des sous-titres sur la voix…"
    try:  # Whisper se trompe souvent de 0,2 a 1 s sur le debut des mots : on recale sur le son
        words = align_words(words, voice_env(os.path.join(d, "audio16k.wav"))); aligned = ALIGN_V
    except Exception:
        aligned = 0
    j["message"] = "Repérage des changements de caméra…"; th.join()
    p = load_project(p["id"]); p["words"] = words; p["scenes"] = sc.get("v", []); p["aligned"] = aligned
    if p.get("kind") == "episode" and p.get("moments_mode") != "manual":
        p["status"] = "recherche des moments"; save_project(p); new_job("moments", p["id"])
    else:
        p["status"] = "prêt"; save_project(p)


def detect_scenes(src):
    # decodage allege (-skip_loop_filter, image reduite) : 2 a 3 fois plus rapide sur une video longue
    r = run([CFG["ffmpeg"], "-v", "info", "-skip_loop_filter", "all", "-i", src, "-vf",
             "scale=320:-2:flags=fast_bilinear,select='gt(scene,0.25)',showinfo", "-an", "-f", "null", "-"])
    return sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)})


def parse_whisper(path):
    d = json.load(open(path, encoding="utf-8"))
    fixes = {norm(k): v for k, v in (CFG.get("word_fixes") or {}).items()}
    words = []
    for seg in d.get("transcription", []):
        for t in seg.get("tokens", []):
            txt = t.get("text", "")
            if not txt.strip() or txt.startswith("[_") or txt.startswith("<|"): continue
            s, e = t["offsets"]["from"] / 1000, t["offsets"]["to"] / 1000
            if txt.startswith(" ") or not words:
                words.append({"w": txt.strip(), "s": s, "e": e})
            else:
                words[-1]["w"] += txt; words[-1]["e"] = e
    out = []
    for w in words:
        k = norm(w["w"])
        if k in fixes:
            punct = re.search(r"[^\w']*$", w["w"]).group(0)
            w["w"] = fixes[k] + punct
        if w["w"].strip(): out.append(w)
    return out


# ---------------------------------------------------------------- recalage des mots sur la voix
def voice_env(wav):
    """Volume de la voix toutes les 10 ms (dB), depuis le wav 16 kHz mono de la transcription."""
    import wave as _w
    with _w.open(wav) as f:
        sr = f.getframerate(); raw = f.readframes(f.getnframes())
    data = array.array("h"); data.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder == "big": data.byteswap()
    try:
        import numpy as np
        x = np.frombuffer(data.tobytes(), np.int16).astype(np.float32)
        n = len(x) // (sr // 100); x = x[:n * (sr // 100)].reshape(n, -1)
        return [float(v) for v in 20 * np.log10(np.sqrt((x * x).mean(axis=1)) + 1.0)]
    except ImportError:
        st = sr // 100
        return [20 * math.log10(math.sqrt(sum(v * v for v in data[i:i + st]) / st) + 1.0) for i in range(0, len(data) - st + 1, st)]


def word_len(w):
    """Duree attendue d'un mot (s), d'apres son nombre de lettres."""
    n = len(re.sub(r"[^\w]", "", w))
    return min(1.1, max(0.12, 0.055 * n + 0.06))


def split_run(dl, a, b, env, thr):
    """Frontieres des mots dans un ilot de parole : on choisit parmi les creux du son (micro-pauses, consonnes)
    ceux qui donnent a chaque mot une duree proche de sa longueur attendue (programmation dynamique)."""
    M = len(dl)
    if M == 1: return [a, b]
    f0, f1 = int(round(a * 100)), int(round(b * 100))
    sm = [sum(env[max(0, f - 1):f + 2]) / len(env[max(0, f - 1):f + 2]) for f in range(f0, f1)]
    cand = {}
    for x in range(3, len(sm) - 3):  # creux : minimum local nettement plus bas que ce qui l'entoure
        if sm[x] <= sm[x - 1] and sm[x] <= sm[x + 1]:
            depth = min(max(sm[max(0, x - 10):x]), max(sm[x + 1:x + 11])) - sm[x]
            if sm[x] < thr: depth += 12  # vrai silence
            if depth >= 3: cand[x] = depth
    keep = {}
    for x in sorted(cand, key=lambda x: -cand[x]):  # un seul creux tous les 40 ms
        if all(abs(x - y) > 4 for y in keep): keep[x] = cand[x]
    rate = (b - a) / sum(dl); exp = [d * rate for d in dl]
    acc = 0.0
    for d in exp[:-1]:  # positions proportionnelles, en secours
        acc += d; x = int(round(acc * 100))
        if all(abs(x - y) > 2 for y in keep): keep[x] = 0.0
    pos = sorted(keep); P = len(pos); L = len(sm)
    INF = float("inf")
    def seg(m, x0, x1):
        ln = max(0.02, (x1 - x0) / 100); r = math.log(ln / exp[m])
        return r * r
    # best[m][p] : cout des m premiers mots, la frontiere m tombant en pos[p]
    best = [[INF] * P for _ in range(M)]; arg = [[-1] * P for _ in range(M)]
    for p in range(P): best[1][p] = seg(0, 0, pos[p]) - 0.045 * keep[pos[p]]
    for m in range(2, M):
        for p in range(P):
            for q in range(max(0, p - 40), p):
                if best[m - 1][q] == INF: continue
                c = best[m - 1][q] + seg(m - 1, pos[q], pos[p]) - 0.045 * keep[pos[p]]
                if c < best[m][p]: best[m][p], arg[m][p] = c, q
    fin = [(best[M - 1][p] + seg(M - 1, pos[p], L), p) for p in range(P) if best[M - 1][p] < INF]
    if not fin:
        acc, out = a, [a]
        for d in exp: acc += d; out.append(acc)
        return out
    _, p = min(fin); bs = []
    for m in range(M - 1, 0, -1):
        bs.append(pos[p]); p = arg[m][p]
    return [a] + [f0 / 100 + x / 100 for x in reversed(bs)] + [b]


ALIGN_V = 1  # version du recalage : les projets plus anciens sont recales une fois a l'ouverture


def realign(p):
    """Recale les mots du projet sur la voix de sa propre video (garde corrections, jaune, zooms, bruitages)."""
    d = pdir(p["id"]); src = os.path.join(d, p["source"]); wav = os.path.join(d, "audio16k.wav")
    if not p.get("words") or not os.path.exists(src) or not p.get("has_audio", True): return p
    if not os.path.exists(wav) or os.path.getmtime(wav) < os.path.getmtime(src):
        r = run([CFG["ffmpeg"], "-y", "-v", "error", "-i", src, "-vn", "-map", "0:a:0"] + audio_mix(src, 1) + ["-ar", "16000", wav])
        if r.returncode: return p
    p["words"] = align_words(p["words"], voice_env(wav)); p["aligned"] = ALIGN_V
    return p


def align_words(words, env):
    """Recale les mots de Whisper sur la voix. Whisper donne le bon texte mais des temps approximatifs
    (souvent 0,2 a 1 s d'ecart). On repere dans le son les ilots de parole, on y range les mots dans l'ordre
    (programmation dynamique : proche du temps Whisper, duree coherente), puis on place chaque mot dans son
    ilot selon sa longueur, en calant les frontieres sur les creux du son."""
    if not words or not env: return words
    n = len(env); srt = sorted(env)
    floor, loud = srt[n // 10], srt[int(n * 0.95)]
    if loud - floor < 10: return words  # pas de voix nette (musique continue...) : on ne touche a rien
    thr = floor + max(6.0, 0.28 * (loud - floor))
    runs, st_ = [], None
    for i, v in enumerate(env + [-1e9]):
        if v > thr and st_ is None: st_ = i
        elif v <= thr and st_ is not None: runs.append([st_, i]); st_ = None
    merged = []
    for a, b in runs:  # coupures de moins de 250 ms : meme ilot (les mots y sont ensuite separes sur les creux du son)
        if merged and a - merged[-1][1] < 25: merged[-1][1] = b
        else: merged.append([a, b])
    runs = [(a / 100, b / 100) for a, b in merged if b - a >= 6]
    if not runs: return words
    R, N = len(runs), len(words)
    dl = [word_len(w["w"]) for w in words]
    starts = [r[0] for r in runs]
    import bisect
    def cands(i):
        w = words[i]; lo, hi = w["s"] - 2.5, w["e"] + 2.5
        k = max(0, bisect.bisect_right(starts, lo) - 1); out = []
        while k < R and runs[k][0] <= hi:
            if runs[k][1] >= lo: out.append(k)
            k += 1
        return out
    C = [set(cands(i)) for i in range(N)]
    INF = float("inf"); dp = {(0, -1): (0.0, None)}
    for i in range(N):
        for (ii, k), (c0, _) in sorted(((key, v) for key, v in dp.items() if key[0] == i), key=lambda z: z[1][0])[:12]:
            if not C[i]:  # mot sans ilot de parole proche : garde son temps Whisper
                key = (i + 1, k); c = c0 + 1.0
                if c < dp.get(key, (INF,))[0]: dp[key] = (c, (i, k, None))
                continue
            for k2 in sorted(x for x in C[i] if x > k):
                a, b = runs[k2]; skip = sum(min(0.6, runs[m][1] - runs[m][0]) for m in range(k + 1, k2)) * 0.25
                tc = 0.0; dsum = 0.0; pen = 0.0
                for j in range(i, min(N, i + 45)):
                    if k2 not in C[j]: break
                    cw = (words[j]["s"] + words[j]["e"]) / 2
                    tc += max(0.0, a - cw, cw - b); dsum += dl[j]
                    c = c0 + skip + tc + pen + 0.6 * abs(dsum - (b - a))
                    # une fin de phrase au milieu d'un ilot est peu probable (penalite pour les mots suivants)
                    pen += 1.0 if re.search(r"[.!?…]$", words[j]["w"]) else 0.3 if words[j]["w"].endswith(",") else 0.0
                    key = (j + 1, k2)
                    if c < dp.get(key, (INF,))[0]: dp[key] = (c, (i, k, k2))
    ends = [(v[0] + sum(min(0.6, runs[m][1] - runs[m][0]) for m in range(k + 1, R)) * 0.25, (j, k)) for (j, k), v in dp.items() if j == N]
    if not ends: return words
    blocks, key = [], min(ends)[1]
    back = {kk: v for kk, v in dp.items()}
    while key[0] > 0:
        _, prev = back[key]
        i, k, k2 = prev; blocks.append((i, key[0], k2)); key = (i, k)
    out = [dict(w) for w in words]
    for i, j, k in reversed(blocks):
        if k is None: continue
        a, b = runs[k]
        bounds = split_run(dl[i:j], a, b, env, thr)
        for m in range(i, j):
            s0, e0 = bounds[m - i], bounds[m - i + 1]
            out[m]["s"], out[m]["e"] = round(s0, 3), round(max(s0 + 0.05, e0 - 0.01), 3)
    for m in range(1, len(out)):  # ordre garanti
        if out[m]["s"] < out[m - 1]["s"]: out[m]["s"] = out[m - 1]["s"] + 0.01
        if out[m]["e"] < out[m]["s"] + 0.05: out[m]["e"] = out[m]["s"] + 0.05
    return out


# ---------------------------------------------------------------- sous-titres (ASS)
def ts(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def chunks(words, max_words, max_chars):
    res, cur = [], []
    for wd in words:
        if cur:
            gap = wd["s"] - cur[-1]["e"]; text = " ".join(x["w"] for x in cur + [wd])
            if (wd.get("br") or len(cur) >= max_words or len(text) > max_chars or gap > 0.45 or re.search(r"[.!?,…]$", cur[-1]["w"])):
                res.append(cur); cur = []
        cur.append(wd)
    if cur: res.append(cur)
    return res


YEL, WHI = "&H0000E1FF&", "&H00FFFFFF&"
PUNCT = re.compile(r"[.,;:!?…«»\"“”()\[\]¿¡]+")  # ponctuation retiree des sous-titres (les apostrophes restent)


def ass_col(hexcol, alpha="00"):
    h = (hexcol or "#FFFFFF").lstrip("#")
    if len(h) == 3: h = "".join(c * 2 for c in h)
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def hook_anim(st):
    """Balises ASS d'entree / sortie de l'accroche."""
    dur = int(float(st["hook_dur"]) * 1000); a = max(60, int(st.get("hook_in_ms", 250))); o = max(60, int(st.get("hook_out_ms", 150)))
    x, y = W // 2, int(H * float(st.get("hook_y", 0.156)))
    tags = f"\\an8\\pos({x},{y})"
    rot = float(st.get("hook_rot", 0) or 0)
    if rot: tags += f"\\frz{-rot:g}"
    kind = st.get("hook_in", "pop")
    if kind == "pop":
        tags += f"\\fscx40\\fscy40\\t(0,{int(a*.6)},\\fscx110\\fscy110)\\t({int(a*.6)},{a},\\fscx100\\fscy100)"
    elif kind == "bounce":
        k = [0, .35, .6, .8, 1.0]
        tags += (f"\\fscx0\\fscy0\\t(0,{int(a*k[1])},\\fscx125\\fscy125)\\t({int(a*k[1])},{int(a*k[2])},\\fscx88\\fscy88)"
                 f"\\t({int(a*k[2])},{int(a*k[3])},\\fscx106\\fscy106)\\t({int(a*k[3])},{a},\\fscx100\\fscy100)")
    elif kind == "fondu":
        tags += f"\\alpha&HFF&\\t(0,{a},\\alpha&H00&)"
    elif kind == "zoom":
        tags += f"\\fscx260\\fscy260\\alpha&HFF&\\t(0,{a},0.6,\\fscx100\\fscy100\\alpha&H00&)"
    elif kind == "glisse":
        tags = tags.replace(f"\\pos({x},{y})", f"\\move({x},{y - 420},{x},{y},0,{a})")
    elif kind == "secousse":
        r0 = -rot; q = a // 4
        tags += (f"\\t(0,{q},\\frz{r0-8:g})\\t({q},{2*q},\\frz{r0+7:g})\\t({2*q},{3*q},\\frz{r0-4:g})\\t({3*q},{a},\\frz{r0:g})")
    # "cut" : rien
    out = st.get("hook_out", "fondu")
    if out == "fondu":
        tags += f"\\t({dur-o},{dur},\\alpha&HFF&)"
    elif out == "pop":
        tags += f"\\t({dur-o},{dur},\\fscx0\\fscy0)"
    elif out == "glisse" and kind != "glisse":
        tags = tags.replace(f"\\pos({x},{y})", f"\\move({x},{y},{x},{y - 420},{dur-o},{dur})")
    return "{" + tags + "}"


def hook_to_ass(text, hl=None, base=None):
    t = text.strip().upper().replace("{", "").replace("}", "")
    y, w = ass_col(hl or "#FFE100") + "&", ass_col(base or "#FFFFFF") + "&"
    t = re.sub(r"\*([^*]+)\*", lambda m: "{\\c" + y + "}" + m.group(1) + "{\\c" + w + "}", t)
    return t.replace("\r", "").replace("\n", "\\N").replace("/", "\\N")


def build_ass(words, total, st, path):
    size = int(st["sub_size"]); mv = int(H * (1 - float(st["sub_y"])))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Montserrat Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{max(2, round(size / 13.5))},{max(1, round(size / 27))},2,50,50,{mv},1
Style: Hook,{st.get("hook_font","Montserrat Black")},{int(st.get("hook_size",124))},{ass_col(st.get("hook_color"))},&H00FFFFFF,{ass_col(st.get("hook_box_color") if st.get("hook_box") else st.get("hook_outline_color"))},{ass_col(st.get("hook_shadow_color"), "20")},0,0,0,0,100,100,0,0,{3 if st.get("hook_box") else 1},{max(0, float(st.get("hook_outline",10))) if not st.get("hook_box") else max(8, float(st.get("hook_outline",10)) + 10)},{max(0, float(st.get("hook_shadow",5)))},8,50,50,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    style = st.get("sub_style", "karaoke")
    nop = st.get("sub_nopunct", True)  # sans ponctuation : un « ! » ou « ? » seul disparait
    cs = chunks([w for w in words if w["w"].strip() and not (nop and not PUNCT.sub("", w["w"]).strip())],
                1 if style == "mot" else int(st["sub_words"]), int(st["sub_chars"]))
    up = st.get("sub_case", "upper") == "upper"
    HL = ass_col(st.get("sub_hl") or "#FFE100") + "&"
    anim_on = bool(st.get("sub_pop", False))  # sans animation : sous-titres fixes
    POP = "\\fscx55\\fscy55\\t(0,70,\\fscx114\\fscy114)\\t(70,140,\\fscx100\\fscy100)" if anim_on else ""
    for ci, c in enumerate(cs):
        nxt = cs[ci + 1][0]["s"] if ci + 1 < len(cs) else total
        c_end = nxt if nxt - c[-1]["e"] < 0.35 else c[-1]["e"] + 0.15
        for wi, wd in enumerate(c):
            s = wd["s"] if wi else c[0]["s"]
            e = c[wi + 1]["s"] if wi + 1 < len(c) else c_end
            if e <= s: e = s + 0.05
            parts = []
            for wj, x in enumerate(c):
                t = (x["w"].upper() if up else x["w"]).replace("{", "").replace("}", "")
                if st.get("sub_nopunct", True): t = PUNCT.sub("", t).strip() or t
                col = HL if x.get("hl") else WHI
                if style == "progressif" and wj > wi:  # pas encore dit : invisible mais garde sa place
                    parts.append("{\\alpha&HFF&}" + t + "{\\alpha&H00&}")
                elif style == "progressif" and wj == wi:
                    parts.append("{\\c" + HL + POP + "}" + t + "{\\c" + WHI + "\\fscx100\\fscy100}")
                elif style == "karaoke" and wj == wi:
                    grow = "\\fscx108\\fscy108" if anim_on else ""
                    parts.append("{\\c" + HL + grow + "}" + t + "{\\c" + WHI + "\\fscx100\\fscy100}")
                elif col != WHI:
                    parts.append("{\\c" + col + "}" + t + "{\\c" + WHI + "}")
                else:
                    parts.append(t)
            if style == "simple": e = c_end  # tout le bloc d'un coup
            if style == "mot": anim = "{" + POP + "}" if POP else ""
            elif style == "progressif" or not anim_on: anim = ""
            else: anim = "{\\fscx80\\fscy80\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
            ev.append(f"Dialogue: 1,{ts(s)},{ts(e)},Sub,,0,0,0,,{anim}{' '.join(parts)}")
            if style == "simple": break  # une seule ligne par bloc, sans mot courant
    
    if st.get("hook", "").strip():
        t0 = float(st.get("hook_start", 0) or 0)
        ev.append(f"Dialogue: 2,{ts(t0)},{ts(t0 + float(st['hook_dur']))},Hook,,0,0,0,,"
                  + hook_anim(st) + hook_to_ass(st["hook"], st.get("hook_hl"), st.get("hook_color")))
    open(path, "w", encoding="utf-8").write(head + "\n".join(ev) + "\n")


# ---------------------------------------------------------------- rendu
# ---------------------------------------------------------------- images fantomes
def scan_ghosts(src, fps, a=0.0, dur=None):
    """Repere les images fantomes de la video : image de fondu entre deux plans a une coupe
    (fusion d'images de Premiere) ou image isolee d'un autre plan. Renvoie les numeros d'image
    (0 = image a l'instant a). Sans numpy : liste vide."""
    try:
        import numpy as np
    except Exception:
        return []
    ww, hh = 96, 54
    cmd = [CFG["ffmpeg"], "-v", "error", "-ss", f"{a:.3f}", "-i", src]
    if dur: cmd += ["-t", f"{dur:.3f}"]
    # fps en tete et sortie image par image : memes numeros d'image que le filtre du rendu (select puis crop)
    cmd += ["-an", "-vf", f"fps={fps},scale={ww}:{hh}:flags=area,format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    fr = []
    while True:
        buf = pr.stdout.read(ww * hh)
        if len(buf) < ww * hh: break
        fr.append(np.frombuffer(buf, np.uint8))
    pr.wait()
    return find_ghosts(fr)


def find_ghosts(small):
    """Fantomes parmi des images miniatures (96x54, gris, uint8), numerotees comme le rendu."""
    import numpy as np
    n = len(small)
    if n < 5: return []
    fr = [x.astype(np.float32) for x in small]
    dist = lambda i, k: float(np.abs(fr[i] - fr[k]).mean())
    d1 = [0.0] + [dist(i, i - 1) for i in range(1, n)]
    ghosts = set()
    for j in range(1, n - 1):
        if j in ghosts: continue
        around = sorted(d1[k] for k in range(max(1, j - 12), min(n, j + 14)) if k < j - 1 or k > j + 3)
        base = around[len(around) // 2] if around else 0.0
        for k in (1, 2):  # fantome d'une ou deux images
            if j + k >= n: break
            P, N = fr[j - 1], fr[j + k]; run_ = range(j, j + k)
            C = dist(j - 1, j + k)
            far = max(4.0, 4 * base + 2.5)
            # image isolee : avant et apres se ressemblent, l'image du milieu vient d'ailleurs
            ab = [min(dist(i, j - 1), dist(i, j + k)) for i in run_]
            if min(ab) >= far and (C < 0.35 * min(ab) or (C < 0.7 * min(ab) and C <= 1.5 * (k + 1) * base + 2)):
                ghosts.update(run_); break
            # image de fondu a une coupe : melange de l'image d'avant et de celle d'apres, plans stables autour
            if C >= max(4.0, 4 * base + 2) and d1[j - 1] < 0.3 * C and (j + k + 1 >= n or d1[j + k + 1] < 0.3 * C):
                D = P - N; dd = float((D * D).sum()) or 1.0; ok = True
                for i in run_:
                    al = float(((fr[i] - N) * D).sum()) / dd
                    res = float(np.abs(fr[i] - (al * P + (1 - al) * N)).mean())
                    if not (0.12 < al < 0.88 and res < 0.2 * C + 1): ok = False; break
                if ok:
                    ghosts.update(run_); break
    return sorted(g for g in ghosts if 0 < g < n - 1)


def drop_vf(idx, fps):
    """Debut de filtre qui retire ces images ; fps comble avec l'image d'avant : duree et son intacts.
    Le premier fps donne aux images les memes numeros que scan_ghosts et speaker_plan."""
    if not idx: return f"fps={fps},"
    return f"fps={fps},select='not(" + "+".join(f"eq(n,{i})" for i in idx) + f")',fps={fps},"


def source_ghosts(p, fps):
    """Fantomes de la source d'un short, gardes en cache tant que la source ne change pas."""
    src = os.path.join(pdir(p["id"]), p["source"]); cache = os.path.join(pdir(p["id"]), "ghosts.json")
    key = f"{os.path.getsize(src)}:{int(os.path.getmtime(src))}"
    try:
        c = json.load(open(cache))
        if c.get("key") == key: return c["frames"]
    except Exception:
        pass
    g = scan_ghosts(src, fps)
    try: json.dump({"key": key, "frames": g}, open(cache, "w"))
    except OSError: pass
    return g


def frame_at(p, t, st=None):
    """Cadrage a l'instant t (temps du short) : centre (x, y) entre 0 et 1, ou y None.
    Recadrage a la main du clip (p["reframe"]) sinon plan automatique (p["frames"])."""
    x, y = 0.5, None
    for f in p.get("frames") or []:
        if f[0] <= t + 1e-3: x, y = f[1], f[2]  # tolerance 1 ms : les temps sont arrondis
        else: break
    for r in p.get("reframe") or []:
        if r["a"] - 1e-3 <= t < r["b"] - 1e-3:
            if r.get("x") is not None: x = r["x"]
            if r.get("y") is not None: y = r["y"]
    return x, y


def cut_frames(p):
    """Coupes de camera dans la video du short (numeros d'image), reperees image par image. En cache."""
    src = os.path.join(pdir(p["id"]), p["source"]); fps = p.get("fps") or 25
    cache = os.path.join(pdir(p["id"]), "coupes.json")
    key = f"{os.path.getsize(src)}:{int(os.path.getmtime(src))}"
    try:
        c = json.load(open(cache))
        if c.get("key") == key: return c["frames"]
    except Exception:
        pass
    try:
        import numpy as np
    except ImportError:
        return []
    ww, hh = 96, 54
    pr = subprocess.Popen([CFG["ffmpeg"], "-v", "error", "-i", src, "-an", "-vf", f"fps={fps},scale={ww}:{hh}:flags=area,format=gray",
                           "-fps_mode", "passthrough", "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    fr = []
    while True:
        buf = pr.stdout.read(ww * hh)
        if len(buf) < ww * hh: break
        fr.append(np.frombuffer(buf, np.uint8).astype(np.float32))
    pr.wait()
    n = len(fr); d1 = [0.0] + [float(np.abs(fr[i] - fr[i - 1]).mean()) for i in range(1, n)]
    out = []
    for j in range(1, n):
        around = sorted(d1[k] for k in range(max(1, j - 10), min(n, j + 11)) if abs(k - j) > 1)
        base = around[len(around) // 2] if around else 0.0
        nb = max(d1[j - 1] if j > 1 else 0.0, d1[j + 1] if j + 1 < n else 0.0)
        if d1[j] >= 3 and d1[j] > 2.5 * base + 2 and d1[j] > 1.6 * nb: out.append(j)
        # coupe etalee sur 2 images (fondu d'une image) : on prend la 2e
        elif j + 1 < n and d1[j] >= 2 and d1[j + 1] >= 2 and float(np.abs(fr[j + 1] - fr[j - 1]).mean()) > max(4, 3 * base + 2) \
                and d1[j - 1] < 0.4 * d1[j] and (j + 2 >= n or d1[j + 2] < 0.4 * d1[j + 1]):
            out.append(j + 1)
    out = sorted(set(out))
    try: json.dump({"key": key, "frames": out}, open(cache, "w"))
    except OSError: pass
    return out


def snap_to_cuts(p):
    """Option « Caler le cadrage sur les coupes » : tout changement de cadrage (automatique ou fait a la main)
    a moins de 8 images d'une coupe de camera tombe exactement sur la coupe. Plus d'image mal cadree au raccord."""
    if not p.get("wide") or not p.get("frames"): return p
    raw = p.get("frames_raw") or p["frames"]; p["frames_raw"] = raw
    rraw = p.get("reframe_raw") if p.get("reframe_raw") is not None else None
    if not (p.get("settings") or {}).get("snap_cuts", True):
        p["frames"] = [list(f) for f in raw]; return p
    fps = p.get("fps") or 25
    try:
        cuts = cut_frames(p)
    except Exception:
        cuts = []
    import bisect
    def snap(t):
        f = round(t * fps)
        if not cuts or f <= 0: return t
        k = bisect.bisect_left(cuts, f); near = [c for c in cuts[max(0, k - 1):k + 1] if abs(c - f) <= 8]
        return round(min(near, key=lambda c: abs(c - f)) / fps, 6) if near else t
    fr = []
    for f in raw:
        t = 0.0 if f[0] <= 0 else snap(f[0])
        if fr and abs(fr[-1][0] - t) < 1e-6: fr[-1] = [t, f[1], f[2]]
        else: fr.append([t, f[1], f[2]])
    p["frames"] = fr
    src = rraw if rraw is not None else p.get("reframe") or []
    p["reframe"] = [dict(r, a=snap(r["a"]) if r["a"] > 0 else 0.0, b=snap(r["b"]) if r["b"] < p["duration"] - 1e-3 else r["b"]) for r in src]
    return p


def find_orphans(path, fps):
    """Images orphelines dans une video FINIE : une image isolee qui ne ressemble ni a celle d'avant ni a celle
    d'apres (mauvais cadre au raccord, image fantome, image d'un autre plan). Renvoie [(image, image_de_remplacement)].
    On n'analyse que le haut de l'image (les sous-titres changent souvent en bas)."""
    import numpy as np
    ww, hh = 96, 54
    pr = subprocess.Popen([CFG["ffmpeg"], "-v", "error", "-i", path, "-an", "-vf",
                           f"crop=iw:ih*0.6:0:0,scale={ww}:{hh}:flags=area,format=gray", "-fps_mode", "passthrough",
                           "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    fr = []
    while True:
        buf = pr.stdout.read(ww * hh)
        if len(buf) < ww * hh: break
        fr.append(np.frombuffer(buf, np.uint8).astype(np.float32))
    pr.wait()
    n = len(fr)
    find_orphans.count = n
    if n < 6: return []
    dist = lambda i, k: float(np.abs(fr[i] - fr[k]).mean())
    d1 = [0.0] + [dist(i, i - 1) for i in range(1, n)]
    out = []
    for j in range(2, n - 2):
        a, b = d1[j], d1[j + 1]  # saut en entrant dans l'image j, et en sortant
        around = sorted(d1[k] for k in range(max(1, j - 10), min(n, j + 12)) if k < j - 1 or k > j + 2)
        base = around[len(around) // 2] if around else 0.0
        T = max(5.0, 4 * base + 3)
        if a < T or b < T: continue
        if d1[j - 1] > 0.35 * min(a, b) or d1[j + 2] > 0.35 * min(a, b): continue  # pas isolee : mouvement normal
        out.append((j, j - 1 if dist(j, j - 1) < dist(j, j + 1) else j + 1))
    return out


def fix_orphans(path, fps, j=None):
    """Remplace chaque image orpheline par sa voisine la plus proche (duree et son intacts)."""
    orph = find_orphans(path, fps); nf = find_orphans.count
    if not orph: return 0
    if j is not None: j["message"] = f"Nettoyage des raccords ({len(orph)} image{'s' if len(orph) > 1 else ''})…"
    n = len(orph); fc = [f"[0:v]split={n + 1}[m0]" + "".join(f"[r{i}]" for i in range(n))]
    for i, (f, rpl) in enumerate(orph):
        fc.append(f"[m{i}][r{i}]freezeframes=first={f}:last={f}:replace={rpl}[m{i + 1}]")
    tmp = path + ".net.mkv"
    with_encoder(lambda: stream_progress([CFG["ffmpeg"], "-y", "-v", "error", "-i", path, "-filter_complex", ";".join(fc),
                                          "-map", f"[m{n}]", "-map", "0:a?", "-frames:v", str(nf)] + venc("final", fps) + ["-c:a", "copy", tmp],
                                         j or {}, 93, 94, 1))
    os.replace(tmp, path)
    return n


def crop_vf(p, st, sg, f0, f1, fps):
    """crop + scale d'un plan du rendu (images f0..f1 du short) : fenetre 9:16 (zoom compris) qui suit
    le cadrage clip par clip, change a l'image pres. Formule a plat (somme d'ecarts) : pas de limite."""
    iw, ih = int(p.get("width") or W), int(p.get("height") or H); z = float(sg["zoom"])
    if iw / ih >= 9 / 16: ch0, cw0 = ih, ih * 9 / 16
    else: cw0, ch0 = iw, iw * 16 / 9
    cw, ch = max(2, int(cw0 / z) // 2 * 2), max(2, int(ch0 / z) // 2 * 2)
    times = {f0}
    for f in p.get("frames") or []: times.add(round(f[0] * fps))
    for r in p.get("reframe") or []: times |= {round(r["a"] * fps), round(r["b"] * fps)}
    pts = sorted(t for t in times if f0 <= t < f1)
    face = float(st.get("face_y", 0.3))
    xs, ys = [], []
    for f in pts:
        cx, cy = frame_at(p, f / fps)
        cyz = sg["cy"] if cy is None else (cy - 0.06 if z > 1 else cy)
        if cy is None and z == 1: cyz = face
        x = int(min(max(cx * iw - cw / 2, 0), iw - cw)); y = int(min(max(cyz * ih - ch / 2, 0), ih - ch))
        if not xs or xs[-1][1] != x: xs.append((f - f0, x))
        if not ys or ys[-1][1] != y: ys.append((f - f0, y))
    ex = str(xs[0][1]) + "".join(f"{b_ - a_:+d}*gte(n,{k})" for (_, a_), (k, b_) in zip(xs, xs[1:]))
    ey = str(ys[0][1]) + "".join(f"{b_ - a_:+d}*gte(n,{k})" for (_, a_), (k, b_) in zip(ys, ys[1:]))
    return f"crop={cw}:{ch}:'{ex}':'{ey}',scale={W}:{H}:flags=lanczos"


def plan_segments(p, st):
    dur, fps = p["duration"], p.get("fps") or 25
    fr = lambda t: round(round(t * fps) / fps, 4)
    punches = [(fr(max(0, w["s"] - 0.04)), fr(min(dur, max(w["e"], w["s"] + float(st["punch_len"])))))
               for w in p.get("words", []) if w.get("zoom")]
    pts = {0.0, fr(dur)}
    for a, b in punches: pts |= {a, b}
    pts = sorted(t for t in pts if 0 <= t <= fr(dur))
    segs = []
    for a, b in zip(pts, pts[1:]):
        if b - a < 1.5 / fps: continue
        mid = (a + b) / 2
        if any(x <= mid < y for x, y in punches):
            segs.append({"s": a, "e": b, "zoom": float(st["punch_amount"]), "cy": max(0.0, float(st["face_y"]) - 0.06)})
        else:
            segs.append({"s": a, "e": b, "zoom": 1.0, "cy": float(st["face_y"])})
    # un plan trop court (< 0,5 s) hors zoom-mot reprend le zoom du plan precedent : evite les sautes d'image
    for i, s in enumerate(segs):
        if s["e"] - s["s"] < 0.5 and s["cy"] == float(st["face_y"]) and i > 0:
            s["zoom"], s["cy"] = segs[i - 1]["zoom"], segs[i - 1]["cy"]
    # fusionne les voisins de meme zoom (moins de fichiers)
    m = []
    for s in segs:
        if m and abs(m[-1]["zoom"] - s["zoom"]) < 1e-6 and abs(m[-1]["e"] - s["s"]) < 1e-3:
            m[-1]["e"] = s["e"]
        else:
            m.append(dict(s))
    return m


def do_render(j):
    """Rendu en une seule passe ffmpeg : chaque plan (zoom) est une entree, les passages retires et les blancs
    sont enleves a l'image pres (select / aselect), puis concat, sous-titres, flash, musique, bruitages."""
    p = load_project(j["project"]); d = pdir(p["id"])
    st = dict(p["settings"]); st.update(j.get("settings") or {})
    if p.get("wide") and p.get("frames"):
        with LOCK:
            p = snap_to_cuts(load_project(p["id"])); save_project(p)
    n = max([v["n"] for v in p.get("versions", [])] + [0]) + 1
    vdir = os.path.join(d, "versions"); os.makedirs(vdir, exist_ok=True)
    work = os.path.join(d, "_rendu"); shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
    src = os.path.join(d, p["source"]); fps = p.get("fps") or 25; has_a = p.get("has_audio", True)
    keep = kept_ranges(all_cuts(p, st), p["duration"])
    total = sum(round((b - a) * fps) for a, b, _ in keep) / fps
    if total < 1: raise RuntimeError("Il reste moins d'une seconde après les passages retirés.")
    words = remap_words(p.get("words", []), keep)
    build_ass(words, total, st, os.path.join(work, "subs.ass"))
    ghosts = []
    if st.get("deghost", True):
        j["message"] = "Repérage des images fantômes…"; ghosts = source_ghosts(p, fps)
    j["message"] = "Préparation du montage…"; j["progress"] = 3
    pan = ""
    am = audio_mix(src, 2) if has_a else []
    if am: pan = am[1] + ","
    inputs, fc, vlab, nseg = [], [], [], 0
    for sg in plan_segments(p, st):
        f0, f1 = round(sg["s"] * fps), round(sg["e"] * fps)
        rngs = [(max(round(a * fps), f0) - f0, min(round(b * fps), f1) - f0) for a, b, _ in keep]
        rngs = [(x, y) for x, y in rngs if y > x]
        if not rngs: continue
        k = nseg; nseg += 1
        inputs += ["-ss", f"{f0 / fps:.4f}", "-t", f"{(f1 - f0 + 3) / fps:.4f}", "-i", src]
        loc = [g - f0 for g in ghosts if f0 < g < f1 - 1]
        sel = "+".join(f"between(n,{x},{y - 1})" for x, y in rngs)
        # cadrage avant la selection : n = numero d'image du short, les recadrages tombent a l'image pres
        fc.append(f"[{k}:v]" + (drop_vf(loc, fps) if loc else f"fps={fps},") + crop_vf(p, st, sg, f0, f1, fps)
                  + f",select='{sel}',setpts=N/FRAME_RATE/TB,setsar=1[v{k}]")
        if has_a:  # son coupe a la milliseconde, cale sur les memes images
            asel = "+".join(f"between(t,{x / fps - 0.0005:.4f},{y / fps - 0.0005:.4f})" for x, y in rngs)
            fc.append(f"[{k}:a]{pan}aresample=48000,aformat=channel_layouts=stereo,asetnsamples=n=48:p=0,"
                      f"aselect='{asel}',asetpts=N/SR/TB[a{k}]")
            vlab.append(f"[v{k}][a{k}]")
        else:
            vlab.append(f"[v{k}]")
    fc.append("".join(vlab) + f"concat=n={nseg}:v=1:a={1 if has_a else 0}" + ("[vcat][acat]" if has_a else "[vcat]"))
    # polices copiees a cote des sous-titres : chemin relatif simple, fonctionne meme si les projets sont sur un autre disque
    shutil.copytree(CFG["fonts_dir"], os.path.join(work, "fonts"), dirs_exist_ok=True)
    if st.get("flash"):
        fd = max(0.05, float(st.get("flash_dur", 0.5)))
        col = (st.get("flash_color") or "#FFFFFF").lstrip("#")
        fc += ["[vcat]ass=subs.ass:fontsdir=fonts[vsub]",
               f"color=c=0x{col}:s={W}x{H}:r={fps}:d={fd:.3f},format=rgba,fade=t=out:st=0:d={fd:.3f}:alpha=1[fl]",
               "[vsub][fl]overlay=eof_action=pass:format=auto[vout]"]
    else:
        fc.append("[vcat]ass=subs.ass:fontsdir=fonts[vout]")
    music = st.get("music")
    use_music = bool(music) and os.path.exists(os.path.join(CFG["music_dir"], music))
    idx = nseg
    if has_a:
        # la 2e branche sert seulement a baisser la musique sous les voix : sans musique, pas de branche en l'air
        fc.append("[acat]asplit=2[voice][vsc]" if use_music else "[acat]anull[voice]"); mix = ["[voice]"]
    else:
        fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f}[voice]"); mix = ["[voice]"]
    if use_music:
        off = float(st.get("music_offset") or 0)
        inputs += ["-stream_loop", "-1", "-i", os.path.join(CFG["music_dir"], music)]
        fc.append(f"[{idx}:a]atrim={off}:{off + total + 1},asetpts=PTS-STARTPTS,aresample=48000,"
                  f"volume={float(st['music_db'])}dB,afade=t=in:d=0.3,afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[mus0]")
        fc.append("[mus0][vsc]sidechaincompress=threshold=0.03:ratio=4:attack=20:release=350[mus]"
                  if has_a else "[mus0]anull[mus]")
        mix.append("[mus]"); idx += 1
    sfx = []
    if st.get("flash") and st.get("flash_sfx"):
        sfx.append({"file": "app:Flash_good_one.wav", "t": 0.0, "gain_db": float(st.get("flash_db", -6))})
    if st.get("hook_sfx") and st.get("hook", "").strip():
        hs = st.get("hook_sfx_file") or next((x for x in list_sfx() if x.endswith("(17) Whoosh Pop.mp3")), None)
        if hs: sfx.append({"file": hs, "t": float(st.get("hook_start", 0) or 0), "gain_db": -10})
    for w in words:
        if w.get("sfx"): sfx.append({"file": w["sfx"], "t": w["s"], "gain_db": float(w.get("sfx_db", -12))})
    for k, sx in enumerate(sfx):
        try:
            f = os.path.join(APP, "sons", os.path.basename(sx["file"][4:])) if sx["file"].startswith("app:") else sfx_path(sx["file"])
        except KeyError:
            continue
        if not os.path.exists(f): continue
        inputs += ["-i", f]; dl = int(max(0, sx["t"]) * 1000)
        fc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={sx['gain_db']}dB,adelay={dl}|{dl}[sx{k}]")
        mix.append(f"[sx{k}]"); idx += 1
    fc.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first[amx]" if len(mix) > 1 else "[voice]anull[amx]")
    fc.append("[amx]aresample=48000[aout]")
    graph = ";".join(fc)
    open(os.path.join(work, "filter.txt"), "w", encoding="utf-8").write(graph.replace(";", ";\n"))  # pour le debogage
    out = os.path.join(vdir, f"V{n}.mp4")
    tmp = os.path.join(work, "mix.mkv")
    j["message"] = "Montage et habillage (sous-titres, musique, bruitages)…"
    gopt = ["-filter_complex", graph] if len(graph) < 24000 else ["-filter_complex_script", "filter.txt"]
    with_encoder(lambda: stream_progress(
        [CFG["ffmpeg"], "-y", "-v", "error"] + inputs + gopt + ["-map", "[vout]", "-map", "[aout]"] + venc("final", fps)
        + ["-c:a", "pcm_s16le", "-t", f"{total:.3f}", "mix.mkv"], j, 5, 93, total, cwd=work))
    if st.get("clean_joins", True):  # derniere verification sur la video finie : aucune image orpheline aux raccords
        try:
            j["fixed_frames"] = fix_orphans(tmp, fps, j)
        except Exception:
            pass
    # normalisation du son en deux passes : mesure, puis correction lineaire a -14 LUFS (standard des reseaux)
    j["message"] = "Réglage du volume final (-14 LUFS)…"; j["progress"] = 94
    target = "I=-14:TP=-1.5:LRA=11"
    r = run([CFG["ffmpeg"], "-hide_banner", "-i", tmp, "-vn", "-af", f"loudnorm={target}:print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
    if m:
        ms = json.loads(m.group(0))
        af = (f"loudnorm={target}:measured_I={ms['input_i']}:measured_TP={ms['input_tp']}:measured_LRA={ms['input_lra']}"
              f":measured_thresh={ms['input_thresh']}:offset={ms['target_offset']}:linear=true,aresample=48000,alimiter=limit=0.84:level=false")
    else:
        af = f"loudnorm={target},aresample=48000,alimiter=limit=0.84:level=false"
    r = run([CFG["ffmpeg"], "-y", "-v", "error", "-i", tmp, "-c:v", "copy", "-af", af,
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    if r.returncode: raise RuntimeError(r.stderr)
    shutil.rmtree(work, ignore_errors=True)
    p = load_project(p["id"])
    p.setdefault("versions", []).append({"n": n, "file": f"versions/V{n}.mp4", "created": time.time(),
                                         "settings": st, "size": os.path.getsize(out), "note": j.get("note", "")})
    p["status"] = "prêt"; save_project(p)
    j["version"] = n


def do_light(j):
    """Copie legere d'une version : 720p, ~10 fois plus petite, pour WhatsApp / mail."""
    p = load_project(j["project"]); d = pdir(p["id"]); n = int(j["n"])
    v = next(x for x in p["versions"] if x["n"] == n)
    src = os.path.join(d, v["file"]); out = os.path.join(d, "versions", f"V{n}_leger.mp4")
    j["message"] = f"Export léger de V{n}…"
    dur = float(probe(src)["duration"] or 1)
    with_encoder(lambda: stream_progress(
        [CFG["ffmpeg"], "-y", "-v", "error", "-i", src, "-vf", "scale=720:-2:flags=bicubic"] + venc("light", p.get("fps") or 25)
        + ["-maxrate", "2M", "-bufsize", "4M", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out], j, 2, 99, dur))
    p = load_project(p["id"])
    for x in p["versions"]:
        if x["n"] == n: x["light"] = f"versions/V{n}_leger.mp4"; x["light_size"] = os.path.getsize(out)
    save_project(p); j["version"] = n


# ---------------------------------------------------------------- videos longues : meilleurs moments -> un short par passage
MOMENTS_PROMPT = """Tu es monteur senior chez MG Prod, spécialiste des formats courts (TikTok, Reels, YouTube Shorts).
Voici la transcription horodatée d'une vidéo longue.{ctx} Chaque ligne : [début-fin en secondes] texte.

Trouve les meilleurs passages à transformer en shorts verticaux. Règles :
- Chaque passage dure entre {mn} et {mx} secondes et se comprend sans le reste de la vidéo.
- Il accroche dès les deux premières secondes : pas d'introduction molle, pas de « alors », « donc », « euh » au début.
- Il se termine sur une chute, une réponse, une punchline ou une réaction, jamais au milieu d'une idée.
- Il commence et se termine aux bornes des lignes de la transcription.
- Les passages ne se chevauchent pas. Donne entre 1 et {n} passages, du meilleur au moins bon. Mieux vaut peu de passages forts que beaucoup de moyens.

Pour chaque passage :
- "start", "end" : en secondes, repris des horodatages
- "title" : titre court pour l'équipe (5 mots maximum)
- "hook" : texte affiché à l'écran au début, 3 à 7 mots, qui donne envie de regarder sans dévoiler la chute. Mets le mot clé entre *étoiles*. Un « / » force un retour à la ligne.
- "highlight" : 2 à 6 mots prononcés dans le passage, recopiés exactement, à passer en jaune (mots forts, chiffres, noms)
- "zoom" : 0 à 3 mots prononcés dans le passage, recopiés exactement, sur lesquels faire un zoom (réaction, chute, moment fort)
- "why" : une phrase qui dit pourquoi ce passage marche
- "score" : potentiel viral de 1 à 10
- "caption" : légende du post, 1 à 3 phrases courtes, sans dévoiler la chute, qui finit par une question ou une invitation à commenter
- "hashtags" : 5 à 8 hashtags pertinents, sans le #
- "hooks" : 2 autres accroches possibles, même format que "hook"

Réponds uniquement avec un objet JSON de la forme {{"moments": [...]}}.

TRANSCRIPTION :
{transcript}"""


def sentences(words):
    out, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 0.8:
            out.append(cur); cur = []
        cur.append(w)
        if (re.search(r"[.!?…]$", w["w"]) and len(cur) >= 3) or len(cur) >= 40:
            out.append(cur); cur = []
    if cur: out.append(cur)
    return [{"s": c[0]["s"], "e": c[-1]["e"], "t": " ".join(x["w"] for x in c)} for c in out]


def moments_prompt(p):
    lines = "\n".join(f"[{x['s']:.1f}-{x['e']:.1f}] {x['t']}" for x in sentences(p.get("words", [])))
    ctx = CFG.get("episode_context") or ""
    taken = p.get("moments") or []
    if taken:
        lines += ("\n\nCes passages sont déjà pris, ne propose rien qui les recoupe : "
                  + ", ".join(f"[{m['start']:.1f}-{m['end']:.1f}]" for m in taken))
    return MOMENTS_PROMPT.format(ctx=(" Contexte : " + ctx) if ctx else "", mn=CFG["clip_min"], mx=CFG["clip_max"],
                                 n=CFG["clip_count"], transcript=lines)


def api_key():
    return (CFG.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def ask_claude(prompt):
    """Renvoie le texte de la reponse, ou None s'il n'y a pas de cle API (mode copier-coller)."""
    key = api_key()
    if not key: return None
    body = json.dumps({"model": CFG.get("claude_model") or "claude-sonnet-5-5", "max_tokens": 6000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API Claude, erreur {e.code} : {e.read().decode('utf-8', 'replace')[:600]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"API Claude injoignable : {e.reason}")
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


PUBLISH_PROMPT = """Tu es social media manager chez MG Prod. Voici la transcription d'un format court vertical (TikTok, Reels, YouTube Shorts).{ctx}

Écris en français, sur le ton de la vidéo, sans emoji en excès (deux maximum) et sans formule creuse :
- "caption" : la légende du post, 1 à 3 phrases courtes, qui donne envie de regarder jusqu'au bout sans dévoiler la chute, et finit par une question ou une invitation à commenter.
- "hashtags" : 5 à 8 hashtags pertinents, du plus large au plus précis, sans le #.
- "hooks" : 3 accroches différentes à afficher à l'écran au début, 3 à 7 mots chacune, le mot clé entre *étoiles*.

Réponds uniquement avec un objet JSON de la forme {{"caption": "...", "hashtags": ["..."], "hooks": ["...", "...", "..."]}}.

TRANSCRIPTION :
{transcript}"""


def publish_prompt(p):
    keep = kept_ranges(all_cuts(p), p["duration"])
    txt = " ".join(w["w"] for w in remap_words(p.get("words", []), keep))
    ctx = CFG.get("episode_context") or ""
    hook = p.get("settings", {}).get("hook", "").strip()
    if hook: ctx += f" Accroche actuelle : « {hook} »."
    return PUBLISH_PROMPT.format(ctx=(" Contexte : " + ctx.strip()) if ctx.strip() else "", transcript=txt)


def parse_publish(text):
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m: raise ValueError("La réponse ne contient pas de JSON. Recopie toute la réponse de Claude.")
    raw = json.loads(m.group(0))
    pub = {"caption": str(raw.get("caption") or "").strip(),
           "hashtags": [str(h).strip().lstrip("#").replace(" ", "") for h in raw.get("hashtags") or [] if str(h).strip()][:10],
           "hooks": [str(h).strip() for h in raw.get("hooks") or [] if str(h).strip()][:5]}
    if not (pub["caption"] or pub["hashtags"] or pub["hooks"]): raise ValueError("Réponse vide.")
    return pub


def do_publish(j):
    p = load_project(j["project"])
    j["progress"] = 20; j["message"] = "Claude rédige le texte de publication…"
    txt = ask_claude(publish_prompt(p))
    if txt is None: raise RuntimeError("Pas de clé API : utilise « Copier la demande » puis colle la réponse de Claude.")
    pub = parse_publish(txt)
    p = load_project(p["id"]); p["publish"] = pub; save_project(p)


def parse_moments(text, p):
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        raw = json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m: raise ValueError("La réponse ne contient pas de JSON. Recopie toute la réponse de Claude.")
        raw = json.loads(m.group(0))
    items = raw.get("moments", []) if isinstance(raw, dict) else raw
    words, dur = p.get("words", []), p["duration"]
    taken = [(m["start"], m["end"]) for m in p.get("moments", [])]
    out = []
    for it in items:
        try:
            a, b = float(it["start"]), float(it["end"])
        except (KeyError, TypeError, ValueError):
            continue
        sn = snap(a, b, words, dur)
        if not sn: continue
        a, b = sn
        if not (0.9 * CFG["clip_min"] <= b - a <= 1.1 * CFG["clip_max"]): continue  # hors de la duree voulue
        if any(min(b, y) - max(a, x) > 0.5 * (b - a) for x, y in taken): continue  # doublon d'un passage deja pris
        taken.append((a, b))
        pub = None
        if it.get("caption") or it.get("hashtags") or it.get("hooks"):
            try:
                pub = parse_publish(json.dumps({"caption": it.get("caption"), "hashtags": it.get("hashtags"), "hooks": it.get("hooks")}))
            except ValueError:
                pub = None
        out.append({"start": a, "end": b, "title": str(it.get("title") or "Passage")[:60], "hook": str(it.get("hook") or ""), "publish": pub,
                    "highlight": [str(x) for x in it.get("highlight") or []], "zoom": [str(x) for x in it.get("zoom") or []],
                    "why": str(it.get("why") or ""), "score": it.get("score")})
    if not out: raise ValueError("Aucun passage exploitable dans la réponse.")
    return out


def snap(a, b, words, dur):
    """Cale le debut et la fin sur les mots, avec un peu d'air."""
    inside = [w for w in words if a - 0.25 <= (w["s"] + w["e"]) / 2 <= b + 0.25]
    if not inside: return None
    s = max(0.0, inside[0]["s"] - 0.12); e = min(dur, inside[-1]["e"] + 0.35)
    return (round(s, 3), round(e, 3)) if e - s >= 3 else None


def mark_words(ws, phrases, key, limit):
    """Marque la premiere occurrence de chaque expression (hl = jaune, zoom = zoom punch)."""
    n = 0
    for ph in phrases:
        toks = [norm(x) for x in ph.split() if norm(x)]
        if not toks: continue
        for i in range(len(ws) - len(toks) + 1):
            if [norm(ws[i + k]["w"]) for k in range(len(toks))] == toks:
                if key == "zoom":
                    if ws[i].get("zoom"): break
                    ws[i]["zoom"] = True
                else:
                    for k in range(len(toks)): ws[i + k]["hl"] = True
                n += 1; break
        if n >= limit: break


def new_settings():
    st = dict(DEFAULT_SETTINGS)
    ml = list_music()
    if ml: st["music"] = ml[0]
    ps = load_presets()
    if ps.get("default") in ps.get("items", {}): st.update(ps["items"][ps["default"]])
    return st


def create_clips(parent_id, moments):
    with LOCK:
        par = load_project(parent_id)
        k0 = max([m["k"] for m in par.get("moments", [])] + [0])
        for k, m in enumerate(moments, k0 + 1):
            cid = f"{parent_id}_C{k:02d}"; d = os.path.join(PROJ, cid)
            shutil.rmtree(d, ignore_errors=True); os.makedirs(d)
            a, b = m["start"], m["end"]
            ws = [{"w": w["w"], "s": round(max(0, w["s"] - a), 3), "e": round(w["e"] - a, 3)}
                  for w in par["words"] if a <= (w["s"] + w["e"]) / 2 <= b]
            mark_words(ws, m.get("highlight", []), "hl", 6)
            mark_words(ws, m.get("zoom", []), "zoom", 3)
            st = new_settings(); st["hook"] = m.get("hook", ""); st["hook_start"] = 0.0
            p = {"id": cid, "name": f"{par['name']} · {k:02d} {m['title']}", "parent": parent_id, "created": time.time(),
                 "source": "source.mp4", "source_name": par.get("source_name"), "status": "en attente", "words": ws,
                 "scenes": [round(c - a, 3) for c in par.get("scenes", []) if a + 0.2 < c < b - 0.2],
                 "versions": [], "settings": st, "duration": round(b - a, 3), "fps": par.get("fps") or 25,
                 "width": W, "height": H, "has_audio": par.get("has_audio", True), "moment": m}
            if m.get("publish"):
                p["publish"] = dict(m["publish"], hooks=[h for h in [m.get("hook", "")] + m["publish"].get("hooks", []) if h][:4])
            json.dump(p, open(os.path.join(d, "project.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            par.setdefault("moments", []).append(dict(m, k=k, id=cid))
            new_job("clip", cid, parent=parent_id)
        par["status"] = "prêt"; par.pop("need_paste", None); save_project(par)


def reslice_clip(par, cid, a, b):
    """Recale un short sur [a, b] de la video longue en gardant le travail deja fait dessus :
    corrections de mots, jaune, zooms, bruitages, passages retires et habillage. Puis le refabrique."""
    c = load_project(cid); old = c["moment"]["start"]; old_end = old + c["duration"]
    mid = lambda w: (w["s"] + w["e"]) / 2
    mine = [dict(w, s=w["s"] + old, e=w["e"] + old) for w in c.get("words", [])]  # mots du short, en temps de la video longue
    words = [w for w in mine if a <= mid(w) <= b]
    words += [dict(w) for w in par.get("words", []) if a <= mid(w) <= b and not (old <= mid(w) <= old_end)]
    words.sort(key=lambda w: w["s"])
    c["words"] = [dict(w, s=round(max(0, w["s"] - a), 3), e=round(w["e"] - a, 3)) for w in words]
    c["cuts"] = [[round(max(0, x + old - a), 3), round(min(b - a, y + old - a), 3)] for x, y in c.get("cuts", [])
                 if y + old > a and x + old < b]
    c["scenes"] = [round(x - a, 3) for x in par.get("scenes", []) if a + 0.2 < x < b - 0.2]
    c["reframe"] = [dict(r, a=round(max(0, r["a"] + old - a), 3), b=round(min(b - a, r["b"] + old - a), 3))
                    for r in c.get("reframe", []) if r["b"] + old > a and r["a"] + old < b]
    c["splits"] = [round(x + old - a, 3) for x in c.get("splits", []) if a < x + old < b]
    c["duration"] = round(b - a, 3); c["moment"] = dict(c["moment"], start=a, end=b)
    c["status"] = "en attente"; c.pop("last_error", None); save_project(c)
    for m in par.get("moments", []):
        if m.get("id") == cid: m["start"], m["end"] = a, b
    save_project(par)
    new_job("clip", cid, parent=par["id"])


def do_moments(j):
    p = load_project(j["project"])
    p["status"] = "recherche des moments"; save_project(p)
    j["progress"] = 15; j["message"] = "Claude cherche les meilleurs moments…"
    txt = ask_claude(moments_prompt(p))
    p = load_project(p["id"])
    if txt is None:
        p["status"] = "à compléter"; p["need_paste"] = True; save_project(p)
        j["message"] = "Pas de clé API : copie la demande dans Claude"; return
    p["claude_reply"] = txt; save_project(p)
    create_clips(p["id"], parse_moments(txt, p))


YUNET = os.path.join(APP, "models", "face_detection_yunet_2023mar.onnx")


def face_detector(cv2, ww, hh):
    """Detecteur de visages : YuNet (reseau moderne, voit aussi les profils et les trois-quarts) si son modele est la,
    sinon l'ancien Haar d'OpenCV. Renvoie f(image_gris) -> [(x, y, w, h, zone_bouche, zone_yeux)]."""
    if os.path.exists(YUNET) and hasattr(cv2, "FaceDetectorYN"):
        try:
            det = cv2.FaceDetectorYN.create(YUNET, "", (ww, hh), 0.6, 0.3, 50)
            def yunet(gray):
                _, fs = det.detect(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
                out = []
                for f in (fs if fs is not None else []):
                    x, y, w, h = (int(v) for v in f[:4])
                    if w < 22 or h < 22 or f[14] < 0.7: continue  # petits visages au fond (public, ecrans) : ignores
                    ex = sorted([f[4], f[6]]); ey = (f[5] + f[7]) / 2          # yeux
                    mx = sorted([f[10], f[12]]); my = (f[11] + f[13]) / 2      # coins de la bouche
                    mw = max(mx[1] - mx[0], w * 0.3)
                    mouth = (slice(max(0, int(my - h * .12)), min(hh, int(my + h * .2))),
                             slice(max(0, int((mx[0] + mx[1]) / 2 - mw * .7)), min(ww, int((mx[0] + mx[1]) / 2 + mw * .7))))
                    eyes = (slice(max(0, int(ey - h * .1)), min(hh, int(ey + h * .1))),
                            slice(max(0, int(ex[0] - w * .1)), min(ww, int(ex[1] + w * .1))))
                    out.append((x, y, w, h, mouth, eyes))
                return out
            return yunet
        except Exception:
            pass
    casc = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    if casc.empty(): return None
    def haar(gray):
        return [(x, y, w, h, (slice(y + int(h * .68), min(hh, y + int(h * 1.02))), slice(x + int(w * .25), x + int(w * .75))),
                 (slice(y + int(h * .2), y + int(h * .45)), slice(x + int(w * .2), x + int(w * .8))))
                for (x, y, w, h) in casc.detectMultiScale(gray, 1.1, 6, minSize=(28, 28))]
    return haar


def speaker_plan(src, a, b, fps, iw, ih, scenes, words):
    """Plan de recadrage 9:16 qui suit la personne qui parle.
    Lit le passage en basse definition image par image (ffmpeg), repere les coupes a l'image pres,
    les visages, et l'activite de la bouche de chacun pendant que la transcription dit que quelqu'un parle.
    La meme lecture sert aussi a reperer les images fantomes (une seule lecture de la video).
    Renvoie ([(n_image_debut, x, y)], fantomes) avec x, y = centre vise (0 a 1), ou None sans OpenCV."""
    try:
        import cv2, numpy as np
    except Exception:
        return None
    hh = 360; ww = int(round(iw * hh / ih / 2)) * 2
    detect = face_detector(cv2, ww, hh)
    if detect is None: return None
    pr = subprocess.Popen([CFG["ffmpeg"], "-v", "error", "-ss", f"{a:.3f}", "-i", src, "-t", f"{b - a:.3f}", "-an",
                           "-vf", f"fps={fps},scale={ww}:{hh},format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    size = ww * hh; diffs, obs, prev, prev2, i, small = [], [], None, None, 0, []
    step = max(1, round(fps / 8))  # visages cherches ~8 fois par seconde
    while True:
        buf = pr.stdout.read(size)
        if len(buf) < size: break
        fr = np.frombuffer(buf, np.uint8).reshape(hh, ww)
        diffs.append(float(cv2.absdiff(fr, prev).mean()) if prev is not None else 0.0)
        small.append(cv2.resize(fr, (96, 54), interpolation=cv2.INTER_AREA).ravel())
        if i % step == 0:
            faces = []
            for (x, y, w, h, m, e) in detect(fr):
                act = 0.0
                if prev2 is not None and fr[m].size and fr[e].size:
                    dm = float(cv2.absdiff(fr[m], prev2[m]).mean()); de = float(cv2.absdiff(fr[e], prev2[e]).mean())
                    act = max(0.0, dm - de)  # la bouche bouge plus que les yeux : la tete seule ne compte pas
                faces.append(((x + w / 2) / ww, (y + h / 2) / hh, w / ww, act))
            obs.append((i, faces))
        prev2, prev = prev, fr; i += 1
    pr.wait()
    n = i
    if not n: return None
    ghosts = find_ghosts(small); small_keep = small
    # coupes : celles reperees par ffmpeg + les sauts d'image nets, a l'image pres
    cuts = {0}
    # une image de fondu a la coupe (fantome) etale le changement sur 2 images : la coupe tombe juste apres
    import numpy as _np
    gs = set(ghosts)
    for g in sorted(gs):
        if g + 1 in gs or g < 1 or g + 1 >= n: continue
        k = g
        while k - 1 in gs: k -= 1
        if k < 1: continue
        if float(_np.abs(small_keep[k - 1].astype(_np.float32) - small_keep[g + 1].astype(_np.float32)).mean()) > 4: cuts.add(g + 1)
    for c in scenes:
        if a < c < b:
            k = int(round((c - a) * fps))
            if 0 < k < n:  # recale sur le plus gros saut d'image autour
                cuts.add(max(range(max(1, k - 2), min(n, k + 3)), key=lambda j: diffs[j]))
    def jump(j, strict=True):
        """Vrai si l'image j change d'un coup par rapport à la précédente (coupe franche), même à décor identique :
        une seule image très différente, alors que ses voisines bougent peu."""
        if j < 1 or j >= n: return False
        around = sorted(diffs[k] for k in range(max(1, j - 8), min(n, j + 9)) if abs(k - j) > 1)
        base = around[len(around) // 2] if around else 0
        nb = max(diffs[j - 1] if j > 1 else 0, diffs[j + 1] if j + 1 < n else 0)
        if strict: return diffs[j] >= 4 and diffs[j] > 3 * base + 2.5 and diffs[j] > 2.2 * nb
        return diffs[j] >= 5 and diffs[j] > 2.5 * base + 2
    for j in range(1, n):
        if jump(j) and all(abs(j - c) > 3 for c in cuts): cuts.add(j)

    def snap_jump(lo, hi):
        """Image du plus gros saut entre lo et hi, si c'est une vraie coupe (sinon None)."""
        lo, hi = max(1, lo), min(n - 1, hi)
        if hi < lo: return None
        j = max(range(lo, hi + 1), key=lambda k: diffs[k])
        return j if jump(j, strict=False) else None
    cuts = sorted(cuts) + [n]
    speech = bytearray(n)  # 1 = quelqu'un parle sur cette image
    for w in words:
        if w["e"] < a or w["s"] > b: continue
        for f in range(max(0, int((w["s"] - a - 0.05) * fps)), min(n, int((w["e"] - a + 0.1) * fps) + 1)):
            speech[f] = 1

    def speaking(f):
        return speech[f] == 1
    cw = (ih * 9 / 16) / iw  # largeur du cadre 9:16, en part de l'image
    plan = []

    def follow(t, start, end):
        """Suit une personne de l'image start a end : cadre sur sa position du moment (mediane sur ~1 s),
        et recadre si elle s'eloigne du centre (tete pres du bord) pendant plus d'une demi-seconde."""
        pts = [q for q in t["pts"] if start <= q[0] < end] or t["pts"]
        med = lambda v: sorted(v)[len(v) // 2]
        win = lambda k: pts[k:k + 9]  # ~1 s d'observations (8 par seconde)
        cur_x, cur_y = med([q[1] for q in win(0)]), med([q[2] for q in win(0)])
        out = [(start, cur_x, cur_y)]; away = None
        for k, q in enumerate(pts):
            if abs(q[1] - cur_x) > cw * 0.2:
                if away is None: away = k
                if (q[0] - pts[away][0]) / fps >= 0.5 and (pts[away][0] - out[-1][0]) / fps >= 1.5:
                    f = pts[away][0]; j0 = snap_jump(f - 3, f + 3)  # sur un changement de camera s'il y en a un
                    f = j0 if j0 else f
                    cur_x, cur_y = med([z[1] for z in win(away)]), med([z[2] for z in win(away)])
                    out.append((max(out[-1][0] + 1, f), cur_x, cur_y)); away = None
            else:
                away = None
        return out
    last = (0.5, None)
    for c0, c1 in zip(cuts, cuts[1:]):
        o = [(f, fs) for f, fs in obs if c0 <= f < c1]
        tracks = []  # une piste = une personne, reperee par sa position horizontale
        for f, fs in o:
            for (x, y, w, act) in fs:
                t = min(tracks, key=lambda t: abs(t["x"] - x), default=None)
                if t and abs(t["x"] - x) < 0.08 and t["pts"][-1][0] != f:
                    t["pts"].append((f, x, y, w, act)); t["x"] = 0.8 * t["x"] + 0.2 * x
                else:
                    tracks.append({"x": x, "pts": [(f, x, y, w, act)]})
        nobs = max(1, len(o))
        tracks = [t for t in tracks if len(t["pts"]) >= max(2, 0.3 * nobs)] or sorted(tracks, key=lambda t: -len(t["pts"]))[:1]
        med = lambda v: sorted(v)[len(v) // 2]
        for t in tracks:
            t["mx"] = med([p[1] for p in t["pts"]]); t["my"] = med([p[2] for p in t["pts"]]); t["mw"] = med([p[3] for p in t["pts"]])
        if not tracks:
            plan.append((c0, last[0], last[1])); continue
        if len(tracks) >= 2:
            # une seule personne visible a la fois (plans serres qui alternent sans changer de decor) :
            # on decoupe le plan a chaque changement de visage, a l'image du plus gros saut entre les deux
            seq, multi = [], 0
            for f, fs in o:
                ids = {id(min(tracks, key=lambda t: abs(t["x"] - x))) for (x, y, w, act) in fs
                       if min(abs(t["x"] - x) for t in tracks) < 0.08}
                if len(ids) == 1: seq.append((f, ids.pop()))
                elif len(ids) > 1: multi += 1
            if seq and multi <= 0.1 * len(o):
                runs = []
                for f, tid in seq:
                    if runs and runs[-1][2] == tid: runs[-1][1] = f; runs[-1][3] += 1
                    else: runs.append([f, f, tid, 1])
                runs = [r for r in runs if r[3] >= 3] or runs  # un visage vu 1 ou 2 fois : bruit
                merged = []
                for r in runs:
                    if merged and merged[-1][2] == r[2]: merged[-1][1] = r[1]; merged[-1][3] += r[3]
                    else: merged.append(r)
                if len(merged) >= 2:
                    byid = {id(t): t for t in tracks}; bounds = [c0]
                    for r0, r1 in zip(merged, merged[1:]):
                        lo, hi = r0[1] + 1, r1[0]
                        bounds.append(max(range(lo, hi + 1), key=lambda k: diffs[k]) if hi >= lo else hi)
                    bounds.append(c1)
                    for r, s0, s1 in zip(merged, bounds, bounds[1:]):
                        plan.extend(follow(byid[r[2]], s0, s1))
                    last = plan[-1][1:]; continue
        if len(tracks) == 1:
            seg_ = follow(tracks[0], c0, c1); plan.extend(seg_); last = seg_[-1][1:]; continue
        lo = min(t["mx"] - t["mw"] / 2 for t in tracks); hi = max(t["mx"] + t["mw"] / 2 for t in tracks)
        if hi - lo <= cw * 0.8:  # tout le monde tient dans le cadre : on centre sur le groupe
            last = ((lo + hi) / 2, med([t["my"] for t in tracks])); plan.append((c0, *last)); continue
        # plusieurs personnes trop ecartees : on suit celle dont la bouche bouge quand on parle
        frames = [f for f, _ in o]
        act = {id(t): {p[0]: p[4] for p in t["pts"]} for t in tracks}
        win = max(1, int(8 * 0.8))  # lissage sur ~0,8 s
        def score(t, k):
            sl = frames[max(0, k - win // 2):k + win // 2 + 1]
            return sum(act[id(t)].get(f, 0.0) for f in sl) / len(sl)
        talk_idx = [k for k, f in enumerate(frames) if speaking(f)]
        first = talk_idx[:16] or list(range(len(frames)))
        cur = max(tracks, key=lambda t: (sum(score(t, k) for k in first), t["mw"]))
        segs = [(c0, cur)]; cand, since = None, None
        for k, f in enumerate(frames):
            if not speaking(f): cand = None; continue
            sc = sorted(((score(t, k), t) for t in tracks), key=lambda z: -z[0])
            best, second = sc[0], sc[1]
            if best[1] is cur or best[0] < 0.6 or best[0] < 1.4 * second[0]:
                cand = None; continue
            if cand is not best[1]: cand, since = best[1], f
            if (f - since) / fps >= 0.6 and (f - segs[-1][0]) / fps >= 1.0:  # parole confirmee, pas de ping-pong
                # bascule sur l'image exacte d'un changement de caméra s'il y en a un juste avant ; sinon un peu avant la phrase
                sw = snap_jump(since - int(0.8 * fps), since + int(0.2 * fps)) or since - int(0.25 * fps)
                sw = max(segs[-1][0] + 1, sw)
                segs.append((sw, best[1])); cur = best[1]; cand = None
        for (s_, t), nxt in zip(segs, [x[0] for x in segs[1:]] + [c1]):
            plan.extend(follow(t, s_, nxt))  # position de la personne pendant SA prise de parole
        last = plan[-1][1:]
    return plan, ghosts


def do_clip(j):
    """Decoupe le passage dans la video longue, le recadre en 9:16 sur les visages, puis genere la V1."""
    p = load_project(j["project"]); par = load_project(p["parent"]); d = pdir(p["id"])
    p["status"] = "découpage"; save_project(p)
    src = os.path.join(pdir(par["id"]), par["source"]); m = p["moment"]; a, b = m["start"], m["end"]
    iw, ih = int(par.get("width") or W), int(par.get("height") or H); fps = par.get("fps") or 25
    # debut cale sur une image exacte : la video de travail et le plan de cadrage ont les memes numeros d'image
    a = round(round(a * fps) / fps, 4); b = round(round(b * fps) / fps, 4)
    deghost = p.get("settings", {}).get("deghost", True)
    res = None
    if iw / max(ih, 1) > 0.6:  # horizontal : une seule lecture pour qui parle ET les images fantomes
        j["message"] = "Repérage de qui parle et des images fantômes…"
        res = speaker_plan(src, a, b, fps, iw, ih, par.get("scenes", []), par.get("words", []))
    if res:
        plan, ghosts = res
    else:
        plan = [(0, 0.5, None)]
        ghosts = scan_ghosts(src, fps, a, b - a) if deghost else []
    if not deghost: ghosts = []
    gset = set(ghosts)
    # Le short garde l'image LARGE du passage : le cadrage 9:16 se fait au rendu, clip par clip,
    # d'apres le plan automatique (qui parle) et les recadrages faits a la main dans l'apercu.
    frames = []
    for f0, fx, fy in plan:
        while f0 in gset: f0 += 1  # l'image fantome est remplacee par celle d'avant : on change de cadre juste apres
        if not frames or abs(frames[-1][1] - fx) > 0.01: frames.append([round(f0 / fps, 6), round(float(fx), 4), None if fy is None else round(float(fy), 3)])
    cap = 1440 if iw >= ih else 1920  # assez de definition pour recadrer, sans fichier enorme
    vf = f"scale=-2:{min(cap, ih - ih % 2)}:flags=lanczos" if ih > cap else "null"
    ys = [fy for _, _, fy in plan if fy is not None]
    p = load_project(p["id"]); p["frames"] = frames; p["wide"] = True
    if ys:  # hauteur des visages, utilisee par les zooms du rendu
        p["settings"]["face_y"] = round(min(0.45, max(0.18, sorted(ys)[len(ys) // 2])), 2)
    save_project(p)
    j["message"] = "Découpage du passage…"
    out = os.path.join(d, "source.mp4")
    with_encoder(lambda: stream_progress(
        [CFG["ffmpeg"], "-y", "-v", "error", "-ss", f"{a:.3f}", "-i", src, "-t", f"{b - a:.3f}",
         "-vf", drop_vf(ghosts, fps) + vf + f",setsar=1,fps={fps},setpts=PTS-STARTPTS"] + venc("work", fps) + ["-map", "0:v:0"]
        + (["-map", "0:a:0"] + audio_mix(src, 2) if par.get("has_audio", True) else ["-an"])
        + ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", out], j, 2, 30 if CFG.get("auto_render", True) else 99, b - a))
    try:  # les images fantomes viennent d'etre retirees : le rendu n'a pas besoin de relire la video pour les chercher
        json.dump({"key": f"{os.path.getsize(out)}:{int(os.path.getmtime(out))}", "frames": []}, open(os.path.join(d, "ghosts.json"), "w"))
    except OSError:
        pass
    p = load_project(p["id"]); p["status"] = "prêt"
    pr_ = probe(out); p["width"], p["height"] = pr_["width"], pr_["height"]
    p.pop("frames_raw", None)
    try:
        j["message"] = "Calage du cadrage sur les coupes…"; snap_to_cuts(p)
    except Exception:
        pass
    try:  # les mots viennent de la video longue : on les recale sur le son du short
        j["message"] = "Calage des sous-titres sur la voix…"; realign(p)
    except Exception:
        pass
    save_project(p)
    if CFG.get("auto_render", True):
        j["note"] = "V1 automatique"
        do_render(j)
    p = load_project(p["id"])
    if not p.get("publish") and api_key():  # texte de publication, s'il n'est pas venu avec le moment
        try:
            j["message"] = "Texte de publication…"; do_publish(j)
        except Exception:
            pass


# ---------------------------------------------------------------- forme d'onde (timeline)
def waveform(p):
    """Pics du son (0 a 100), mis en cache dans le projet. ~50 points/s pour un short, 10/s pour une video longue."""
    d = pdir(p["id"]); cache = os.path.join(d, "wave.json"); src = os.path.join(d, p["source"])
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(src):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except ValueError:
            pass
    rate = 50 if p["duration"] < 240 else 10; sr = 8000; step = sr // rate
    if not p.get("has_audio", True):
        return {"rate": rate, "peaks": []}
    r = subprocess.run([CFG["ffmpeg"], "-v", "error", "-i", src, "-map", "0:a:0"] + audio_mix(src, 1)
                       + ["-ar", str(sr), "-f", "s16le", "-"], capture_output=True, creationflags=NOWIN)
    data = array.array("h"); raw = r.stdout
    data.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder == "big": data.byteswap()
    peaks = [max(max(c), -min(c)) for c in (data[i:i + step] for i in range(0, len(data), step)) if len(c)]
    top = sorted(peaks)[int(len(peaks) * 0.995)] if peaks else 1
    w = {"rate": rate, "peaks": [min(100, round(100 * x / max(top, 1))) for x in peaks]}
    json.dump(w, open(cache, "w", encoding="utf-8"))
    return w


def resume_jobs():
    """Au demarrage : relance ce qui a ete interrompu (fenetre fermee pendant un traitement)."""
    for pid in sorted(os.listdir(PROJ)):
        try:
            p = load_project(pid)
        except Exception:
            continue
        shutil.rmtree(os.path.join(PROJ, pid, "_rendu"), ignore_errors=True)
        src_ok = os.path.exists(os.path.join(PROJ, pid, p.get("source") or "source.mp4"))
        if p.get("parent"):
            if not src_ok and p.get("status") != "erreur":
                new_job("clip", pid, parent=p["parent"])
        elif not src_ok:
            continue
        elif not p.get("words") and p.get("status") != "erreur":
            new_job("transcribe", pid)
        elif p.get("kind") == "episode" and p.get("status") == "recherche des moments":
            new_job("moments", pid)


# ---------------------------------------------------------------- presets
PRESETS = os.path.join(APP, "presets.json")
PROJECT_ONLY = {"hook"}  # le texte de l'accroche reste propre a chaque projet


def load_presets():
    try:
        return json.load(open(PRESETS, encoding="utf-8"))
    except Exception:
        return {"default": None, "items": {}}


def save_presets(ps):
    tmp = PRESETS + ".tmp"
    json.dump(ps, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1); os.replace(tmp, PRESETS)


def preset_settings(st):
    return {k: v for k, v in st.items() if k not in PROJECT_ONLY}


# ---------------------------------------------------------------- HTTP
class H_(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(b)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def send_file(self, path, download_name=None):
        if not os.path.isfile(path): return self.send_json({"error": "introuvable"}, 404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range"); start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)) and (m.group(1) or m.group(2)):
            if m.group(1): start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
            else: start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(206); self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype); self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download_name:
            q = urllib.parse.quote(download_name)
            self.send_header("Content-Disposition", f"attachment; filename=\"{slug(download_name.rsplit('.', 1)[0])}.mp4\"; filename*=UTF-8''{q}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start); left = end - start + 1
            try:
                while left > 0:
                    b = f.read(min(1 << 20, left))
                    if not b: break
                    self.wfile.write(b); left -= len(b)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass

    def remote(self):
        """Requete venue par le lien d'equipe (Cloudflare) ou d'un autre poste, et pas de ce PC."""
        return bool(self.headers.get("Cf-Connecting-Ip") or self.headers.get("Cf-Ray")) or self.client_address[0] not in ("127.0.0.1", "::1")

    def authed(self):
        if not self.remote(): return True
        if not CFG.get("team_password"): return False
        ck = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part: k, v = part.strip().split("=", 1); ck[k] = v
        return hmac.compare_digest(ck.get("mg_auth", ""), auth_token())

    def gate(self, parts):
        """True si la requete peut passer ; sinon la page de connexion (ou 401 pour l'API) est deja envoyee."""
        if self.authed() or parts[:1] == ["login"]: return True
        if parts[:1] == ["api"] or self.command != "GET":
            self.send_json({"error": "Connexion nécessaire : recharge la page et entre le mot de passe."}, 401)
        else:
            self.send_html(LOGIN_HTML.replace("%ERR%", ""))
        return False

    def send_html(self, html, code=200, extra=None):
        b = html.encode()
        self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def route(self):
        u = urllib.parse.urlparse(self.path); parts = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/") if x]
        return parts, urllib.parse.parse_qs(u.query)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        try:
            parts, q = self.route()
            if not self.gate(parts): return
            if parts[:1] == ["login"]: return self.send_html(LOGIN_HTML.replace("%ERR%", ""))
            if parts[:2] == ["api", "share"]:
                return self.send_json({"local": not self.remote(), "on": bool(TUNNEL["proc"] and TUNNEL["proc"].poll() is None),
                                       "url": TUNNEL["url"], "starting": TUNNEL["starting"], "error": TUNNEL["error"],
                                       "has_password": bool(CFG.get("team_password")), "fixed": bool(CFG.get("tunnel_token"))})
            if not parts: return self.send_file(os.path.join(APP, "web", "index.html"))
            if parts[0] == "web": return self.send_file(os.path.join(APP, "web", os.path.basename(parts[-1])))
            if parts[0] == "fonts": return self.send_file(os.path.join(CFG["fonts_dir"], os.path.basename(parts[-1])))
            if parts[0] == "sons": return self.send_file(os.path.join(APP, "sons", os.path.basename(parts[-1])))
            if parts[0] == "musique": return self.send_file(os.path.join(CFG["music_dir"], os.path.basename(parts[-1])))
            if parts[0] == "sfx": return self.send_file(sfx_path("/".join(parts[1:])))
            if parts[0] == "media":
                d = pdir(parts[1]); rel = "/".join(parts[2:])
                path = os.path.normpath(os.path.join(d, rel))
                if not inside(d, path): raise KeyError(rel)
                name = None
                if "dl" in q:
                    pr = load_project(parts[1]); name = f"{pr['name']}_{os.path.basename(rel)}"
                return self.send_file(path, name)
            if parts[:2] == ["api", "status"]:
                return self.send_json({"ffmpeg": bool(shutil.which(CFG["ffmpeg"]) or os.path.exists(CFG["ffmpeg"])),
                                       "whisper": bool(shutil.which(CFG["whisper"]) or os.path.exists(CFG["whisper"])),
                                       "model": os.path.exists(CFG["model"]), "sfx": len(list_sfx()),
                                       "music": list_music(), "defaults": DEFAULT_SETTINGS, "version": VERSION})
            if parts[:2] == ["api", "music"]: return self.send_json(list_music())
            if parts[:2] == ["api", "presets"]: return self.send_json(load_presets())
            if parts[:2] == ["api", "safezones"]:
                d = os.path.join(APP, "safezones")
                return self.send_json(sorted(f for f in os.listdir(d) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))) if os.path.isdir(d) else [])
            if parts[0] == "safezones": return self.send_file(os.path.join(APP, "safezones", os.path.basename(parts[-1])))
            if parts[:2] == ["api", "sfx"]:
                qq = norm(q.get("q", [""])[0]); items = list_sfx()
                if qq: items = [x for x in items if all(t in norm(x.replace("/", " ")) or t in x.lower() for t in q["q"][0].lower().split())]
                return self.send_json(items[:80])
            if parts[:2] == ["api", "projects"] and len(parts) == 2:
                out = []
                for pid in os.listdir(PROJ):
                    try:
                        p = load_project(pid)
                        out.append({k: p.get(k) for k in ("id", "name", "created", "status", "duration", "kind", "parent")} |
                                   {"versions": len(p.get("versions", []))})
                    except Exception:
                        pass
                return self.send_json(sorted(out, key=lambda x: -x["created"]))
            if parts[:2] == ["api", "projects"] and len(parts) == 4 and parts[3] == "wave":
                return self.send_json(waveform(load_project(parts[2])))
            if parts[:2] == ["api", "projects"] and len(parts) == 4 and parts[3] == "prompt":
                return self.send_json({"prompt": moments_prompt(load_project(parts[2]))})
            if parts[:2] == ["api", "projects"] and len(parts) == 4 and parts[3] == "publish_prompt":
                return self.send_json({"prompt": publish_prompt(load_project(parts[2])), "api": bool(api_key())})
            if parts[:2] == ["api", "projects"] and len(parts) == 3:
                p = load_project(parts[2])
                if p.get("kind") == "episode":  # la vue episode n'a pas besoin des milliers de mots
                    p["word_count"] = len(p.pop("words", []))
                    clips = []
                    for m in p.get("moments", []):
                        try:
                            c = load_project(m["id"])
                            clips.append({"id": c["id"], "status": c["status"], "versions": [v["n"] for v in c.get("versions", [])],
                                          "busy": any(j["project"] == c["id"] and j["status"] in ("en attente", "en cours") for j in JOBS.values())})
                        except Exception:
                            clips.append({"id": m["id"], "status": "supprimé", "versions": [], "busy": False})
                    p["clips"] = clips
                else:
                    if p.get("words") and p.get("aligned", 0) < ALIGN_V and p.get("status") == "prêt" and not busy(p["id"]):
                        try:  # projet d'avant le recalage : on le recale une fois
                            with LOCK:
                                p = realign(load_project(p["id"])); save_project(p)
                        except Exception:
                            pass
                    p["auto_cuts"] = silence_cuts(p)
                p["jobs"] = [j for j in JOBS.values() if j["project"] == p["id"] and j["status"] in ("en attente", "en cours")]
                p["has_api"] = bool(api_key())
                return self.send_json(p)
            if parts[:2] == ["api", "jobs"]: return self.send_json(JOBS[parts[2]])
            self.send_json({"error": "route inconnue"}, 404)
        except KeyError as e:
            self.send_json({"error": f"introuvable : {e}"}, 404)
        except Exception as e:  # noqa
            self.send_json({"error": str(e)}, 500)

    def do_HEAD(self):
        self.do_GET()

    # ------------------------------------------------------------ upload (corps brut, gros fichiers)
    def do_PUT(self):
        try:
            parts, q = self.route()
            if not self.gate(parts): return
            if parts[:2] != ["api", "upload"]: return self.send_json({"error": "route inconnue"}, 404)
            fname = os.path.basename(q.get("filename", ["video.mp4"])[0])
            name = q.get("name", [fname.rsplit(".", 1)[0]])[0].strip() or "Projet"
            pid = slug(name); base = pid; i = 2
            while os.path.exists(os.path.join(PROJ, pid)): pid = f"{base}_{i}"; i += 1
            d = os.path.join(PROJ, pid); os.makedirs(d)
            ext = os.path.splitext(fname)[1].lower() or ".mp4"
            uid = re.sub(r"[^a-z0-9]", "", q.get("uid", [""])[0])
            if uid:  # fichier envoye en morceaux (le lien Cloudflare refuse les envois de plus de 100 Mo d'un coup)
                part = os.path.join(UPLOADS, uid, "part")
                if not os.path.exists(part): shutil.rmtree(d, ignore_errors=True); return self.send_json({"error": "envoi introuvable"}, 400)
                os.replace(part, os.path.join(d, "source" + ext)); shutil.rmtree(os.path.join(UPLOADS, uid), ignore_errors=True)
            else:
                left = int(self.headers.get("Content-Length") or 0)
                with open(os.path.join(d, "source" + ext), "wb") as f:
                    while left > 0:
                        b = self.rfile.read(min(4 << 20, left))
                        if not b: break
                        f.write(b); left -= len(b)
                if left > 0:
                    shutil.rmtree(d, ignore_errors=True); return self.send_json({"error": "upload interrompu"}, 400)
            info = probe(os.path.join(d, "source" + ext))
            mode = q.get("mode", ["auto"])[0]
            long_ = mode == "episode" or (mode == "auto" and info["duration"] >= float(CFG.get("episode_min_sec", 150)))
            p = {"id": pid, "name": name, "created": time.time(), "source": "source" + ext, "source_name": fname,
                 "status": "en attente", "words": [], "scenes": [], "versions": [], "settings": new_settings(), **info}
            if long_:
                p["kind"] = "episode"; p["moments"] = []
                p["moments_mode"] = "manual" if q.get("moments", ["auto"])[0] == "manual" else "auto"  # choisi a l'import
            ps = load_presets()
            if ps.get("default") in ps.get("items", {}): p["preset"] = ps["default"]
            json.dump(p, open(os.path.join(d, "project.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            j = new_job("transcribe", pid)
            self.send_json({"project": p, "job": j})
        except Exception as e:  # noqa
            self.send_json({"error": str(e)}, 500)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        try:
            parts0 = self.route()[0]
            if parts0[:1] == ["login"]:
                n = int(self.headers.get("Content-Length") or 0)
                pw = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("password", [""])[0]
                if CFG.get("team_password") and hmac.compare_digest(pw.encode(), CFG["team_password"].encode()):
                    return self.send_html('<meta http-equiv="refresh" content="0;url=/">', extra=[
                        ("Set-Cookie", f"mg_auth={auth_token()}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax" + ("; Secure" if self.remote() else ""))])
                time.sleep(1.5)  # ralentit les essais au hasard
                return self.send_html(LOGIN_HTML.replace("%ERR%", '<p class="e">Mot de passe incorrect.</p>'), 401)
            if not self.gate(parts0): return
            if parts0[:2] == ["api", "upload_chunk"]:  # un morceau d'une video envoyee en plusieurs fois
                q = self.route()[1]; uid = re.sub(r"[^a-z0-9]", "", q.get("uid", [""])[0]); i = int(q.get("i", ["0"])[0])
                if not uid: return self.send_json({"error": "uid manquant"}, 400)
                d = os.path.join(UPLOADS, uid); os.makedirs(d, exist_ok=True)
                nf = os.path.join(d, "next"); nxt = int(open(nf).read()) if os.path.exists(nf) else 0
                left = int(self.headers.get("Content-Length") or 0)
                if i < nxt:  # deja recu (nouvel essai apres une coupure) : on ignore
                    while left > 0: left -= len(self.rfile.read(min(4 << 20, left)) or b"x" * left)
                    return self.send_json({"ok": True, "next": nxt})
                if i > nxt: return self.send_json({"error": f"morceau {i} recu avant le {nxt}", "next": nxt}, 409)
                with open(os.path.join(d, "part"), "ab") as f:
                    while left > 0:
                        b = self.rfile.read(min(4 << 20, left))
                        if not b: break
                        f.write(b); left -= len(b)
                if left > 0: return self.send_json({"error": "morceau incomplet", "next": nxt}, 400)
                open(nf, "w").write(str(nxt + 1)); return self.send_json({"ok": True, "next": nxt + 1})
            if parts0[:2] == ["api", "share"]:  # lien d'equipe : reglable seulement depuis ce PC
                if self.remote(): return self.send_json({"error": "Le lien d'équipe se règle sur le PC qui fait tourner l'outil."}, 403)
                body = self.body_json(); act = body.get("action")
                if body.get("password") is not None:
                    pw = str(body["password"]).strip()
                    if len(pw) < 6: return self.send_json({"error": "Mot de passe trop court (6 caractères minimum)."}, 400)
                    save_cfg(team_password=pw)
                if act == "on":
                    if not CFG.get("team_password"): return self.send_json({"error": "Choisis d'abord un mot de passe d'équipe."}, 400)
                    save_cfg(tunnel_on=True); start_tunnel()
                elif act == "off":
                    save_cfg(tunnel_on=False); stop_tunnel()
                return self.send_json({"ok": True})
            parts, q = self.route(); body = self.body_json()
            if parts[:2] == ["api", "projects"] and len(parts) == 4:
                pid, act = parts[2], parts[3]; p = load_project(pid)
                if act == "clear_error":
                    p.pop("last_error", None); save_project(p); return self.send_json({"ok": True})
                if act == "save":
                    if "settings" in body: p["settings"].update(body["settings"])
                    if "words" in body: p["words"] = body["words"]
                    if "cuts" in body: p["cuts"] = norm_cuts(body["cuts"], p["duration"], p.get("fps") or 25)
                    if "reframe" in body:  # recadrages faits a la main dans l'apercu, clip par clip
                        rf = []
                        for r in body["reframe"] or []:
                            a_, b_ = float(r["a"]), float(r["b"])
                            if b_ - a_ < 0.02: continue
                            rf.append({"a": round(a_, 3), "b": round(b_, 3),
                                       "x": None if r.get("x") is None else round(min(1, max(0, float(r["x"]))), 4),
                                       "y": None if r.get("y") is None else round(min(1, max(0, float(r["y"]))), 4)})
                        p["reframe"] = rf
                    if "reframe" in body: p.pop("reframe_raw", None)
                    if p.get("wide") and ("reframe" in body or "snap_cuts" in (body.get("settings") or {})):
                        snap_to_cuts(p)
                    if "splits" in body:  # coupures posees avec l'outil Couper (temps de la source)
                        p["splits"] = sorted({round(float(x), 3) for x in body["splits"] if 0 < float(x) < p["duration"]})
                    if "name" in body and body["name"].strip(): p["name"] = body["name"].strip()
                    save_project(p)
                    return self.send_json({"ok": True, "cuts": p.get("cuts", []), "auto_cuts": silence_cuts(p),
                                           "frames": p.get("frames"), "reframe": p.get("reframe")})
                if act == "realign":
                    if busy(pid): return self.send_json({"error": "Un traitement est en cours sur ce projet."}, 409)
                    p = realign(p); save_project(p)
                    return self.send_json({"ok": True, "words": p["words"], "auto_cuts": silence_cuts(p)})
                if act == "publish":
                    if not api_key(): return self.send_json({"error": "Pas de clé API : utilise « Copier la demande »."}, 400)
                    return self.send_json(new_job("publish", pid))
                if act == "paste_publish":
                    p["publish"] = parse_publish(body.get("text", "")); save_project(p)
                    return self.send_json({"ok": True, "publish": p["publish"]})
                if act == "save_publish":
                    pub = body.get("publish") or {}
                    p["publish"] = {"caption": str(pub.get("caption", ""))[:3000], "hashtags": [str(h) for h in pub.get("hashtags", [])][:15],
                                    "hooks": [str(h) for h in pub.get("hooks", [])][:6]}
                    save_project(p); return self.send_json({"ok": True})
                if act in ("transcribe", "find_moments") and busy(pid, children=False):
                    return self.send_json({"error": "Un traitement est déjà en cours sur ce projet."}, 409)
                if act == "transcribe" and p.get("parent"):  # short d'une video longue : on refait la decoupe
                    p["status"] = "en attente"; p.pop("last_error", None); save_project(p)
                    return self.send_json(new_job("clip", pid, parent=p["parent"]))
                if act == "transcribe": return self.send_json(new_job("transcribe", pid))
                if act == "find_moments": return self.send_json(new_job("moments", pid))
                if act == "paste_moments":  # reponse de Claude collee a la main (pas de cle API)
                    ms = parse_moments(body.get("text", ""), p); p["claude_reply"] = body.get("text", ""); save_project(p)
                    create_clips(pid, ms); return self.send_json({"ok": True, "count": len(ms)})
                if act == "set_range":  # change le debut / la fin d'un short deja cree
                    cid = os.path.basename(str(body.get("id", "")))
                    m = next((x for x in p.get("moments", []) if x.get("id") == cid), None)
                    if not m: return self.send_json({"error": "Short introuvable."}, 404)
                    if busy(cid): return self.send_json({"error": "Ce short est en cours de fabrication : attends qu'il se termine."}, 409)
                    sn = snap(float(body["start"]), float(body["end"]), p.get("words", []), p["duration"])
                    if not sn: return self.send_json({"error": "Passage trop court ou sans paroles."}, 400)
                    reslice_clip(p, cid, *sn)
                    return self.send_json({"ok": True, "start": sn[0], "end": sn[1]})
                if act == "add_moment" and not p.get("words"):
                    return self.send_json({"error": "La transcription n'est pas encore finie : attends qu'elle se termine."}, 409)
                if act == "add_moment":  # passage choisi a la main
                    sn = snap(float(body["start"]), float(body["end"]), p.get("words", []), p["duration"])
                    if not sn: return self.send_json({"error": "Passage trop court ou sans paroles."}, 400)
                    dup = next((m for m in p.get("moments", [])
                                if min(sn[1], m["end"]) - max(sn[0], m["start"]) > 0.9 * max(sn[1] - sn[0], m["end"] - m["start"])), None)
                    if dup: return self.send_json({"error": f"Ce passage existe déjà : short {dup['k']} « {dup['title']} »."}, 409)
                    create_clips(pid, [{"start": sn[0], "end": sn[1], "title": (body.get("title") or "Passage manuel")[:60],
                                        "hook": body.get("hook", ""), "highlight": [], "zoom": [], "why": "Choisi à la main", "score": None}])
                    return self.send_json({"ok": True})
                if act == "light": return self.send_json(new_job("light", pid, n=int(body["n"])))
                if act == "render":
                    if body.get("settings"): p["settings"].update(body["settings"]); save_project(p)
                    return self.send_json(new_job("render", pid, note=body.get("note", "")))
                if act == "delete_version":
                    n = int(body["n"]); keep = []
                    for v in p.get("versions", []):
                        if v["n"] == n:
                            for f in (v["file"], v.get("light")):
                                try:
                                    if f: os.remove(os.path.join(pdir(pid), f))
                                except OSError: pass
                        else: keep.append(v)
                    p["versions"] = keep; save_project(p); return self.send_json({"ok": True})
                if act == "note_version":
                    for v in p.get("versions", []):
                        if v["n"] == int(body["n"]): v["note"] = body.get("note", "")
                    save_project(p); return self.send_json({"ok": True})
                if act == "delete":
                    if busy(pid): return self.send_json({"error": "Un traitement est en cours sur ce projet : attends qu'il se termine."}, 409)
                    shutil.rmtree(pdir(pid), ignore_errors=True)
                    if p.get("parent"):  # retire le short de la liste de son episode
                        try:
                            par = load_project(p["parent"]); par["moments"] = [m for m in par.get("moments", []) if m.get("id") != pid]
                            save_project(par)
                        except Exception:
                            pass
                    return self.send_json({"ok": True})
            if parts[:2] == ["api", "presets"]:
                ps = load_presets(); act = parts[2] if len(parts) > 2 else ""
                name = (body.get("name") or "").strip()
                if act == "save" and name:
                    ps["items"][name] = preset_settings(body.get("settings") or {})
                    if body.get("default"): ps["default"] = name
                elif act == "delete":
                    ps["items"].pop(name, None)
                    if ps.get("default") == name: ps["default"] = None
                elif act == "default":
                    ps["default"] = name if name in ps["items"] else None
                save_presets(ps); return self.send_json(ps)
            if parts[:2] == ["api", "reveal"] and WIN:  # ouvre le dossier du projet dans l'Explorateur
                subprocess.Popen(["explorer", pdir(body["id"])]); return self.send_json({"ok": True})
            self.send_json({"error": "route inconnue"}, 404)
        except KeyError as e:
            self.send_json({"error": f"introuvable : {e}"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa
            self.send_json({"error": str(e)}, 500)


def main():
    resume_jobs()
    port = int(CFG.get("port", 8765))
    srv = ThreadingHTTPServer(("127.0.0.1", port), H_)
    print(f"MG Shorts Studio : http://localhost:{port}  (Ctrl+C pour arrêter)")
    if CFG.get("tunnel_on") and CFG.get("team_password"):  # lien d'equipe active : on le relance
        start_tunnel()
        def show():
            for _ in range(60):
                time.sleep(1)
                if TUNNEL["url"]: print(f"Lien pour l'équipe : {TUNNEL['url']}"); return
        threading.Thread(target=show, daemon=True).start()
    import atexit; atexit.register(stop_tunnel)
    if "--open" in sys.argv:
        import webbrowser; threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
