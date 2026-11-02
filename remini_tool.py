import base64
import hashlib
import json
import os
import random
import sys
import threading
import time
import uuid
from queue import Queue

import requests

ORACLE = "https://api.remini.ai/v1/mobile/oracle"
API = "https://a.android.api.remini.ai/v1/mobile"
API2 = "https://a.android.api.remini.ai/v2/mobile"
UA = "okhttp/4.12.0"

DEVICES = [
    ("Samsung", "SM-G998B", "6.8", "33"),
    ("Google", "Pixel 7 Pro", "6.7", "33"),
    ("Xiaomi", "2201116SG", "6.67", "32"),
    ("OPPO", "CPH2211", "6.5", "31"),
    ("INFINIX", "Infinix X669", "6.6", "31"),
]

MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"}

MULTITOOL = {"type": "multi-tool", "pipelines": [{"bokeh": {"aperture_radius": "0.30", "apply_front_bokeh": "false", "group_picture": "true", "highlights": "0.25", "rescale_kernel_for_small_images": "true", "vivid": "0.75"}, "color_enhance": {"model": "prism-blend"}, "auto_balance": {"auto_contrast": {"contrast_intensity_coefficient": "1.5"}}, "background_enhance": {"model": "shiba-strong-tensorrt", "remove_color_shift": "true"}, "face_lifting": {"model": "pinko_bigger_dataset-style"}, "dodge_burn": {"intensity": 0.5, "model": "dodge_burn_residuals_big"}, "clip_extractor": {}, "face_enhance": {"model": "recommender_entire_dataset", "pre_blur": "0.6"}, "jpeg_quality": "80", "interpolation": "bicubic", "max_output_resolution": "3480", "enlarge_small_images_to": "2048", "smart_subject_selection": "true", "smart_subject_selection_factor": "1.5"}]}

DEFAULT_MAGIC_PRESET = "magic_video_wan_distill_lora_v2"


class Identity:
    def __init__(self, proxy=None):
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.proxy = proxy
        self.token = None
        self.android_id = uuid.uuid4().hex[:16]
        self.aaid = str(uuid.uuid4())
        self.backend_id = str(uuid.uuid4())
        man, model, dtype, osv = random.choice(DEVICES)
        self.base = {
            "bsp-id": "com.bigwinepot.nwdn.international.android",
            "build-number": "202526404",
            "build-version": "3.7.1477",
            "country": "US",
            "device-manufacturer": man,
            "device-model": model,
            "device-type": dtype,
            "language": "en",
            "locale": "en_US",
            "os-version": osv,
            "platform": "Android",
            "timezone": "America/New_York",
            "android-id": self.android_id,
            "aaid": self.aaid,
            "user-agent": UA,
        }

    def headers(self, extra=None):
        h = dict(self.base)
        if self.token:
            h["identity-token"] = self.token
        if extra:
            h.update(extra)
        return h

    def auth(self):
        h = self.headers({
            "first-install-timestamp": str(int(time.time())) + "E9",
            "backup-persistent-id": self.android_id + "_com.bigwinepot.nwdn.international",
            "non-backup-persistent-id": self.backend_id,
            "environment": "Production",
            "settings-response-version": "v2",
            "is-app-running-in-background": "false",
            "is-old-user": "true",
            "app-set-id": str(uuid.uuid4()),
        })
        r = self.s.get(ORACLE + "/setup", headers=h, timeout=40)
        r.raise_for_status()
        self.token = r.json()["settings"]["__identity__"]["token"]
        self.s.get(API + "/users/@me", headers=self.headers(), timeout=40)
        return self


class Farm:
    def __init__(self, proxies=None):
        self.proxies = proxies or []
        self.lock = threading.Lock()
        self.count = 0

    def next_proxy(self):
        with self.lock:
            self.count += 1
            if not self.proxies:
                return None
            return self.proxies[self.count % len(self.proxies)]

    def identity(self):
        return Identity(self.next_proxy()).auth()


def read_image(path):
    content = open(path, "rb").read()
    md5 = base64.b64encode(hashlib.md5(content).digest()).decode()
    mime = MIMES.get(os.path.splitext(path)[1].lower(), "image/jpeg")
    w = h = None
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.size
    except Exception:
        pass
    return content, md5, mime, w, h


