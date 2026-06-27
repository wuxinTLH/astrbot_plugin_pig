import os
import re
import time
import json
import asyncio
import random
import shutil
import urllib.parse
import aiohttp

from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.io import download_image_by_url

PIGHUB_BASE = "https://pighub.top"
PIGHUB_API  = "https://pighub.top/api/images?sort=2&limit=10000"

# 图片实际下载走 GitHub raw（pighub 直链被 WAF 拦截，GitHub raw 可正常访问）
GHRAW_BASE  = "https://raw.githubusercontent.com/BadFish-HSrui/PigHub-DB/master/data/"

# 支持的图片后缀（从文件名中识别）
_VALID_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
              ".avif", ".tiff", ".tif", ".svg", ".ico")


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.1.5")
class PigRandomImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)

        try:
            self.cooldown_period = float(config.get("cooldown_period", 5))
        except Exception:
            self.cooldown_period = 5.0

        self.load_to_local = bool(config.get("load_to_local", False))

        try:
            self.max_retries = int(config.get("max_retries", 2))
        except Exception:
            self.max_retries = 2

        try:
            self.update_cycle = int(config.get("update_cycle", 0))
            if self.update_cycle < 0:
                self.update_cycle = 0
        except Exception:
            self.update_cycle = 0

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
        self.pig_images: List[Dict[str, Any]] = []

        base_dir = os.path.dirname(__file__)
        self.local_img_dir = os.path.join(base_dir, "imgs", "pig")
        self.json_path     = os.path.join(base_dir, "list.json")

        self._download_semaphore = asyncio.Semaphore(3)
        self._update_lock        = asyncio.Lock()
        self._scheduler_task: Optional[asyncio.Task] = None

        self._create_local_dir()
        self._load_pig_from_json()

    # ── 工具 ──────────────────────────────────────────────────────────────

    def _create_local_dir(self):
        if not self.load_to_local:
            return
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            logger.info(f"本地图片目录初始化完成：{self.local_img_dir}")
        except OSError as e:
            self.load_to_local = False
            logger.error(f"创建图片目录失败：{e}，已切换为仅网络加载")

    def _sanitize_filename(self, name: str, default: str = "image") -> str:
        if not name:
            name = default
        name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
        allowed = set("-_.() ")
        cleaned = []
        for ch in name:
            if ch.isalnum() or ch in allowed:
                cleaned.append(ch)
            elif 0x4E00 <= ord(ch) <= 0x9FFF:
                cleaned.append(ch)
            else:
                cleaned.append("_")
        result = "".join(cleaned).strip()[:200]
        return result or default

    def _quote_path(self, path: str) -> str:
        """对 URL 路径各段做 percent-encoding（保留斜杠）。"""
        return "/".join(urllib.parse.quote(seg) for seg in path.split("/"))

    def _is_valid_url(self, url: str) -> bool:
        try:
            p = urllib.parse.urlparse(url)
            return p.scheme in ("http", "https") and bool(p.netloc)
        except Exception:
            return False

    def _is_valid_img_suffix(self, filename: str) -> bool:
        return filename.lower().endswith(_VALID_EXT)

    def _detect_ext(self, filename: str) -> str:
        """从文件名中提取后缀；若无已知后缀则返回 .jpg 兜底。"""
        _, ext = os.path.splitext(filename)
        return ext.lower() if ext.lower() in _VALID_EXT else ".jpg"

    def _clean_text(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        text = re.sub(r"\[At:[^\]]+\]", "", text)
        text = re.sub(r"<at[^>]*>.*?</at>", "", text, flags=re.I | re.S)
        return re.sub(r"\s+", " ", text.strip().lstrip("/\\／﹨")).strip()

    def _build_full_url(self, image_url: str) -> Optional[str]:
        """
        将 API 返回的 image_url 字段（形如 '/images/叉烧….jpg'）
        拼成可直接下载的完整 URL，并对路径做 percent-encoding。
        """
        if not image_url:
            return None
        image_url = str(image_url)
        if self._is_valid_url(image_url):
            raw = image_url
        else:
            raw = PIGHUB_BASE + "/" + image_url.lstrip("/")
        try:
            p = urllib.parse.urlparse(raw)
            encoded = urllib.parse.urlunparse(
                (p.scheme, p.netloc, self._quote_path(p.path),
                 p.params, p.query, p.fragment)
            )
            return encoded
        except Exception:
            return None

    # ── JSON 加载 ──────────────────────────────────────────────────────────
    # list.json 格式：{"images": [{"id", "title", "filename", "image_url", ...}, ...]}
    # 与 pighub API 的 data 数组格式完全一致，直接存储 data 数组即可。

    def _load_pig_from_json(self):
        if not os.path.exists(self.json_path):
            logger.info("list.json 不存在，跳过本地加载")
            self.pig_images = []
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
        except Exception as e:
            logger.error(f"加载 list.json 失败：{e}")
            self.pig_images = []
            return

        raw_images = json_data.get("images", []) if isinstance(json_data, dict) else []
        self.pig_images.clear()
        for img in raw_images:
            if not isinstance(img, dict):
                continue

            # 文件名：优先 filename 字段，否则从 image_url 路径推断
            image_url_field = img.get("image_url", "")
            filename = img.get("filename") or os.path.basename(image_url_field)
            filename = self._sanitize_filename(str(filename))
            if not filename:
                logger.warning(f"跳过无效条目：{img.get('title', '未知')}")
                continue
            if not self._is_valid_img_suffix(filename):
                filename += ".jpg"

            # 图片下载走 GitHub raw（pighub 直链被 WAF 拦截，API 列表接口不受影响）
            encoded_filename = urllib.parse.quote(filename, safe="")
            full_url = GHRAW_BASE + encoded_filename

            self.pig_images.append({
                "title":    img.get("title", "随机猪图"),
                "full_url": full_url,
                "filename": filename,
                "id":       img.get("id"),
            })

        logger.info(f"图片配置加载成功，共 {len(self.pig_images)} 张")

    # ── 远程拉取 & 更新 ────────────────────────────────────────────────────

    async def _fetch_remote_images(self) -> Optional[dict]:
        """
        请求 pighub API，返回标准化后的 {"images": [...]} 结构；
        失败返回 None。

        API 响应结构：
          {"code": 0, "message": "OK", "data": [{id, title, filename, image_url, ...}]}
        """
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as sess:
                async with sess.get(PIGHUB_API) as resp:
                    if resp.status != 200:
                        logger.error(f"远程请求失败，状态码：{resp.status}")
                        return None
                    payload = await resp.json(content_type=None)

            if not isinstance(payload, dict):
                logger.error("远程响应非 JSON 对象")
                return None
            if payload.get("code") != 0:
                logger.error(f"远程接口返回错误：code={payload.get('code')}, message={payload.get('message')}")
                return None
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                logger.error("远程接口 data 字段为空或格式错误")
                return None

            logger.info(f"远程接口返回 {len(data)} 张图片")
            return {"images": data}

        except asyncio.TimeoutError:
            logger.error("远程请求超时")
            return None
        except Exception as e:
            logger.error(f"远程请求异常：{e}")
            return None

    def _apply_remote_data_if_needed(self, remote_data: dict) -> bool:
        if not isinstance(remote_data, dict):
            return False
        remote_images = remote_data.get("images")
        if not isinstance(remote_images, list) or not remote_images:
            logger.error("远程数据无效，拒绝更新")
            return False

        local_data = None
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
            except Exception:
                local_data = None

        def extract_ids(d):
            if not isinstance(d, dict):
                return set()
            imgs = d.get("images") or []
            return {i.get("id") for i in imgs if isinstance(i, dict) and "id" in i}

        local_ids  = extract_ids(local_data)
        remote_ids = extract_ids(remote_data)
        need_update = (
            not local_data
            or local_ids != remote_ids
            or len((local_data or {}).get("images", [])) != len(remote_images)
        )
        if not need_update:
            return False

        tmp = f"{self.json_path}.tmp_{int(time.time())}_{random.randint(0, 10**9)}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(remote_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.json_path)
            logger.info("list.json 已更新（远程变化）")
            self._load_pig_from_json()
            return True
        except Exception as e:
            logger.error(f"保存 list.json 失败：{e}")
            try:
                os.path.exists(tmp) and os.remove(tmp)
            except Exception:
                pass
            return False

    # ── 初始化 ─────────────────────────────────────────────────────────────

    async def initialize(self):
        remote_data = await self._fetch_remote_images()
        if remote_data:
            try:
                updated = self._apply_remote_data_if_needed(remote_data)
                logger.info("初始化：" + ("已从远程更新本地列表" if updated else "本地列表已是最新"))
            except Exception as e:
                logger.error(f"处理远程数据时出错：{e}")

        if self.update_cycle > 0:
            if self._scheduler_task and not self._scheduler_task.done():
                self._scheduler_task.cancel()
            self._scheduler_task = asyncio.create_task(self._update_cycle_task())
            logger.info(f"已启动后台更新调度（周期：{self.update_cycle} 天）")
        else:
            logger.info("未启用后台自动更新（update_cycle=0）")

        logger.info(
            f"猪图插件 v0.1.5 初始化完成 | "
            f"冷却 {self.cooldown_period}s | 本地缓存 {self.load_to_local} | "
            f"更新周期 {self.update_cycle} 天 | 已加载 {len(self.pig_images)} 张"
        )

    # ── 下载与缓存 ─────────────────────────────────────────────────────────

    def _is_on_cooldown(self, key: str) -> Tuple[bool, float]:
        elapsed = time.time() - self.last_called_times.get(key, 0)
        return elapsed < self.cooldown_period, max(0.0, self.cooldown_period - elapsed)

    async def _get_local_image(self, selected_img: dict) -> Optional[str]:
        img_filename = selected_img.get("filename")
        if not img_filename:
            return None

        local_abs = os.path.abspath(os.path.join(self.local_img_dir, img_filename))
        base_abs  = os.path.abspath(self.local_img_dir)
        if not local_abs.startswith(base_abs + os.sep):
            logger.warning("可疑路径，拒绝：%s", local_abs)
            return None

        if os.path.exists(local_abs) and self._is_valid_img_suffix(local_abs):
            logger.info(f"使用本地缓存：{img_filename}")
            return local_abs

        logger.info(f"本地缺失，开始下载：{img_filename}")
        url = selected_img.get("full_url", "")
        if not self._is_valid_url(url):
            return None

        async with self._download_semaphore:
            try:
                temp_path = await download_image_by_url(url)
            except Exception as e:
                logger.error(f"download_image_by_url 出错：{e}")
                temp_path = None

        if not temp_path or not self._is_valid_img_suffix(os.path.basename(temp_path)):
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            logger.error(f"下载失败或非图片格式：{img_filename}")
            return None

        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            td = os.path.join(self.local_img_dir,
                              f".tmp_{int(time.time())}_{random.randint(0, 10**9)}")
            shutil.copy2(temp_path, td)
            os.replace(td, local_abs)
            logger.info(f"已缓存到本地：{local_abs}")
            return local_abs
        except Exception as e:
            logger.error(f"保存本地失败：{e}")
            return temp_path if os.path.exists(temp_path) else None

    async def _download_with_retries(self, url: str, title: str) -> Optional[str]:
        if not self._is_valid_url(url):
            logger.warning("无效 URL：%s", url)
            return None

        for attempt in range(1, max(1, self.max_retries) + 1):
            async with self._download_semaphore:
                try:
                    logger.info(f"下载尝试 {attempt}/{self.max_retries}：{title}")
                    temp_path = await download_image_by_url(url)
                    if not temp_path:
                        raise RuntimeError("download returned None")
                    if not self._is_valid_img_suffix(os.path.basename(temp_path)):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                        raise RuntimeError("非图片格式")
                    return temp_path
                except Exception as e:
                    logger.debug(f"下载尝试 {attempt}/{self.max_retries} 失败：{str(e)[:120]}")
                    if attempt >= max(1, self.max_retries):
                        logger.error(f"获取 {title} 失败（共 {attempt} 次）")
                        return None
                    await asyncio.sleep(min(2.0, 1.5 ** attempt))
        return None

    async def _save_to_local_cache_async(self, downloaded_path: str, target_filename: str):
        if not downloaded_path:
            return
        try:
            if not target_filename:
                target_filename = os.path.basename(downloaded_path)
            safe = self._sanitize_filename(str(target_filename))
            os.makedirs(self.local_img_dir, exist_ok=True)
            dest = os.path.join(self.local_img_dir, safe)
            td   = os.path.join(self.local_img_dir,
                                 f".tmp_{int(time.time())}_{random.randint(0, 10**9)}")
            shutil.copy2(downloaded_path, td)
            os.replace(td, dest)
            logger.info("后台缓存完成：%s", dest)
        except Exception as e:
            logger.debug("后台缓存失败：%s", e)

    # ── 发图主流程 ─────────────────────────────────────────────────────────

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        key = "pig"
        on_cd, remaining = self._is_on_cooldown(key)
        if on_cd:
            yield event.plain_result(f"冷却中～还需 {remaining:.0f} 秒")
            return

        if not self.pig_images:
            yield event.plain_result("无可用猪图数据，请稍后重试")
            return

        tried: set = set()
        max_candidates = min(len(self.pig_images), 3)
        for _ in range(max_candidates):
            idx = random.randrange(len(self.pig_images))
            if idx in tried and len(tried) < len(self.pig_images):
                continue
            tried.add(idx)
            selected_img = self.pig_images[idx]
            img_title    = selected_img.get("title", "随机猪图")

            if self.load_to_local:
                try:
                    img_path = await self._get_local_image(selected_img)
                    if img_path:
                        yield event.image_result(img_path)
                        self.last_called_times[key] = time.time()
                        return
                    logger.debug("本地加载失败，切换为网络加载")
                except Exception as e:
                    logger.error(f"本地加载出错：{e}")

            temp_path = await self._download_with_retries(
                selected_img.get("full_url", ""), img_title
            )
            if temp_path:
                yield event.image_result(temp_path)
                self.last_called_times[key] = time.time()
                if self.load_to_local:
                    asyncio.create_task(
                        self._save_to_local_cache_async(temp_path, selected_img.get("filename"))
                    )
                return

        yield event.plain_result("获取猪图失败，请稍后重试")
        self.last_called_times[key] = time.time()

    # ── 定时更新调度 ───────────────────────────────────────────────────────

    async def _update_cycle_task(self):
        try:
            while True:
                now = time.time()
                lt  = time.localtime(now)
                try:
                    next_mid = time.mktime((
                        lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                        0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst
                    ))
                    sleep_s = max(0, int(next_mid - now) + 1)
                except Exception:
                    sleep_s = 60

                logger.info("后台更新调度：等待 %d 秒至下一个零点", sleep_s)
                try:
                    await asyncio.sleep(sleep_s)
                except asyncio.CancelledError:
                    break

                try:
                    remote_data = await self._fetch_remote_images()
                    if remote_data:
                        updated = self._apply_remote_data_if_needed(remote_data)
                        logger.info("后台更新：" + ("已更新" if updated else "无变化"))
                    else:
                        logger.warning("后台更新：无法获取远程数据，跳过")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"后台更新出错：{e}")

                if self.update_cycle > 1:
                    try:
                        await asyncio.sleep((self.update_cycle - 1) * 86400)
                    except asyncio.CancelledError:
                        break
        finally:
            logger.info("后台更新调度退出")

    # ── 手动更新 ───────────────────────────────────────────────────────────

    async def _do_manual_update(self, event: AstrMessageEvent):
        async with self._update_lock:
            try:
                remote_data = await self._fetch_remote_images()
                if not remote_data:
                    yield event.plain_result("[Pig] 手动更新失败：无法拉取远程数据")
                    return
                updated = self._apply_remote_data_if_needed(remote_data)
                msg = "手动更新成功：本地 list.json 已更新" if updated else "手动更新完成：本地已是最新"
                yield event.plain_result(f"[Pig] {msg}")
            except Exception as e:
                logger.error(f"手动更新异常：{e}")
                yield event.plain_result(f"[Pig] 手动更新失败：{e}")

    # ── 指令 ───────────────────────────────────────────────────────────────

    @filter.regex(r"(?i)^[/／]?pig(?:\s+(update|更新))?$")
    async def pig_command(self, event: AstrMessageEvent):
        """
        /pig        — 发送随机猪图
        /pig update — 手动刷新图片列表
        /pig 更新  — 同上
        """
        raw   = getattr(event, "message_str", None) or getattr(event, "message", "") or ""
        clean = self._clean_text(str(raw))
        m     = re.match(r"(?i)^[/／]?pig(?:\s+(.+))?$", clean)
        sub   = (m.group(1) or "").strip().lower() if m else ""
        if sub in ("update", "更新"):
            async for r in self._do_manual_update(event):
                yield r
            return
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

    # ── 卸载 ───────────────────────────────────────────────────────────────

    async def terminate(self):
        if self._scheduler_task:
            try:
                self._scheduler_task.cancel()
                try:
                    await asyncio.wait_for(self._scheduler_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            except Exception as e:
                logger.debug("取消调度任务出错：%s", e)
            finally:
                self._scheduler_task = None
        logger.info("猪图插件 v0.1.5 已卸载")