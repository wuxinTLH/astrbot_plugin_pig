import os
import re
import time
import json
import asyncio
import random
import shutil
import socket
import urllib.parse
import aiohttp

from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger

# ── 图源常量 ───────────────────────────────────────────────────────────────
# pighub.top API（sort=2 随机排序，curl 可正常访问，无 WAF 拦截）
PIGHUB_API  = "https://pighub.top/api/images?sort=2"
PIGHUB_BASE = "https://pighub.top"

# jsDelivr / GitHub raw 作为图片下载备用（API 不可用时也可用于列表）
JSDLVR_CDN  = "https://cdn.jsdelivr.net/gh/BadFish-HSrui/PigHub-DB@master/data/"
JSDLVR_API  = "https://data.jsdelivr.com/v1/packages/gh/BadFish-HSrui/PigHub-DB@master/flat"
GHRAW_CDN   = "https://raw.githubusercontent.com/BadFish-HSrui/PigHub-DB/master/data/"

# 诊断探测端点
PROBE_TARGETS = [
    ("pighub API",   PIGHUB_API),
    ("pighub 图片",  "https://pighub.top/"),
    ("jsDelivr CDN", "https://cdn.jsdelivr.net/gh/BadFish-HSrui/PigHub-DB@master/data/"),
    ("jsDelivr API", JSDLVR_API),
    ("GitHub raw",   "https://raw.githubusercontent.com/BadFish-HSrui/PigHub-DB/master/data/"),
]

_VALID_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")

# 请求图片时的 Accept 头：pighub.top 的 WAF 校验 Accept 字段，
# 只接受 text/* / application/json 等，不能带 image/* ——
# 用 */* 兜底，既能拿到图片二进制，又不触发"只支持 text"的拦截规则。
_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
}