def gcs_put(url, headers, content):
    uh = dict(headers)
    uh["Content-Length"] = str(len(content))
    uh["User-Agent"] = UA
    requests.put(url, headers=uh, data=content, timeout=180).raise_for_status()


def poll_v1(ident, task_id, max_wait=420):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        time.sleep(4)
        r = ident.s.get(f"{API}/tasks/{task_id}", headers=ident.headers(), timeout=40)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        d = r.json()
        st = d.get("status")
        if st == "completed":
            outs = (d.get("result") or {}).get("outputs") or []
            if outs:
                return outs[0]["url"]
            raise RuntimeError("completed without outputs")
        if st in ("failed", "error"):
            raise RuntimeError("task failed: " + json.dumps(d)[:200])
    raise TimeoutError("poll timeout")


def poll_v2(ident, task_id, max_wait=600):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        time.sleep(6)
        r = ident.s.get(f"{API2}/multi-avatar/tasks/{task_id}", headers=ident.headers(), timeout=40)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        d = r.json()
        st = d.get("status")
        if st == "completed":
            res = d.get("results") or []
            if res:
                return res[0]["uri"]
            raise RuntimeError("completed without results")
        if st in ("failed", "error"):
            raise RuntimeError("task failed: " + json.dumps(d)[:200])
    raise TimeoutError("poll timeout")


def download(url, out_path):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=300)
    r.raise_for_status()
    open(out_path, "wb").write(r.content)
    return len(r.content)


def enhance(farm, image, out, multitool=False):
    ident = farm.identity()
    content, md5, mime, w, h = read_image(image)
    feature = MULTITOOL if multitool else {"type": "enhance", "models": []}
    body = {"image_content_type": mime, "image_md5": md5, "image_size": len(content), "feature": feature}
    if multitool:
        body["image_resolution_width"] = w or 640
        body["image_resolution_height"] = h or 640
    else:
        body["metadata"] = {"size": len(content)}
        body["options"] = {"high_quality_output": False, "save_input": True}
    r = ident.s.post(API + "/tasks", headers=ident.headers({"content-type": "application/json; charset=UTF-8"}), json=body, timeout=40)
    r.raise_for_status()
    d = r.json()
    gcs_put(d["upload_url"], d["upload_headers"], content)
    ident.s.post(f"{API}/tasks/{d['task_id']}/process", headers=ident.headers({"content-length": "0"}), timeout=40).raise_for_status()
    url = poll_v1(ident, d["task_id"])
    size = download(url, out)
    return out, size


def magic_video(farm, face_image, out, preset=DEFAULT_MAGIC_PRESET):
    ident = farm.identity()
    content, md5, mime, w, h = read_image(face_image)
    h2 = ident.headers({"content-type": "application/json; charset=UTF-8"})
    r = ident.s.post(API2 + "/multi-avatar/multi-images/models", headers=h2, json={"subjects": [{"images": [{"content_type": mime, "md5": md5}]}]}, timeout=40)
    r.raise_for_status()
    subj = r.json()["subjects"][0]
    gcs_put(subj["images"][0]["upload_uri"], subj["images"][0]["upload_headers"], content)
    body = {"subjects": [{"photo_model_id": subj["photo_model_id"]}], "preset_ids": [preset], "n_output_images": 1,
            "ai_generation_config": {"free": {"generation": {"config": {"job_type": "multiavatar"}, "handler_type": "multiavatar"}},
                                     "premium": {"generation": {"config": {"job_type": "multiavatar"}, "handler_type": "multiavatar"}}},
            "input_people": 1}
    r = ident.s.post(API2 + "/multi-avatar/tasks", headers=h2, json=body, timeout=40)
    r.raise_for_status()
    tid = r.json()["photo_task_id"]
    url = poll_v2(ident, tid)
    size = download(url, out)
    return out, size


def stylize(farm, image, out, style="toon"):
    ident = farm.identity()
    content, md5, mime, w, h = read_image(image)
    body = {"image_content_type": mime, "image_md5": md5, "image_size": len(content), "feature": {"type": "stylization-v2", "id": style}}
    r = ident.s.post(API + "/tasks", headers=ident.headers({"content-type": "application/json; charset=UTF-8"}), json=body, timeout=40)
    r.raise_for_status()
    d = r.json()
    gcs_put(d["upload_url"], d["upload_headers"], content)
    ident.s.post(f"{API}/tasks/{d['task_id']}/process", headers=ident.headers({"content-length": "0"}), timeout=40).raise_for_status()
    url = poll_v1(ident, d["task_id"])
    size = download(url, out)
    return out, size


