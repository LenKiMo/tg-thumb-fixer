#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg-thumb-fixer —— Telegram 频道 fixvx/fixupx 帖文「预览图缺失」自动补图

背景（2026-09 实测）：
  fixvx / fixupx 的 og:image 指向 pbs.twimg.com / pbs.fxtwitter.com 的 ?name=orig 原图。
  Telegram 服务端抓 og:image 时对体积有硬限（实测 ~1.4MB 以上一律失败：
  0.98MB / 1.33MB 可抓，1.52MB / 2.29MB / 2.33MB / 2.59MB 全部 400 failed to get HTTP URL content）。
  抓图失败 → 帖文只剩「站点名 + 标题 + 描述」的文字预览，没有缩略图，且 Telegram 不会重试。
  所以当原图 > ~1.4MB 时帖文就永久缺图。

修复思路：
  我们这边能正常下载 X 原图 → 直接以「上传字节」方式 editMessageMedia，
  把帖文改写成「图片 + 原文字作 caption」。实测：机器人只要有频道「编辑消息」权限，
  就能编辑**别人（含 inline bot）发的**频道帖文（非机器人自己发的也能改）。

检测（不需要任何账号）：
  拉 https://t.me/s/<频道> 的网页，比对 Telegram 侧**实际存下的**预览：
  有缩略图的帖文带 .link_preview_image，缺失的没有 —— 与 MTProto 侧 web_preview.photo 完全一致。

用法：
  python tg_thumb_fixer.py --config config.json --dry-run --backfill 300
  python tg_thumb_fixer.py --config config.json --backfill 300     # 真正修
  python tg_thumb_fixer.py --config config.json --watch             # 常驻轮询
  python tg_thumb_fixer.py --config config.json --fix-one <chat_id> <message_id> <text>
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
TG_API = "https://api.telegram.org/bot{token}/{method}"
SERVICE_HOSTS = ("fixvx.com", "vxtwitter.com", "fixupx.com", "fxtwitter.com",
                 "fxvx.com", "fxupx.com", "x.com", "twitter.com")
TWEET_RE = re.compile(r"(?:fixvx\.com|vxtwitter\.com|fixupx\.com|fxtwitter\.com|x\.com|twitter\.com)"
                      r"/([A-Za-z0-9_]{1,20})/status/(\d+)")
# 补图时把镜像域改写成官方域（fx 服务的宿主与官方域本就成对）
OFFICIAL_MAP = {
    "fixupx.com": "x.com",
    "fxupx.com": "x.com",
    "fixvx.com": "x.com",
    "fxvx.com": "x.com",
    "fxtwitter.com": "twitter.com",
    "vxtwitter.com": "twitter.com",
}
FX_HOST_RE = re.compile(r"(?i)\b(?:www\.)?(fixupx|fxupx|fixvx|fxvx|fxtwitter|vxtwitter)\.com\b")
MAX_CAPTION = 1024          # 图片消息 caption 上限
PAGE_TIMEOUT = 20           # t.me/s 单次抓取超时（秒）；t.me 偶发卡死，别拖垮一轮
MAX_UPLOAD = 45 * 1024 * 1024   # 机器人体积上限（50MB，留余量）
PHOTO_SIDE_LIMIT = 10000    # Telegram 图片单边上限


# ----------------------------------------------------------------------------- 配置