_API_HEADERS = {
    "User-Agent": "astrbot_plugin_pig/0.1.5-t",
    "Accept": "application/json, text/plain, */*",
}


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.1.5-t")
class PigRandomImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)

        try:
            self.cooldown_period = float(config.get("cooldown_period", 5))
        except Exception:
            self.cooldown_period = 5.0

        self.load_to_local = bool(config.get("load_to_local", False))

        try:
            self.max_retries = int(config.get("max_retries", 3))
        except Exception:
            self.max_retries = 3

        try:
            self.request_timeout = float(config.get("request_timeout", 15))
        except Exception:
            self.request_timeout = 15.0

        try:
            self.overall_budget = float(config.get("overall_timeout", 20))
        except Exception:
            self.overall_budget = 20.0

        # 列表缓存有效期（秒），默认 1 小时
        # pighub API sort=2 是随机排序，每次调用结果不同，
        # 设短一点让列表保持新鲜；图片下载走实时 URL 不走缓存
        try:
            self.list_cache_ttl = float(config.get("list_cache_ttl", 3600))
        except Exception:
            self.list_cache_ttl = 3600.0

        try:
            self.is_match_all_msg = bool(config.get("is_match_all_msg", False))
        except Exception:
            self.is_match_all_msg = False

        try:
            self.is_exact_match = bool(config.get("is_exact_match", True))
        except Exception:
            self.is_exact_match = True

        try:
            self.match_keywords = list(config.get("match_keywords", []))
        except Exception:
            self.match_keywords = ["猪", "祝", "🐷", "🐖", "🐽", "㊗", "㊗️"]

        try:
            self.exclude_prefixes = tuple(config.get("exclude_prefixes", ()))
        except Exception:
            self.exclude_prefixes = ("/", "!", "！", "#", "ww")

        self.last_called_times: Dict[str, float] = {}

        # pig_images: List[{"title", "full_url", "filename"}]
        # full_url 优先用 API 返回的原始 URL，下载失败时降级到 jsDelivr/GitHub raw
        self.pig_images: List[Dict[str, Any]] = []
        self._list_fetched_at: float = 0.0

        base_dir = os.path.dirname(__file__)
        self.local_img_dir  = os.path.join(base_dir, "imgs", "pig")
        self.tmp_dl_dir     = os.path.join(base_dir, "imgs", "tmp_dl")
        self.filelist_cache = os.path.join(base_dir, "filelist.json")

        for d in (self.tmp_dl_dir,):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                logger.error(f"创建目录失败 {d}：{e}")

        self._download_semaphore = asyncio.Semaphore(3)
        self._list_lock          = asyncio.Lock()

        self._create_local_dir()
        self._load_cache()

    # ── 工具 ──────────────────────────────────────────────────────────────

    def _create_local_dir(self):
        if not self.load_to_local:
            return
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
        except OSError as e:
            self.load_to_local = False
            logger.error(f"创建本地图片目录失败：{e}")

    def _sanitize_filename(self, name: str, default: str = "image") -> str:
        if not name:
            return default
        name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
        allowed = set("-_.() ")
        cleaned = []
        for ch in name:
            if ch.isalnum() or ch in allowed or 0x4E00 <= ord(ch) <= 0x9FFF:
                cleaned.append(ch)
            else:
                cleaned.append("_")
        return ("".join(cleaned).strip()[:200]) or default

    def _is_valid_url(self, url: str) -> bool:
        try:
            p = urllib.parse.urlparse(url)
            return p.scheme in ("http", "https") and bool(p.netloc)
        except Exception:
            return False

    def _clean_text(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        text = re.sub(r"\[At:[^\]]+\]", "", text)
        text = re.sub(r"<at[^>]*>.*?</at>", "", text, flags=re.I | re.S)
        return re.sub(r"\s+", " ", text.strip().lstrip("/\\／﹨")).strip()

    def _guess_ext_from_ct(self, ct: str) -> Optional[str]:
        ct = (ct or "").split(";")[0].strip().lower()
        return {
            "image/jpeg": ".jpg", "image/jpg": ".jpg",
            "image/png":  ".png", "image/gif": ".gif",
            "image/bmp":  ".bmp", "image/webp": ".webp",
        }.get(ct)

    def _is_valid_img(self, name: str) -> bool:
        return name.lower().endswith(_VALID_EXT)

    def _cleanup_tmp(self, max_age: int = 3600):
        try:
            now = time.time()
            for fn in os.listdir(self.tmp_dl_dir):
                if not fn.startswith("pig_dl_"):
                    continue
                fp = os.path.join(self.tmp_dl_dir, fn)
                try:
                    if now - os.path.getmtime(fp) > max_age:
                        os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    def _quote_path(self, path: str) -> str:
        return "/".join(urllib.parse.quote(seg) for seg in path.split("/"))

    # ── 本地缓存读写 ───────────────────────────────────────────────────────

    def _load_cache(self):
        if not os.path.exists(self.filelist_cache):
            return
        try:
            with open(self.filelist_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            images = data.get("images", [])
            ts     = float(data.get("fetched_at", 0))
            if isinstance(images, list) and images:
                self.pig_images       = images
                self._list_fetched_at = ts
                logger.info(f"从缓存加载图片列表：{len(images)} 张")
        except Exception as e:
            logger.warning(f"读取 filelist.json 失败：{e}")

    def _save_cache(self, images: List[Dict]):
        tmp = f"{self.filelist_cache}.tmp_{int(time.time())}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": time.time(), "images": images},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.filelist_cache)
            logger.info(f"图片列表已缓存：{len(images)} 张")
        except Exception as e:
            logger.warning(f"保存 filelist.json 失败：{e}")
            try:
                os.path.exists(tmp) and os.remove(tmp)
            except Exception:
                pass

    # ── 图片列表获取 ───────────────────────────────────────────────────────

    async def _fetch_via_pighub(self) -> List[Dict]:
        """
        pighub.top API（sort=2 随机排序）。
        用 application/json Accept 头，避开 WAF 的 image/* 拦截规则。
        返回列表格式：[{"title", "full_url", "filename"}, ...]
        """
        logger.info(f"[列表] 请求 pighub API：{PIGHUB_API}")
        timeout = aiohttp.ClientTimeout(total=15)
        t0 = time.perf_counter()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=_API_HEADERS) as sess:
                async with sess.get(PIGHUB_API) as resp:
                    elapsed = (time.perf_counter() - t0) * 1000
                    ct = resp.headers.get("Content-Type", "")
                    logger.info(
                        f"[列表] pighub API 响应：HTTP {resp.status} | "
                        f"CT: {ct[:60]} | 耗时 {elapsed:.0f}ms"
                    )
                    if resp.status != 200:
                        body = (await resp.text(errors="ignore"))[:300]
                        logger.error(f"[列表] pighub API 非 200，响应体：{body}")
                        return []
                    payload = await resp.json(content_type=None)

            raw = payload if isinstance(payload, list) else payload.get("images", [])
            result: List[Dict] = []
            for img in raw:
                if not isinstance(img, dict):
                    continue
                thumb = img.get("thumbnail") or img.get("url") or ""
                if not thumb:
                    continue
                # 补全域名
                if thumb.startswith("/"):
                    full_url = PIGHUB_BASE + thumb
                elif self._is_valid_url(thumb):
                    full_url = thumb
                else:
                    full_url = PIGHUB_BASE + "/" + thumb.lstrip("/")

                # 对路径做 percent-encoding（保留 scheme://host/）
                try:
                    p = urllib.parse.urlparse(full_url)
                    full_url = urllib.parse.urlunparse(
                        (p.scheme, p.netloc, self._quote_path(p.path),
                         p.params, p.query, p.fragment)
                    )
                except Exception:
                    pass

                filename = img.get("filename") or os.path.basename(thumb)
                filename = self._sanitize_filename(filename)
                if not self._is_valid_img(filename):
                    filename += ".jpg"

                result.append({
                    "title":    img.get("title", "随机猪图"),
                    "full_url": full_url,
                    "filename": filename,
                })

            logger.info(f"[列表] pighub API 解析完成：{len(result)} 张")
            return result

        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"[列表] pighub API 超时（{elapsed:.0f}ms）")
            return []
        except aiohttp.ClientConnectorError as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"[列表] pighub API 连接失败（{elapsed:.0f}ms）[{type(e).__name__}]: {e}")
            return []
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"[列表] pighub API 异常（{elapsed:.0f}ms）[{type(e).__name__}]: {e}")
            return []

    async def _fetch_via_jsdelivr(self) -> List[Dict]:
        """jsDelivr flat API（备用，pighub 不可用时降级）。"""
        logger.info(f"[列表] 降级：请求 jsDelivr API：{JSDLVR_API}")
        timeout = aiohttp.ClientTimeout(total=20)
        t0 = time.perf_counter()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=_API_HEADERS) as sess:
                async with sess.get(JSDLVR_API) as resp:
                    elapsed = (time.perf_counter() - t0) * 1000
                    ct = resp.headers.get("Content-Type", "")
                    logger.info(
                        f"[列表] jsDelivr API 响应：HTTP {resp.status} | "
                        f"CT: {ct[:60]} | 耗时 {elapsed:.0f}ms"
                    )
                    if resp.status != 200:
                        body = (await resp.text(errors="ignore"))[:300]
                        logger.error(f"[列表] jsDelivr API 非 200，响应体：{body}")
                        return []
                    payload = await resp.json(content_type=None)

            result: List[Dict] = []
            for entry in payload.get("files", []):
                name = entry.get("name", "")
                if not name.startswith("/data/"):
                    continue
                filename = name[len("/data/"):]
                if not filename or not self._is_valid_img(filename):
                    continue
                encoded  = urllib.parse.quote(filename, safe="")
                full_url = JSDLVR_CDN + encoded
                result.append({
                    "title":    os.path.splitext(filename)[0],
                    "full_url": full_url,
                    "filename": filename,
                })

            logger.info(f"[列表] jsDelivr API 解析完成：{len(result)} 张")
            return result

        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"[列表] jsDelivr API 超时（{elapsed:.0f}ms）")
            return []
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(f"[列表] jsDelivr API 异常（{elapsed:.0f}ms）[{type(e).__name__}]: {e}")
            return []

    async def _fetch_imagelist(self) -> List[Dict]:
        """优先 pighub API，失败则降级 jsDelivr。"""
        images = await self._fetch_via_pighub()
        if images:
            return images
        logger.warning("[列表] pighub API 不可用，尝试 jsDelivr 备用")
        return await self._fetch_via_jsdelivr()

    async def _ensure_imagelist(self) -> bool:
        now = time.time()
        if self.pig_images and (now - self._list_fetched_at) < self.list_cache_ttl:
            return True
        async with self._list_lock:
            if self.pig_images and (time.time() - self._list_fetched_at) < self.list_cache_ttl:
                return True
            images = await self._fetch_imagelist()
            if images:
                self.pig_images       = images
                self._list_fetched_at = time.time()
                self._save_cache(images)
                return True
            if self.pig_images:
                logger.warning(f"[列表] 刷新失败，继续使用旧缓存（{len(self.pig_images)} 张）")
                return True
            return False

    # ── 图片下载（带详细日志 + 备用 URL）────────────────────────────────────

    async def _download_image(self, url: str, headers: dict = None) -> Optional[str]:
        if not self._is_valid_url(url):
            logger.warning(f"[下载] 无效 URL：{url}")
            return None
        hdrs    = headers or _IMG_HEADERS
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        t0 = time.perf_counter()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=hdrs) as sess:
                async with sess.get(url, allow_redirects=True) as resp:
                    elapsed = (time.perf_counter() - t0) * 1000
                    ct = resp.headers.get("Content-Type", "")
                    logger.info(
                        f"[下载] {url[:80]!r} → "
                        f"HTTP {resp.status} | CT: {ct[:50]} | 首字节 {elapsed:.0f}ms"
                    )
                    if resp.status != 200 or not ct.lower().startswith("image/"):
                        try:
                            preview = (await resp.text(errors="ignore"))[:300]
                        except Exception:
                            preview = ""
                        logger.error(
                            f"[下载] 非图片响应（{resp.status}，{ct}）：{preview}"
                        )
                        return None
                    data = await resp.read()
                    read_ms = (time.perf_counter() - t0) * 1000
                    logger.info(f"[下载] 完成 {len(data)} 字节，总耗时 {read_ms:.0f}ms")
                    if not data:
                        return None
                    ext = (self._guess_ext_from_ct(ct)
                           or os.path.splitext(urllib.parse.urlparse(url).path)[-1]
                           or ".jpg")
                    if not ext.startswith("."):
                        ext = "." + ext
                    fname = f"pig_dl_{int(time.time())}_{random.randint(0, 10**9)}{ext}"
                    tmp   = os.path.join(self.tmp_dl_dir, fname)
                    with open(tmp, "wb") as f:
                        f.write(data)
                    return tmp
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(
                f"[下载] 超时（{elapsed:.0f}ms > {self.request_timeout*1000:.0f}ms）：{url[:80]}"
            )
            return None
        except aiohttp.ClientConnectorError as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(
                f"[下载] 连接失败（{elapsed:.0f}ms）[{type(e).__name__}]: {e} | {url[:80]}"
            )
            return None
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error(
                f"[下载] 异常（{elapsed:.0f}ms）[{type(e).__name__}]: {e} | {url[:80]}"
            )
            return None

    def _build_fallback_url(self, filename: str) -> List[str]:
        """为某个文件名生成备用下载 URL 列表（jsDelivr → GitHub raw）。"""
        encoded = urllib.parse.quote(filename, safe="")
        return [
            JSDLVR_CDN + encoded,
            GHRAW_CDN  + encoded,
        ]

    async def _download_with_fallback(self, img: Dict) -> Optional[str]:
        """
        按优先级尝试下载：
          1. pighub.top 原始 URL（用浏览器 Accept 头绕过 WAF）
          2. jsDelivr CDN
          3. GitHub raw
        每个 URL 只试一次，失败立即换下一个，不在同一 URL 上反复重试。
        """
        primary_url  = img.get("full_url", "")
        filename     = img.get("filename", "")
        title        = img.get("title", filename)
        fallback_urls = self._build_fallback_url(filename) if filename else []

        all_urls = []
        if primary_url:
            all_urls.append(primary_url)
        for u in fallback_urls:
            if u not in all_urls:
                all_urls.append(u)

        for url in all_urls:
            async with self._download_semaphore:
                logger.info(f"[下载] 尝试：{title} → {url[:80]}")
                path = await self._download_image(url)
                if path:
                    return path
        logger.error(f"[下载] 所有 URL 均失败：{title}")
        return None

    # ── 本地缓存辅助 ──────────────────────────────────────────────────────

    async def _get_local_image(self, img: Dict) -> Optional[str]:
        filename = img.get("filename", "")
        if not filename:
            return None
        safe      = self._sanitize_filename(filename)
        local_abs = os.path.abspath(os.path.join(self.local_img_dir, safe))
        base_abs  = os.path.abspath(self.local_img_dir)
        if not local_abs.startswith(base_abs + os.sep):
            return None
        if os.path.exists(local_abs) and self._is_valid_img(local_abs):
            logger.info(f"命中本地缓存：{safe}")
            return local_abs
        path = await self._download_with_fallback(img)
        if not path:
            return None
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            td = os.path.join(self.local_img_dir,
                              f".tmp_{int(time.time())}_{random.randint(0, 10**9)}")
            shutil.copy2(path, td)
            os.replace(td, local_abs)
            return local_abs
        except Exception as e:
            logger.error(f"保存本地失败：{e}")
            return path if os.path.exists(path) else None

    async def _save_local_async(self, src: str, filename: str):
        try:
            safe = self._sanitize_filename(filename or os.path.basename(src))
            os.makedirs(self.local_img_dir, exist_ok=True)
            dest = os.path.join(self.local_img_dir, safe)
            td   = os.path.join(self.local_img_dir,
                                 f".tmp_{int(time.time())}_{random.randint(0, 10**9)}")
            shutil.copy2(src, td)
            os.replace(td, dest)
            logger.info("后台缓存完成：%s", dest)
        except Exception as e:
            logger.debug("后台缓存失败：%s", e)

    # ── 网络诊断 ──────────────────────────────────────────────────────────

    async def _probe_one(self, label: str, url: str, timeout: float = 8.0) -> str:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.hostname or ""
        t0 = time.perf_counter()
        try:
            loop  = asyncio.get_event_loop()
            addrs = await asyncio.wait_for(
                loop.run_in_executor(None, socket.getaddrinfo, host, None),
                timeout=5.0,
            )
            dns_ms = (time.perf_counter() - t0) * 1000
            ip     = addrs[0][4][0] if addrs else "?"
            dns_ok = f"DNS {dns_ms:.0f}ms → {ip}"
        except asyncio.TimeoutError:
            return f"❌ {label}：DNS 超时（>{5:.0f}s）"
        except Exception as e:
            return f"❌ {label}：DNS 失败 [{type(e).__name__}: {e}]"

        t1 = time.perf_counter()
        try:
            tc = aiohttp.ClientTimeout(connect=timeout/2, sock_read=timeout/2, total=timeout)
            async with aiohttp.ClientSession(timeout=tc, headers=_API_HEADERS) as sess:
                async with sess.get(url, allow_redirects=True) as resp:
                    await resp.content.read(256)
                    http_ms = (time.perf_counter() - t1) * 1000
                    ct = resp.headers.get("Content-Type", "")[:40]
                    return (f"✅ {label}：{dns_ok} | HTTP {resp.status} {http_ms:.0f}ms | {ct}")
        except asyncio.TimeoutError:
            http_ms = (time.perf_counter() - t1) * 1000
            return f"⚠️  {label}：{dns_ok} | HTTP 超时 {http_ms:.0f}ms"
        except aiohttp.ClientConnectorError as e:
            http_ms = (time.perf_counter() - t1) * 1000
            return f"❌ {label}：{dns_ok} | 连接失败 {http_ms:.0f}ms [{type(e).__name__}]"
        except Exception as e:
            http_ms = (time.perf_counter() - t1) * 1000
            return f"❌ {label}：{dns_ok} | 异常 {http_ms:.0f}ms [{type(e).__name__}: {e}]"

    async def _run_diagnostics(self, event: AstrMessageEvent):
        yield event.plain_result("[Pig] 开始网络诊断，探测所有候选图源（约 10 秒）……")
        tasks   = [self._probe_one(label, url) for label, url in PROBE_TARGETS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        lines   = ["[Pig] 网络诊断结果：\n"]
        for (label, _), res in zip(PROBE_TARGETS, results):
            lines.append(str(res) if not isinstance(res, Exception)
                         else f"❌ {label}：未知异常 [{type(res).__name__}: {res}]")
        report = "\n".join(lines)
        logger.info(report)
        yield event.plain_result(report)

    # ── 发图主流程 ────────────────────────────────────────────────────────

    def _is_on_cooldown(self, key: str) -> Tuple[bool, float]:
        elapsed = time.time() - self.last_called_times.get(key, 0)
        return elapsed < self.cooldown_period, max(0.0, self.cooldown_period - elapsed)

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        key = "pig"
        on_cd, remaining = self._is_on_cooldown(key)
        if on_cd:
            yield event.plain_result(f"冷却中～还需 {remaining:.0f} 秒")
            return

        if not await self._ensure_imagelist():
            yield event.plain_result(
                "暂时无法获取图片列表，请发送 /pig test 查看网络诊断"
            )
            return

        self._cleanup_tmp()
        start      = time.time()
        candidates = random.sample(self.pig_images, min(3, len(self.pig_images)))

        for img in candidates:
            if time.time() - start > self.overall_budget:
                logger.warning("[发图] 超出总耗时预算 %.1fs，提前放弃", self.overall_budget)
                break

            if self.load_to_local:
                try:
                    path = await self._get_local_image(img)
                    if path:
                        yield event.image_result(path)
                        self.last_called_times[key] = time.time()
                        return
                except Exception as e:
                    logger.error(f"[发图] 本地缓存出错：{e}")

            if time.time() - start > self.overall_budget:
                logger.warning("[发图] 超出总耗时预算 %.1fs，提前放弃", self.overall_budget)
                break

            path = await self._download_with_fallback(img)
            if path:
                yield event.image_result(path)
                self.last_called_times[key] = time.time()
                if self.load_to_local:
                    asyncio.create_task(
                        self._save_local_async(path, img.get("filename", ""))
                    )
                return

        yield event.plain_result(
            "获取猪图失败，请发送 /pig test 查看网络诊断结果"
        )
        self.last_called_times[key] = time.time()

    # ── 初始化 ────────────────────────────────────────────────────────────

    async def initialize(self):
        ok = await self._ensure_imagelist()
        logger.info(
            f"[初始化] 图片列表{'就绪：' + str(len(self.pig_images)) + ' 张' if ok else '获取失败，将在首次调用时重试'}"
        )
        logger.info(
            f"猪图插件 v0.1.5-t 初始化完成 | 图源：pighub API (sort=2) + jsDelivr 备用 | "
            f"冷却 {self.cooldown_period}s | 本地缓存 {self.load_to_local} | "
            f"列表 TTL {self.list_cache_ttl}s | "
            f"请求超时 {self.request_timeout}s | 总预算 {self.overall_budget}s"
        )

    # ── 手动刷新 ──────────────────────────────────────────────────────────

    async def _do_manual_update(self, event: AstrMessageEvent):
        async with self._list_lock:
            images = await self._fetch_imagelist()
            if not images:
                yield event.plain_result(
                    "[Pig] 手动更新失败，请发送 /pig test 查看网络诊断"
                )
                return
            self.pig_images       = images
            self._list_fetched_at = time.time()
            self._save_cache(images)
            yield event.plain_result(f"[Pig] 列表更新完成，共 {len(images)} 张")

    # ── 指令 ──────────────────────────────────────────────────────────────

    @filter.regex(r"(?i)^[/／]?pig(?:\s+(update|更新|test|诊断))?$")
    async def pig_command(self, event: AstrMessageEvent):
        """
        /pig        — 随机发送一张猪图
        /pig update — 强制刷新图片列表缓存
        /pig test   — 网络连通性诊断
        """
        raw   = getattr(event, "message_str", None) or getattr(event, "message", "") or ""
        clean = self._clean_text(str(raw))
        m     = re.match(r"(?i)^[/／]?pig(?:\s+(.+))?$", clean)
        sub   = (m.group(1) or "").strip().lower() if m else ""

        if sub in ("update", "更新"):
            async for r in self._do_manual_update(event):
                yield r
        elif sub in ("test", "诊断"):
            async for r in self._run_diagnostics(event):
                yield r
        else:
            async for r in self._get_random_pig_image(event):
                yield r

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def keyword_trigger(self, event: AstrMessageEvent):
        if not self.is_match_all_msg:
            return
        msg = event.message_str
        if not msg or msg.startswith(self.exclude_prefixes):
            return
        if self._is_trigger_keyword(msg, self.match_keywords):
            async for r in self._get_random_pig_image(event):
                yield r

    def _is_trigger_keyword(self, message: str, keywords: list) -> bool:
        if message.strip() in keywords:
            return True
        if not self.is_exact_match:
            return any(kw in message for kw in keywords)
        return False

    # ── 卸载 ──────────────────────────────────────────────────────────────

    async def terminate(self):
        logger.info("猪图插件 v0.1.5-t 已卸载")