def ai_sticker(farm, image, out, preset="ai_sticker_geemoji"):
    ident = farm.identity()
    content, md5, mime, w, h = read_image(image)
    H = ident.headers({"content-type": "application/json; charset=UTF-8"})
    r = ident.s.post(API2 + "/ai-sticker/input-photos", headers=H, json={"input_photos": [{"content_type": mime, "md5": md5}]}, timeout=40)
    r.raise_for_status()
    d = r.json()
    up = d["input_photos"][0]
    gcs_put(up["upload_uri"], up["upload_headers"], content)
    r = ident.s.post(API2 + "/ai-sticker/tasks", headers=H, json={"input_photo_group_id": d["input_photo_group_id"], "preset_ids": [preset]}, timeout=40)
    r.raise_for_status()
    tid = r.json()["task_id"]
    t0 = time.time()
    while time.time() - t0 < 1800:
        time.sleep(10)
        p = ident.s.get(f"{API2}/ai-sticker/tasks/{tid}", headers=ident.headers(), timeout=40).json()
        st = p.get("status")
        if st == "completed":
            res = p.get("results") or []
            if not res:
                raise RuntimeError("completed without results")
            size = download(res[0].get("uri") or res[0].get("url"), out)
            return out, size
        if st in ("failed", "error"):
            raise RuntimeError("task failed: " + json.dumps(p)[:200])
    raise TimeoutError("sticker poll timeout")


def avatar(farm, image_paths, out, preset):
    ident = farm.identity()
    H = ident.headers({"content-type": "application/json; charset=UTF-8"})
    imgs = []
    datas = []
    for p in image_paths:
        content, md5, mime, w, h = read_image(p)
        imgs.append({"content_type": mime, "md5": md5})
        datas.append(content)
    r = ident.s.post(API2 + "/photo-tasks/models", headers=H, json={"images": imgs}, timeout=40)
    r.raise_for_status()
    d = r.json()
    mid = d["photo_model_id"]
    for up, content in zip(d["images"], datas):
        gcs_put(up["upload_uri"], up["upload_headers"], content)
    t0 = time.time()
    while time.time() - t0 < 3600:
        time.sleep(20)
        m = ident.s.get(f"{API2}/photo-tasks/models/{mid}", headers=ident.headers(), timeout=40).json()
        st = m.get("status")
        if st in ("ready", "completed", "trained"):
            break
        if st in ("failed", "error"):
            raise RuntimeError("model failed: " + json.dumps(m)[:200])
    else:
        raise TimeoutError("model training timeout")
    gen = {"generation": {"config": {"job_type": "multiavatar"}, "handler_type": "multiavatar"}}
    r = ident.s.post(API2 + "/photo-tasks/pack-photo", headers=H, json={"ai_generation_config": {"free": gen, "premium": gen}, "presets": [{"preset_id": preset, "photo_model_id": mid}]}, timeout=40)
    r.raise_for_status()
    tid = r.json().get("photo_task_id") or r.json().get("task_id")
    t0 = time.time()
    while time.time() - t0 < 1800:
        time.sleep(10)
        g = ident.s.get(f"{API2}/photo-tasks/preset-photo/{tid}", headers=ident.headers(), timeout=40).json()
        st = g.get("status")
        if st == "completed":
            res = g.get("results") or []
            if not res:
                raise RuntimeError("completed without results")
            size = download(res[0].get("uri") or res[0].get("url"), out)
            return out, size
        if st in ("failed", "error"):
            raise RuntimeError("task failed: " + json.dumps(g)[:200])
    raise TimeoutError("avatar poll timeout")


MODES = {"enhance": False, "multitool": True}

BANNER = "=" * 64 + "\n  Remini Full-Features Tool — All Modes Unlocked\n  enhance | multitool | stylize | magicvideo | sticker | avatar\n  By @Q_b_h — All Rights Reserved\n" + "=" * 64


def banner():
    print(BANNER, flush=True)