@dataclass
class Config:
    bot_token: str
    channels: list[str] = field(default_factory=lambda: ["your_channel"])
    state_file: str = "state.json"
    log_file: str = "fixer.log"
    interval: int = 300
    backfill: int = 300
    photo_mode: str = "photo"        # photo(=Telegram 图片，最长边压到 2560) | document(原图文件，无损)
    multi_media: str = "mosaic"      # mosaic(多图拼一张，原地改) | first(只取第一张) | album(删原帖+重发相册)
    incremental: bool = True               # 增量扫描：翻到上轮位置就停（--full 可强制深扫）
    video_mode: str = "poster"       # poster(只补视频封面图，帖文仍是"图片+配文") | video(直接附 mp4，≤50MB)
    dry_run: bool = False
    skip_text_over_caption: bool = True    # 正文 > 1024 字（caption 上限）时不改
    official_links: bool = True            # 补图时把 fx 镜像域改写成官方域
    official_domain: str | None = None     # 指定则所有 fx 域统一改成它（如 "x.com"）
    normalize_fixed_links: bool = True     # 每轮顺带把「已修过的帖」caption 里的 fx 域改成官方域（幂等）
    normalize_scope: str = "fixed"         # fixed(只改本工具修过的帖) | all(频道里所有带 fx 链接的图/视频帖)
    lock_file: str = "fixer.lock"           # cron 叠加保护用的锁文件（相对路径按 config 目录解析）
    config_dir: str = "."                   # 由 Config.load 填入 config 所在目录
    mosaic_host: str = "https://mosaic.fxtwitter.com/jpeg"
    alert_cmd: list[str] | None = None     # 出问题时执行的告警命令（常态静默，仅异常告警）

    @staticmethod
    def load(path: str, **overrides) -> "Config":
        data = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        # 同目录的 .env（KEY=VALUE）里也可以放 token，方便 cron 直接跑
        env_file = os.path.join(os.path.dirname(os.path.abspath(path)) if path else ".", ".env")
        if os.path.exists(env_file):
            for line in open(env_file, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or data.pop("bot_token", "")
        cfg = Config(bot_token=token, **{k: v for k, v in data.items()
                                        if k in Config.__dataclass_fields__ and not k.startswith("_")})
        cfg.config_dir = os.path.dirname(os.path.abspath(path)) if path else "."
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg


# ----------------------------------------------------------------------------- 工具

def http_get(url: str, timeout: int = 60, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def tg_call(cfg: Config, method: str, params: dict | None = None,
            files: dict[str, tuple[str, bytes]] | None = None, timeout: int = 180) -> dict:
    """调用 Bot API；files 非空时走 multipart，并把文件字段用 attach://<key> 引用。"""
    params = params or {}
    url = TG_API.format(token=cfg.bot_token, method=method)
    if not files:
        req = urllib.request.Request(url, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
    else:
        boundary = "----tgthumb" + uuid.uuid4().hex
        body = bytearray()
        for k, v in params.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for key, (fname, blob) in files.items():
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; "
                     f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
            body += blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode()          # 结束界只在所有文件之后写一次
        req = urllib.request.Request(url, data=bytes(body),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": f"{type(e).__name__}: {e}"}


def log(cfg: Config, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(cfg.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state(cfg: Config) -> dict:
    if os.path.exists(cfg.state_file):
        try:
            with open(cfg.state_file, encoding="utf-8") as f:
                st = json.load(f)
            for k in ("fixed", "last_id", "pending_delete"):
                st.setdefault(k, {})
            return st
        except Exception:
            pass
    return {"fixed": {}, "last_id": {}, "pending_delete": {}}


def save_state(cfg: Config, state: dict) -> None:
    tmp = cfg.state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, cfg.state_file)


# ----------------------------------------------------------------------------- 频道网页解析

@dataclass
class Post:
    channel: str
    message_id: int
    text: str
    has_photo: bool           # 帖文本身带图/视频（含我们补过的图）
    has_link_preview: bool
    preview_media: str | None  # image | video | embed | None —— 与 MTProto 的 web_preview.photo/document 对应
    preview_image: str | None
    preview_site: str | None
    preview_url: str | None

    @property
    def link(self) -> str | None:
        m = TWEET_RE.search(self.text) or (TWEET_RE.search(self.preview_url or ""))
        return m.group(0) if m else None

    @property
    def broken(self) -> bool:
        """帖文带推文链接、但 Telegram 侧没存下任何预览媒体（图/视频） → 需要补图。

        两种情况都算坏：
        - 有预览卡片但没有缩略图（article 型，最常见）；
        - 连预览卡片都没有（Telegram 抓取整体失败）。
        注意：视频预览（link_preview_video）在网页 widget 里没有 link_preview_image，
        但 Telegram 侧是有封面的（web_preview.document + photo），不算坏，必须区分。
        """
        return bool(self.link) and not self.has_photo and self.preview_media is None


BLOCK_RE = re.compile(r'<div class="tgme_widget_message[^"]*"(?P<body>.*?)(?=<div class="tgme_widget_message_wrap|</section>)', re.S)


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return htmllib.unescape(s).strip()


def parse_channel_page(html_text: str, channel: str) -> list[Post]:
    posts: list[Post] = []
    for m in BLOCK_RE.finditer(html_text):
        block = m.group(0)
        body = m.group("body")
        dp = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not dp:
            continue
        mid = int(dp.group(2))
        text_m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', body, re.S)
        text = strip_tags(text_m.group(1)) if text_m else ""
        has_photo = 'tgme_widget_message_photo_wrap' in body or 'tgme_widget_message_video_wrap' in body
        lp = re.search(r'<a class="tgme_widget_message_link_preview"[^>]*href="([^"]+)"', body)
        # 预览媒体类型：与 MTProto 的 web_preview.photo / document 一一对应
        # 网页 widget 的三种渲染：link_preview_image（大图）、link_preview_right_image（右侧小图）、
        # link_preview_video（视频预览，MTProto 侧 photo+document）
        if re.search(r'class="[^"]*link_preview_image', body) or re.search(r'class="[^"]*link_preview_right_image', body):
            preview_media = "image"
        elif 'link_preview_video' in body:
            preview_media = "video"
        elif 'link_preview_embed' in body:
            preview_media = "embed"
        else:
            preview_media = None
        prev_img = re.search(r'class="[^"]*link_preview_image[^"]*"[^>]*style="[^"]*url\(&quot;?\'?([^)&quot;\']+)', body) \
            or re.search(r'class="[^"]*link_preview_image[^"]*"[^>]*background-image:url\([\'"]?([^\'")]+)', body)
        if prev_img is None and preview_media == "image":
            u = re.search(r'background-image:\s*url\(([^)]+)\)', body)
            prev_img = re.search(r'[\'"]?([^\'")]+)', u.group(1)) if u else None
        site = re.search(r'class="link_preview_site_name[^"]*"[^>]*>(.*?)</', body)
        posts.append(Post(
            channel=channel, message_id=mid, text=text, has_photo=has_photo,
            has_link_preview=bool(lp), preview_media=preview_media,
            preview_image=(prev_img.group(1) if prev_img else None),
            preview_site=strip_tags(site.group(1)) if site else None,
            preview_url=htmllib.unescape(lp.group(1)) if lp else None,
        ))
    return posts


def fetch_channel_page(channel: str, before: int | None = None, tries: int = 2) -> str:
    """抓 t.me/s 页面。实测 t.me 偶发抽风（同一台机器上 0.2s 的请求会卡到 60s+），
    所以超时压到 20s 并重试一次；再不行就由调用方降级，别让一轮拖几分钟导致 cron 叠加。"""
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    last: Exception | None = None
    for i in range(tries):
        try:
            return http_get(url, timeout=PAGE_TIMEOUT).decode("utf-8", "ignore")
        except Exception as e:                       # noqa: BLE001
            last = e
            if i + 1 < tries:
                time.sleep(2)
    raise last if last else RuntimeError("fetch failed")


def scan_channel(channel: str, want: int = 300, page_size: int = 20,
                 stop_id: int | None = None) -> list[Post]:
    """倒序翻页取最近 want 条帖文。翻页中途抓不到就用手上已有的（宁少勿拖）。

    stop_id 非空时做**增量**：翻到「本页最小 id ≤ stop_id」说明已经碰到上轮见过的位置，
    收下这页就停（静默频道 1 页 ≈105KB，而不是每次 3 页 ≈310KB）。
    """
    out: list[Post] = []
    seen: set[int] = set()
    before: int | None = None
    while len(out) < want:
        try:
            page = parse_channel_page(fetch_channel_page(channel, before), channel)
        except Exception as e:                       # noqa: BLE001
            print(f"[warn] @{channel} 翻页失败（{type(e).__name__}: {str(e)[:80]}），"
                  f"本轮到 {len(out)} 帖为止", flush=True)
            break
        fresh = [p for p in page if p.message_id not in seen]
        if not fresh:
            break
        for p in fresh:
            seen.add(p.message_id)
        out.extend(fresh)
        before = min(p.message_id for p in fresh)
        if stop_id and before <= stop_id:            # 增量：已追上上轮位置
            break
        if before <= 1:
            break
    out.sort(key=lambda p: p.message_id)
    return out[-want:]


# ----------------------------------------------------------------------------- 原图解析

@dataclass
class Media:
    kind: str            # photo | video
    url: str
    name: str
    thumb: str | None = None


def media_key(url: str) -> str:
    """pbs.twimg.com/media/HR2rD3eagAAukv1.jpg?name=orig -> HR2rD3eagAAukv1"""
    return re.sub(r"\?.*$", "", url).split("/")[-1].rsplit(".", 1)[0]


def original_photos(tweet: dict, limit: int = 10) -> list[Media]:
    """推文里的原图（改 name=orig），最多 limit 张（Telegram 相册上限 10）。"""
    out: list[Media] = []
    for ph in ((tweet.get("media") or {}).get("photos") or [])[:limit]:
        url = ph.get("url", "")
        if "name=" in url:
            url = re.sub(r"name=\w+", "name=orig", url)
        if url:
            out.append(Media("photo", url, os.path.basename(url.split("?")[0]) or "photo.jpg"))
    return out


def resolve_tweet(link: str, cfg: Config | None = None) -> tuple[list[Media], dict]:
    """解析推文 → 候选附件列表（按优先级：多图拼图 → 单图 → 视频封面 → 视频）。"""
    m = TWEET_RE.search(link)
    if not m:
        return [], {}
    user, status_id = m.group(1), m.group(2)
    data = json.loads(http_get(f"https://api.fxtwitter.com/{user}/status/{status_id}", timeout=45)
                      .decode("utf-8", "ignore"))
    tweet = data.get("tweet") or {}
    media = tweet.get("media") or {}
    photos = original_photos(tweet)
    videos = []
    for v in media.get("videos") or []:
        best = max((x for x in (v.get("variants") or []) if ".mp4" in (x.get("url") or "")),
                   key=lambda x: x.get("bitrate", 0), default=None)
        if best:
            videos.append(Media("video", best["url"], f"{status_id}.mp4", thumb=v.get("thumbnail_url")))

    cands: list[Media] = []
    multi = (cfg.multi_media if cfg else "mosaic")
    if len(photos) > 1 and multi in ("mosaic", "album"):
        keys = "/".join(media_key(p.url) for p in photos)
        host = cfg.mosaic_host if cfg else "https://mosaic.fxtwitter.com/jpeg"
        cands.append(Media("photo", f"{host}/{status_id}/{keys}", f"{status_id}_mosaic.jpg"))
    cands.extend(photos)                       # 单图 / 拼图失败时的兜底（第一张）
    if videos:
        kind = (cfg.video_mode if cfg else "video")
        if kind == "poster" and videos[0].thumb:
            cands.append(Media("photo", videos[0].thumb, f"{status_id}_poster.jpg"))
        else:
            cands.append(videos[0])          # Bot API 上传视频时自行取首帧
    return cands, tweet


def tiny_stats(blob: bytes) -> str:
    return f"{len(blob)/1048576:.2f}MB"


# ----------------------------------------------------------------------------- 修复动作

def build_caption(text: str, tweet: dict, cfg: Config) -> str | None:
    cap = rewrite_links((text or "").strip(), cfg)
    if not cap:
        cap = tweet.get("url", "")
    if len(cap) > MAX_CAPTION:
        if cfg.skip_text_over_caption:
            return None
        cap = cap[:MAX_CAPTION - 1] + "…"
    return cap


def rewrite_links(text: str, cfg: Config | None = None) -> str:
    """把 fixvx/fixupx/fxtwitter/vxtwitter 等镜像域改写成官方域。

    默认按 fx 服务的成对关系：fixupx/fixvx → x.com，fxtwitter/vxtwitter → twitter.com；
    cfg.official_domain 指定时（如 "x.com"）统一改成它；cfg.official_links=False 则不改写。
    """
    if not text:
        return text
    if cfg is not None and not cfg.official_links:
        return text
    force = cfg.official_domain if cfg is not None else None

    def _sub(m: re.Match) -> str:
        host = (m.group(1) or "").lower() + ".com"
        return force or OFFICIAL_MAP.get(host, m.group(0))

    return FX_HOST_RE.sub(_sub, text)


def pick_and_download(cands: list[Media]) -> tuple[Media | None, bytes, list[str]]:
    """按优先级下载第一个能拿到的候选；返回 (选中, 字节, 失败原因列表)。"""
    errs: list[str] = []
    for c in cands:
        try:
            blob = http_get(c.url, timeout=150, headers={"Referer": "https://x.com/"})
        except Exception as e:
            errs.append(f"{c.kind}:{type(e).__name__}")
            continue
        if len(blob) > MAX_UPLOAD:
            errs.append(f"{c.kind}:too_big({tiny_stats(blob)})")
            continue
        if len(blob) < 2048:
            errs.append(f"{c.kind}:too_small")
            continue
        return c, blob, errs
    return None, b"", errs


def send_album_and_delete(cfg: Config, chat_id: str, message_id: int,
                          photos: list[Media], cap: str) -> dict:
    """删原帖 + 重发相册（多图原地编辑做不到，只能用这条路）。

    需要 bot 在频道里有「发布消息」+「删除消息」权限；任一缺失/体积超限 → 返回 ok=False，
    调用方会退回「拼图原地编辑」。删除失败会重试一次，仍失败则交由 pending_delete 兜底补删。
    """
    blobs: list[tuple[str, bytes]] = []
    total = 0
    errs: list[str] = []
    for i, ph in enumerate(photos):
        try:
            blob = http_get(ph.url, timeout=150, headers={"Referer": "https://x.com/"})
        except Exception as e:                       # noqa: BLE001
            errs.append(f"photo{i}:{type(e).__name__}")
            continue
        if len(blob) < 2048:
            errs.append(f"photo{i}:too_small")
            continue
        if total + len(blob) > MAX_UPLOAD:
            errs.append(f"photo{i}:total_too_big({(total + len(blob))/1048576:.1f}MB)")
            break
        blobs.append((f"{i}.jpg", blob))
        total += len(blob)
    if len(blobs) < 2:
        return {"ok": False, "reason": f"album_too_few({len(blobs)}/{len(photos)}) {errs}"}

    files: dict[str, tuple[str, bytes]] = {}
    media: list[dict] = []
    for i, (fname, blob) in enumerate(blobs):
        item = {"type": "photo", "media": f"attach://up{i}"}
        if i == 0:
            item["caption"] = cap                  # 相册只在第一条挂 caption
        media.append(item)
        files[f"up{i}"] = (fname, blob)
    r = tg_call(cfg, "sendMediaGroup",
                {"chat_id": str(chat_id), "media": json.dumps(media)}, files, timeout=420)
    if not r.get("ok"):
        return {"ok": False, "reason": f"send_album_failed: {r.get('description')}"}
    sent = [m["message_id"] for m in (r.get("result") or []) if isinstance(m, dict)]

    dele = tg_call(cfg, "deleteMessage", {"chat_id": str(chat_id), "message_id": str(message_id)})
    if not dele.get("ok"):
        time.sleep(2)
        dele = tg_call(cfg, "deleteMessage", {"chat_id": str(chat_id), "message_id": str(message_id)})
    return {"ok": True, "sent": sent, "bytes": total, "n": len(blobs),
            "deleted": bool(dele.get("ok")),
            "delete_error": None if dele.get("ok") else dele.get("description")}


def fix_one(cfg: Config, chat_id: str, message_id: int, text: str, link: str | None = None,
            dry_run: bool = False) -> dict:
    """把一条帖文改写为「图片 + 原文字」。返回结果字典。"""
    link = link or (TWEET_RE.search(text).group(0) if TWEET_RE.search(text) else None)
    if not link:
        return {"ok": False, "reason": "no_tweet_link"}
    try:
        cands, tweet = resolve_tweet(link, cfg)
    except Exception as e:
        return {"ok": False, "reason": f"resolve_failed: {e}", "link": link}
    if not cands:
        return {"ok": False, "reason": "no_media_or_deleted", "link": link}
    cap = build_caption(text, tweet, cfg)
    if cap is None:
        return {"ok": False, "reason": "text_too_long_for_caption", "link": link}

    # 多图 + album 模式：删原帖重发相册（原图逐张，不做拼图压缩）
    photos = original_photos(tweet)
    videos = (tweet.get("media") or {}).get("videos") or []
    album_err = None
    if cfg.multi_media == "album" and len(photos) >= 2 and not videos:
        if dry_run:
            return {"ok": True, "dry_run": True, "link": link, "kind": "album", "mode": "album",
                    "n_photos": len(photos), "caption": cap}
        ra = send_album_and_delete(cfg, chat_id, message_id, photos, cap)
        if ra.get("ok"):
            return {"ok": True, "link": link, "kind": "album", "mode": "album", "bytes": ra["bytes"],
                    "sent": ra["sent"], "deleted": ra["deleted"], "delete_error": ra.get("delete_error"),
                    "media_url": f"{ra['n']} 张原图相册"}
        album_err = ra.get("reason")

    pick, blob, errs = pick_and_download(cands)
    if pick is None:
        return {"ok": False, "reason": f"download_failed: {errs}", "link": link, "album_error": album_err}

    if dry_run:
        return {"ok": True, "dry_run": True, "link": link, "kind": pick.kind, "bytes": len(blob),
                "media_url": pick.url, "n_candidates": len(cands), "caption": cap, "album_error": album_err}

    if pick.kind == "photo":
        media_type = "document" if cfg.photo_mode == "document" else "photo"
    else:
        media_type = "video"
    fields = {
        "chat_id": str(chat_id), "message_id": str(message_id),
        "media": json.dumps({"type": media_type, "media": "attach://upload", "caption": cap}),
    }
    r = tg_call(cfg, "editMessageMedia", fields, {"upload": (pick.name, blob)})
    if not r.get("ok"):
        return {"ok": False, "reason": r.get("description"), "link": link}
    return {"ok": True, "link": link, "kind": media_type, "bytes": len(blob), "media_url": pick.url}


# ----------------------------------------------------------------------------- 主流程

def needs_link_normalize(text: str) -> bool:
    return bool(text) and bool(FX_HOST_RE.search(text))


def normalize_fixed_links(cfg: Config, channel: str, posts: list[Post], state: dict) -> int:
    """收尾：把帖文 caption 里的 fx 镜像域改成官方域。

    范围由 cfg.normalize_scope 决定：
    - "fixed"（默认）：只改**本工具修过**的帖（state 里登记过的）；
    - "all"：频道里所有带 fx 链接的图/视频帖都改（含别人手写的帖）。
    幂等：caption 已是官方域时跳过，不产生无谓的编辑。
    """
    fixed_ids = state["fixed"].get(channel, {})
    targets = ([p for sid in fixed_ids if (p := {q.message_id: q for q in posts}.get(int(sid)))]
               if cfg.normalize_scope == "fixed" else list(posts))
    done = 0
    for p in targets:
        if not p.has_photo or not needs_link_normalize(p.text):
            continue
        new_cap = rewrite_links(p.text, cfg)
        if cfg.dry_run:
            log(cfg, f"DRY-NORM @{channel}/{p.message_id}: {p.text[:45]!r} -> {new_cap[:45]!r}")
            done += 1
            continue
        r = tg_call(cfg, "editMessageCaption",
                    {"chat_id": f"@{channel}", "message_id": p.message_id, "caption": new_cap})
        if r.get("ok"):
            log(cfg, f"NORM @{channel}/{p.message_id}: caption 链接改为官方域 {new_cap[:60]!r}")
            if str(p.message_id) in fixed_ids:
                fixed_ids[str(p.message_id)]["link"] = new_cap
        else:
            log(cfg, f"SKIP-NORM @{channel}/{p.message_id} ({r.get('description')})")
        time.sleep(1.5)
        done += 1
    return done


def retry_pending_deletes(cfg: Config, channel: str, state: dict) -> int:
    """补删上一轮「相册已发出但原帖没删掉」的帖（避免频道里出现重复内容）。"""
    pend = state.setdefault("pending_delete", {}).setdefault(channel, {})
    done = 0
    for mid in list(pend):
        d = tg_call(cfg, "deleteMessage", {"chat_id": f"@{channel}", "message_id": mid})
        if d.get("ok"):
            del pend[mid]
            log(cfg, f"DEL 补删成功 @{channel}/{mid}")
            done += 1
        else:
            log(cfg, f"DEL 补删仍失败 @{channel}/{mid} ({d.get('description')})")
    return done


def run(cfg: Config, backfill: int, watch: bool) -> int:
    state = load_state(cfg)
    problems = 0
    me = tg_call(cfg, "getMe")
    if not me.get("ok"):
        log(cfg, f"!! 机器人不可用: {me}")
        return 2
    log(cfg, f"机器人 @{me['result'].get('username')} 启动 | 频道 {cfg.channels} | dry_run={cfg.dry_run}"
             f" | 多图={cfg.multi_media} | 增量={cfg.incremental}")

    while True:
        for channel in cfg.channels:
            want = backfill if not watch else min(backfill, 60)
            stop_id = state["last_id"].get(channel) if cfg.incremental else None
            try:
                posts = scan_channel(channel, want=want, stop_id=stop_id)
            except Exception as e:
                log(cfg, f"!! 读取频道页面失败 @{channel}: {e}")
                problems += 1
                continue
            fixed_known = state["fixed"].get(channel, {})
            candidates = [p for p in posts if p.broken and str(p.message_id) not in fixed_known]
            if not posts:
                log(cfg, f"!! @{channel} 读不到任何帖文（非公开频道 / 用户名有误 / t.me 被墙），已跳过")
            hit = "（增量命中上次位置）" if (cfg.incremental and stop_id and len(posts) < want) else ""
            log(cfg, f"@{channel}: 扫描 {len(posts)} 帖{hit}，待修 {len(candidates)} 条")
            if cfg.normalize_fixed_links:
                normalize_fixed_links(cfg, channel, posts, state)
            retry_pending_deletes(cfg, channel, state)
            for p in candidates:
                r = fix_one(cfg, f"@{channel}", p.message_id, p.text, dry_run=cfg.dry_run)
                if r.get("ok"):
                    tag = "DRY" if r.get("dry_run") else "FIXED"
                    extra = ""
                    if r.get("mode") == "album":
                        extra = (f" 相册×{len(r.get('sent') or [])}"
                                 + ("，原帖已删" if r.get("deleted") else "，⚠ 原帖未删"))
                    log(cfg, f"{tag} @{channel}/{p.message_id} <- {r.get('link')} "
                             f"[{r.get('kind', '')} {r.get('bytes', 0)/1048576:.2f}MB{extra}] {p.text[:50]!r}")
                    if not cfg.dry_run:
                        entry = {"link": r.get("link"), "ts": int(time.time())}
                        if r.get("mode") == "album":
                            entry.update({"mode": "album", "sent": r.get("sent")})
                        fixed_known[str(p.message_id)] = entry
                        if r.get("mode") == "album" and not r.get("deleted"):
                            state.setdefault("pending_delete", {}).setdefault(channel, {})[str(p.message_id)] = {
                                "ts": int(time.time()), "error": r.get("delete_error")}
                            problems += 1
                else:
                    detail = r.get("reason")
                    if r.get("album_error"):
                        detail = f"{detail} | album: {r['album_error']}"
                    log(cfg, f"SKIP @{channel}/{p.message_id} ({detail}) {p.text[:50]!r}")
                    if not cfg.dry_run and str(r.get("reason")).startswith(
                            ("no_media_or_deleted", "text_too_long_for_caption")):
                        fixed_known[str(p.message_id)] = {"skipped": r.get("reason"), "ts": int(time.time())}
                    problems += 1
                time.sleep(2.0)
            state["fixed"][channel] = fixed_known
            state["last_id"][channel] = max((p.message_id for p in posts), default=0)
        save_state(cfg, state)
        if problems and cfg.alert_cmd:
            try:
                import subprocess
                subprocess.run(cfg.alert_cmd + [f"tg-thumb-fixer: {problems} 个问题，详见 {cfg.log_file}"],
                               timeout=60, check=False)
            except Exception as e:
                log(cfg, f"!! 告警命令失败: {e}")
        if not watch:
            break
        time.sleep(cfg.interval)
    return 0


def lock_path(cfg: Config) -> str:
    p = cfg.lock_file
    if os.path.isabs(p):
        return p
    base = cfg.config_dir if cfg.config_dir else "."
    return os.path.join(base, p)


def acquire_lock(cfg: Config, stale_after: int = 900) -> bool:
    """cron 叠加保护：上一轮还没跑完（t.me 抽风时可能拖几分钟）就不重复开始。

    锁文件记 pid + 时间戳；超过 stale_after 秒视为陈旧锁（上轮被 kill/断电）直接接管。
    """
    p = lock_path(cfg)
    try:
        if os.path.exists(p):
            age = time.time() - os.path.getmtime(p)
            if age < stale_after:
                return False
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {int(time.time())}\n")
        return True
    except OSError:
        return True          # 写不了锁就别因为锁把任务卡死


def release_lock(cfg: Config) -> None:
    try:
        os.remove(lock_path(cfg))
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram 频道 fixvx/fixupx 预览图缺失自动补图")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", type=int, default=None, help="扫描最近 N 条帖文")
    ap.add_argument("--watch", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=int, default=None)
    ap.add_argument("--channels", nargs="*", default=None)
    ap.add_argument("--no-lock", action="store_true", help="忽略叠加保护（调试用）")
    ap.add_argument("--full", action="store_true", help="强制深度扫描（默认增量：翻到上轮位置即停）")
    ap.add_argument("--fix-one", nargs=3, metavar=("CHAT_ID", "MESSAGE_ID", "TEXT"),
                    help="手工修一条（自动化测试用）")
    a = ap.parse_args()
    cfg = Config.load(a.config, dry_run=a.dry_run or None, channels=a.channels,
                      interval=a.interval, backfill=a.backfill,
                      incremental=(False if a.full else None))
    if not cfg.bot_token:
        print("缺少 bot_token（config.json 或环境变量 TELEGRAM_BOT_TOKEN）", file=sys.stderr)
        return 1
    if a.fix_one:
        chat_id, mid, text = a.fix_one
        r = fix_one(cfg, chat_id, int(mid), text, dry_run=cfg.dry_run)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("ok") else 1
    if not a.no_lock and not acquire_lock(cfg):
        log(cfg, "上一轮仍在运行（或锁未释放），跳过本轮")
        return 0
    try:
        return run(cfg, cfg.backfill, a.watch)
    finally:
        if not a.no_lock:
            release_lock(cfg)


if __name__ == "__main__":
    sys.exit(main())