def worker(farm, mode, preset, q, outdir, results, wlock):
    while True:
        try:
            src = q.get_nowait()
        except Exception:
            return
        name = os.path.splitext(os.path.basename(src))[0]
        try:
            if mode == "magicvideo":
                out = os.path.join(outdir, name + "_magic.mp4")
                _, size = magic_video(farm, src, out, preset)
            elif mode == "stylize":
                out = os.path.join(outdir, name + "_" + preset + ".jpg")
                _, size = stylize(farm, src, out, preset)
            elif mode == "sticker":
                out = os.path.join(outdir, name + "_sticker.png")
                _, size = ai_sticker(farm, src, out, preset)
            else:
                out = os.path.join(outdir, name + "_hd.jpg")
                _, size = enhance(farm, src, out, multitool=MODES[mode])
            with wlock:
                results.append(("OK", src, out, size))
                print(f"[OK] {os.path.basename(src)} -> {os.path.basename(out)} ({size} bytes)", flush=True)
        except Exception as e:
            with wlock:
                results.append(("FAIL", src, str(e)[:160], 0))
                print(f"[FAIL] {os.path.basename(src)}: {str(e)[:160]}", flush=True)


def load_proxies(path):
    if not path:
        return []
    lines = [l.strip() for l in open(path) if l.strip()]
    out = []
    for l in lines:
        if l.startswith("http"):
            out.append(l)
        else:
            parts = l.split(":")
            if len(parts) == 4:
                out.append(f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}")
            else:
                out.append("http://" + l)
    return out


def main():
    banner()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("remini_tool.py <enhance|multitool|magicvideo|stylize|sticker|avatar> <image|list.txt|dir> [--out DIR] [--proxy proxies.txt] [--threads N] [--preset ID]")
        print("  avatar: src = مجلد صور وجوه للتدريب، --preset = preset أفاتار مثل female_caucasian_00174")
        return
    mode = args[0]
    src = args[1]
    outdir = "remini_out"
    threads = 4
    preset = DEFAULT_MAGIC_PRESET
    if mode == "stylize":
        preset = "toon"
    elif mode == "sticker":
        preset = "ai_sticker_geemoji"
    elif mode == "avatar":
        preset = "female_caucasian_00174"
    proxies = []
    i = 2
    while i < len(args):
        if args[i] == "--out":
            outdir = args[i + 1]; i += 2
        elif args[i] == "--proxy":
            proxies = load_proxies(args[i + 1]); i += 2
        elif args[i] == "--threads":
            threads = int(args[i + 1]); i += 2
        elif args[i] == "--preset":
            preset = args[i + 1]; i += 2
        else:
            i += 1
    os.makedirs(outdir, exist_ok=True)
    if os.path.isdir(src):
        files = [os.path.join(src, f) for f in sorted(os.listdir(src)) if os.path.splitext(f)[1].lower() in MIMES]
    elif os.path.isfile(src) and os.path.splitext(src)[1].lower() in MIMES:
        files = [src]
    else:
        files = [l.strip() for l in open(src) if l.strip()]
    print(f"[*] mode={mode} files={len(files)} threads={threads} proxies={len(proxies)} out={outdir}", flush=True)
    farm = Farm(proxies)
    if mode == "avatar":
        photos = [os.path.join(src, f) for f in sorted(os.listdir(src)) if os.path.splitext(f)[1].lower() in MIMES]
        print(f"[*] avatar: {len(photos)} صور تدريب، preset={preset}", flush=True)
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, "avatar_" + preset + ".jpg")
        try:
            _, size = avatar(farm, photos, out, preset)
            print(f"[OK] avatar -> {out} ({size} bytes)", flush=True)
        except Exception as e:
            print(f"[FAIL] avatar: {str(e)[:200]}", flush=True)
        return
    q = Queue()
    for f in files:
        q.put(f)
    results = []
    wlock = threading.Lock()
    ts = [threading.Thread(target=worker, args=(farm, mode, preset, q, outdir, results, wlock), daemon=True) for _ in range(min(threads, len(files)) or 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ok = [r for r in results if r[0] == "OK"]
    fail = [r for r in results if r[0] == "FAIL"]
    print(f"[*] DONE ok={len(ok)} fail={len(fail)}", flush=True)
    with open(os.path.join(outdir, "report.txt"), "w") as f:
        for r in results:
            f.write("\t".join(str(x) for x in r) + "\n")


if __name__ == "__main__":
    main()
