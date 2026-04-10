import os
import re
import time
import json
import asyncio
import random
import shutil
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import ClientTimeout, TCPConnector

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.io import download_image_by_url


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.1.2")
class PigRandomImagePlugin(Star):

    # ══════════════════════════════════════════
    #  初始化
    # ══════════════════════════════════════════

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)

        # ── 配置读取（带类型转换和默认值）──
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
            self.update_cycle = max(0, int(config.get("update_cycle", 0)))
        except Exception:
            self.update_cycle = 0

        try:
            # is_match_all_msg=True：只要消息不以 exclude_prefixes 开头就回复，不再检查关键词
            # is_match_all_msg=False（默认）：仅当消息命中 match_keywords 时回复
            self.is_match_all_msg = bool(config.get("is_match_all_msg", False))
        except Exception:
            self.is_match_all_msg = False

        try:
            # is_exact_match=True：消息整体等于关键词才触发（精确匹配）
            # is_exact_match=False：消息包含关键词即触发（模糊匹配）
            self.is_exact_match = bool(config.get("is_exact_match", True))
        except Exception:
            self.is_exact_match = True

        try:
            self.match_keywords: List[str] = list(config.get("match_keywords", []))
        except Exception:
            self.match_keywords = ["猪", "祝", "🐷", "🐖", "🐽", "㊗", "㊗️"]

        try:
            self.exclude_prefixes: Tuple[str, ...] = tuple(
                config.get("exclude_prefixes", ())
            )
        except Exception:
            self.exclude_prefixes = ("/", "!", "！", "#", "ww")

        # ── 路径 ──
        base_dir = os.path.dirname(__file__)
        self.local_img_dir = os.path.join(base_dir, "imgs", "pig")
        self.json_path = os.path.join(base_dir, "list.json")

        # ── 内存状态 ──
        # FIX-1：冷却表 key 改为 (user_id, group_id) 元组，实现用户级隔离
        # 原版：Dict[str, float]，全部用 "pig" 作 key，所有用户共用同一冷却
        self.last_called_times: Dict[Tuple[str, str], float] = {}
        self.pig_images: List[Dict[str, Any]] = []

        # ── 并发控制 ──
        self._download_semaphore = asyncio.Semaphore(3)
        self._update_lock = asyncio.Lock()
        self._scheduler_task: Optional[asyncio.Task] = None

        # FIX-PERF：全局复用 aiohttp session，避免每次请求重新建立连接
        # 原版：_fetch_remote_images 内每次都 async with aiohttp.ClientSession(...)
        self._http_session: Optional[aiohttp.ClientSession] = None

        self._create_local_dir()
        self._load_pig_from_json()

    # ══════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════

    async def initialize(self):
        # 创建全局 HTTP session
        conn = TCPConnector(ssl=True, limit=4, ttl_dns_cache=300, use_dns_cache=True)
        self._http_session = aiohttp.ClientSession(
            connector=conn,
            timeout=ClientTimeout(total=15),
        )

        remote_data = await self._fetch_remote_images()
        if remote_data:
            try:
                updated = self._apply_remote_data_if_needed(remote_data)
                if updated:
                    logger.info("[Pig] 初始化时已从远程更新本地列表")
                else:
                    logger.info("[Pig] 本地 list.json 与远程一致，无需更新")
            except Exception as e:
                logger.error(f"[Pig] 处理远程数据出错：{e}")

        if self.update_cycle > 0:
            if self._scheduler_task and not self._scheduler_task.done():
                self._scheduler_task.cancel()
            self._scheduler_task = asyncio.create_task(self._update_cycle_task())
            logger.info(f"[Pig] 已启动后台更新调度器（周期：{self.update_cycle} 天）")
        else:
            logger.info("[Pig] 未启用后台自动更新（update_cycle=0）")

        logger.info(
            f"[Pig] 插件初始化完成 | "
            f"冷却 {self.cooldown_period}s | "
            f"本地缓存 {self.load_to_local} | "
            f"更新周期 {self.update_cycle}d"
        )

    async def terminate(self):
        # FIX-7：terminate 中先 cancel 再 await，直接捕获 CancelledError
        # 原版：cancel() 后再 wait_for()，wait_for 在已取消任务上是冗余且误导性的
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[Pig] 调度任务退出异常：{e}")
            finally:
                self._scheduler_task = None

        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception:
                pass

        logger.info("[Pig] 插件已卸载")

    # ══════════════════════════════════════════
    #  工具函数
    # ══════════════════════════════════════════

    def _create_local_dir(self):
        if not self.load_to_local:
            return
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            logger.info(f"[Pig] 本地图片目录初始化完成：{self.local_img_dir}")
        except OSError as e:
            self.load_to_local = False
            logger.error(f"[Pig] 创建图片目录失败：{e}，已切换为仅网络加载")

    def _sanitize_filename(self, name: str, default: str = "image") -> str:
        if not name:
            name = default
        name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
        allowed = set("-_.() ")
        cleaned = []
        for ch in name:
            if ch.isalnum() or ch in allowed or (0x4E00 <= ord(ch) <= 0x9FFF):
                cleaned.append(ch)
            else:
                cleaned.append("_")
        result = "".join(cleaned).strip()[:200]
        return result or default

    def _quote_path_preserving_slashes(self, path: str) -> str:
        return "/".join(urllib.parse.quote(seg) for seg in path.split("/"))

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
        text = text.strip().lstrip("/\\／﹨")
        return re.sub(r"\s+", " ", text).strip()

    def _get_identity(self, event: AstrMessageEvent) -> Tuple[str, str]:
        """提取 (user_id, group_id) 二元组，用于用户级冷却隔离。"""
        user_id = (
            getattr(event, "user_id", None)
            or getattr(getattr(event, "sender", None), "user_id", None)
            or getattr(getattr(event, "from_user", None), "id", None)
            or getattr(getattr(event, "author", None), "id", None)
        )
        user_id = str(user_id) if user_id else f"anon_{int(time.time() // 600)}"

        group_id = (
            getattr(event, "group_id", None)
            or getattr(getattr(event, "session", None), "group_id", None)
            or getattr(getattr(event, "group", None), "id", None)
        )
        group_id = str(group_id) if group_id else f"private_{abs(hash(user_id)) % 100003}"

        return user_id, group_id

    # FIX-1：冷却检查改为接受 identity 元组
    # 原版：接受 command_name: str，调用方传 "pig"，全局共享
    def _is_on_cooldown(self, identity: Tuple[str, str]) -> Tuple[bool, float]:
        last = self.last_called_times.get(identity, 0.0)
        remaining = self.cooldown_period - (time.time() - last)
        return (True, max(0.0, remaining)) if remaining > 0 else (False, 0.0)

    def _is_valid_image_suffix(self, filename: str) -> bool:
        return filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"))

    # ══════════════════════════════════════════
    #  JSON 加载 / 远程更新
    # ══════════════════════════════════════════

    def _load_pig_from_json(self):
        if not os.path.exists(self.json_path):
            logger.info("[Pig] list.json 不存在，跳过本地加载")
            self.pig_images = []
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
        except Exception as e:
            logger.error(f"[Pig] 加载 list.json 失败：{e}")
            self.pig_images = []
            return

        raw_images = json_data.get("images", []) if isinstance(json_data, dict) else []
        self.pig_images.clear()
        base_url = "https://pighub.top/"
        valid_suffixes = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")

        for img in raw_images:
            if not isinstance(img, dict):
                continue
            thumbnail = img.get("thumbnail", "")
            if not thumbnail:
                logger.warning(f"[Pig] 跳过空 thumbnail：{img.get('title', '未知')}")
                continue
            thumbnail = str(thumbnail).lstrip("/")

            if self._is_valid_url(thumbnail):
                unencoded_url = thumbnail
            else:
                unencoded_url = urllib.parse.urljoin(base_url, thumbnail)

            try:
                parsed = urllib.parse.urlparse(unencoded_url)
                encoded_path = self._quote_path_preserving_slashes(parsed.path)
                full_url = urllib.parse.urlunparse((
                    parsed.scheme, parsed.netloc, encoded_path,
                    parsed.params, parsed.query, parsed.fragment,
                ))
            except Exception:
                logger.debug(f"[Pig] 构建 URL 失败，跳过：{unencoded_url}")
                continue

            img_filename = img.get("filename")
            if img_filename:
                img_filename = self._sanitize_filename(str(img_filename))
            else:
                ext = os.path.splitext(thumbnail)[-1]
                if not ext or "." not in ext:
                    ext = ".jpg"
                img_filename = self._sanitize_filename(f"{img.get('title', '未知图片')}{ext}")

            if not img_filename.lower().endswith(valid_suffixes):
                img_filename += ".jpg"

            self.pig_images.append({
                "title": img.get("title", "随机猪图"),
                "full_url": full_url,
                "filename": img_filename,
                "id": img.get("id"),
            })

        logger.info(f"[Pig] 图片配置加载完成，共 {len(self.pig_images)} 张")

    async def _fetch_remote_images(self) -> Optional[Dict]:
        """从 pighub.top 拉取最新图片列表。复用全局 session（如可用）。"""
        url = "https://pighub.top/api/images?limit=10000&sort=latest"
        # FIX-PERF：优先复用全局 session；session 不可用时临时创建
        # 原版：每次都 async with aiohttp.ClientSession(...) 新建连接
        if self._http_session and not self._http_session.closed:
            session = self._http_session
            own_session = False
        else:
            session = aiohttp.ClientSession(timeout=ClientTimeout(total=15))
            own_session = True

        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"[Pig] 远程请求失败，状态码：{resp.status}")
                return None
        except Exception as e:
            logger.error(f"[Pig] 远程请求异常：{e}")
            return None
        finally:
            if own_session:
                await session.close()

    def _apply_remote_data_if_needed(self, remote_data: Dict) -> bool:
        if not isinstance(remote_data, dict):
            return False

        local_data = None
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
            except Exception:
                local_data = None

        def extract_ids(d):
            if not d or not isinstance(d, dict):
                return set()
            imgs = d.get("images")
            if not isinstance(imgs, list):
                return set()
            return {item.get("id") for item in imgs if isinstance(item, dict) and "id" in item}

        need_update = (
            not local_data
            or extract_ids(local_data) != extract_ids(remote_data)
            or len(local_data.get("images", [])) != len(remote_data.get("images", []))
        )
        if not need_update:
            return False

        tmp_path = f"{self.json_path}.tmp_{int(time.time())}_{random.randint(0, 10**9)}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(remote_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.json_path)
            logger.info("[Pig] list.json 已更新（远程有变化）")
            self._load_pig_from_json()
            return True
        except Exception as e:
            logger.error(f"[Pig] 更新 list.json 失败：{e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

    # ══════════════════════════════════════════
    #  下载与本地缓存
    # ══════════════════════════════════════════

    async def _get_local_image(self, selected_img: Dict) -> Optional[str]:
        img_filename = selected_img.get("filename")
        if not img_filename:
            return None
        local_img_path = os.path.join(self.local_img_dir, img_filename)

        try:
            local_abs = os.path.abspath(local_img_path)
            base_abs = os.path.abspath(self.local_img_dir)
            if not (local_abs == base_abs or local_abs.startswith(base_abs + os.sep)):
                logger.warning(f"[Pig] 可疑路径，拒绝访问：{local_img_path}")
                return None
        except Exception:
            return None

        if os.path.exists(local_abs) and self._is_valid_image_suffix(local_abs):
            logger.info(f"[Pig] 使用本地图片：{img_filename}")
            return local_abs

        url = selected_img.get("full_url", "")
        if not self._is_valid_url(url):
            return None

        logger.info(f"[Pig] 本地缺失，下载：{img_filename}")
        async with self._download_semaphore:
            try:
                temp_path = await download_image_by_url(url)
            except Exception as e:
                logger.error(f"[Pig] download_image_by_url 异常：{e}")
                temp_path = None

        if not temp_path:
            return None

        if not self._is_valid_image_suffix(os.path.basename(temp_path)):
            try:
                os.remove(temp_path)
            except Exception:
                pass
            return None

        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            tmp_dest = os.path.join(
                self.local_img_dir,
                f".tmp_{int(time.time())}_{random.randint(0, 10**9)}",
            )
            shutil.copy2(temp_path, tmp_dest)
            os.replace(tmp_dest, local_abs)
            return local_abs
        except Exception as e:
            logger.error(f"[Pig] 保存本地失败：{e}")
            return temp_path if os.path.exists(temp_path) else None

    async def _download_with_retries(self, url: str, title: str) -> Optional[str]:
        if not self._is_valid_url(url):
            return None
        for attempt in range(1, max(1, self.max_retries) + 1):
            async with self._download_semaphore:
                try:
                    logger.info(f"[Pig] 网络加载 {attempt}/{self.max_retries}：{title}")
                    temp_path = await download_image_by_url(url)
                    if not temp_path:
                        raise RuntimeError("download returned None")
                    if not self._is_valid_image_suffix(os.path.basename(temp_path)):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                        raise RuntimeError("非图片格式")
                    return temp_path
                except Exception as e:
                    logger.debug(f"[Pig] 下载失败（{attempt}/{self.max_retries}）：{str(e)[:120]}")
                    if attempt >= max(1, self.max_retries):
                        logger.error(f"[Pig] 获取 {title} 最终失败")
                        return None
                    await asyncio.sleep(min(2 ** attempt + random.random(), 10))
        return None

    async def _save_to_local_cache_async(self, downloaded_path: str, target_filename: Optional[str]):
        if not downloaded_path:
            return
        tmp_dest = None
        try:
            if not target_filename:
                target_filename = os.path.basename(downloaded_path)
            safe_name = self._sanitize_filename(str(target_filename))
            os.makedirs(self.local_img_dir, exist_ok=True)
            dest_path = os.path.join(self.local_img_dir, safe_name)
            tmp_dest = os.path.join(
                self.local_img_dir,
                f".tmp_{int(time.time())}_{random.randint(0, 10**9)}",
            )
            shutil.copy2(downloaded_path, tmp_dest)
            os.replace(tmp_dest, dest_path)
            logger.info(f"[Pig] 后台缓存图片：{dest_path}")
        except Exception as e:
            logger.debug(f"[Pig] 后台保存失败：{e}")
            if tmp_dest:
                try:
                    if os.path.exists(tmp_dest):
                        os.remove(tmp_dest)
                except Exception:
                    pass

    # ══════════════════════════════════════════
    #  随机猪图主逻辑
    # ══════════════════════════════════════════

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        # FIX-1：冷却改为用户级 identity，不同用户独立冷却
        # 原版：command_name = "pig"，所有用户共享
        identity = self._get_identity(event)
        on_cooldown, remaining = self._is_on_cooldown(identity)
        if on_cooldown:
            yield event.plain_result(f"冷却中～还需 {remaining:.0f} 秒")
            return

        if not self.pig_images:
            yield event.plain_result("无可用猪图数据")
            return

        # FIX-4：修复随机去重逻辑
        # 原版：continue 后 for 循环变量继续推进，实际不能保证跳过重复 idx
        # 修复：用 random.sample 直接取不重复候选下标列表
        pool_size = len(self.pig_images)
        max_candidates = min(pool_size, 5)
        candidates = random.sample(range(pool_size), max_candidates)

        for idx in candidates:
            selected_img = self.pig_images[idx]
            img_title = selected_img.get("title", "随机猪图")

            if self.load_to_local:
                try:
                    img_path = await self._get_local_image(selected_img)
                    if img_path:
                        yield event.image_result(img_path)
                        self.last_called_times[identity] = time.time()
                        return
                    logger.debug("[Pig] 本地加载失败，切换网络")
                except Exception as e:
                    logger.error(f"[Pig] 本地加载异常：{e}，切换网络")

            temp_path = await self._download_with_retries(
                selected_img.get("full_url", ""), img_title
            )
            if temp_path:
                yield event.image_result(temp_path)
                self.last_called_times[identity] = time.time()
                if self.load_to_local:
                    asyncio.create_task(
                        self._save_to_local_cache_async(
                            temp_path, selected_img.get("filename")
                        )
                    )
                return

        yield event.plain_result("获取猪图失败，请稍后重试")
        self.last_called_times[identity] = time.time()

    # ══════════════════════════════════════════
    #  后台定时更新
    # ══════════════════════════════════════════

    async def _update_cycle_task(self):
        """按 update_cycle（天）周期在每日零点检查并更新图片列表。"""
        try:
            while True:
                # FIX-5：改用 datetime 计算零点，避免 tm_mday+1 月末溢出和夏令时偏差
                # 原版：time.mktime((... lt.tm_mday + 1 ...)) 月末可能产生偏差
                now = datetime.now()
                next_midnight = datetime.combine(
                    (now + timedelta(days=1)).date(),
                    datetime.min.time(),
                )
                sleep_seconds = max(0.0, (next_midnight - now).total_seconds()) + 1

                logger.info(f"[Pig] 调度器：等待 {sleep_seconds:.0f}s 到下一个零点")
                try:
                    await asyncio.sleep(sleep_seconds)
                except asyncio.CancelledError:
                    break

                try:
                    remote_data = await self._fetch_remote_images()
                    if remote_data:
                        updated = self._apply_remote_data_if_needed(remote_data)
                        logger.info(
                            f"[Pig] 调度器：{'已更新' if updated else '无需更新'} list.json"
                        )
                    else:
                        logger.warning("[Pig] 调度器：获取远程数据失败，跳过本次")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[Pig] 调度器执行更新异常：{e}")

                # 若 update_cycle > 1，额外等待剩余天数
                if self.update_cycle > 1:
                    extra = (self.update_cycle - 1) * 24 * 3600
                    logger.info(f"[Pig] 调度器：额外等待 {self.update_cycle - 1} 天")
                    try:
                        await asyncio.sleep(extra)
                    except asyncio.CancelledError:
                        break
        finally:
            logger.info("[Pig] 调度任务退出")

    # ══════════════════════════════════════════
    #  手动更新逻辑（抽取为独立方法，供命令复用）
    # ══════════════════════════════════════════

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
                logger.error(f"[Pig] 手动更新异常：{e}")
                yield event.plain_result(f"[Pig] 手动更新失败：{e}")

    # ══════════════════════════════════════════
    #  命令绑定
    # ══════════════════════════════════════════

    # ── pig 主指令（大小写不敏感）─────────────────────────────────────────────
    # filter.regex + (?i) 替代原版 filter.command("pig")
    # /pig  /Pig  /PIG  /pIg  pig  PIG（前缀 / 可选，大小写任意）
    # 支持子命令：/pig update | /pig 更新
    @filter.regex(r"(?i)^[/／]?pig(?:\s+(update|更新))?$")
    async def pig_command(self, event: AstrMessageEvent):
        """
        /pig          — 发送随机猪图（/Pig /PIG /pIg 等均可）
        /pig update   — 手动拉取最新图片列表
        /pig 更新   — 同上
        """
        raw = getattr(event, "message_str", None) or getattr(event, "message", "") or ""
        clean = self._clean_text(str(raw))
        m = re.match(r"(?i)^[/／]?pig(?:\s+(.+))?$", clean)
        sub = m.group(1).strip().lower() if (m and m.group(1)) else None
        is_update = sub in ("update", "更新")
        if is_update:
            async for r in self._do_manual_update(event):
                yield r
            return
        async for r in self._get_random_pig_image(event):
            yield r

    # ── 原版 alias={"猪","祝","猪猪","猪猪图"} 保留 ───────────────────────────
    # 原版写法：filter.command("pig", alias={"猪","祝","猪猪","猪猪图"})
    # alias 在 AstrBot 里是精确前缀匹配，对这四个中文词功能正常。
    # 此处改为独立 filter.regex，精确匹配整条消息等于这些词的情况，
    # 与下方 keyword_trigger 的"包含匹配"形成互补：
    #   独立发"猪" / "祝" / "猪猪" / "猪猪图"           → pig_alias_command（精确）
    #   发"今天天气真好猪猪"（is_exact_match=False 时） → keyword_trigger（包含）
    @filter.regex(r"^(?:猪|祝|猪猪|猪猪图)$")
    async def pig_alias_command(self, event: AstrMessageEvent):
        """原版 alias 中文指令：猪 / 祝 / 猪猪 / 猪猪图（精确消息触发，保持原有行为）"""
        async for r in self._get_random_pig_image(event):
            yield r

    # @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    # async def keyword_trigger(self, event: AstrMessageEvent):
    #     message_str = event.message_str
    #     if not message_str:
    #         return

    #     msg = message_str.strip()

    #     # ── 去重：已被 pig_command 或 pig_alias_command 处理的消息不重复触发 ──
    #     # pig_command 覆盖：/pig /Pig /PIG pig PIG 等（含可选子命令）
    #     if re.match(r"(?i)^[/／]?pig(\s+.*)?$", msg):
    #         return
    #     # pig_alias_command 覆盖：猪 / 祝 / 猪猪 / 猪猪图（精确）
    #     if re.match(r"^(?:猪|祝|猪猪|猪猪图)$", msg):
    #         return
    #     # exclude_prefixes：命令前缀开头一律排除
    #     if message_str.startswith(self.exclude_prefixes):
    #         return

    #     if self.is_match_all_msg:
    #         # 匹配所有消息模式：不检查关键词，直接触发
    #         async for r in self._get_random_pig_image(event):
    #             yield r
    #     else:
    #         # 默认模式：仅当消息命中 match_keywords 才触发
    #         if self._is_trigger_keyword(message_str, self.match_keywords):
    #             async for r in self._get_random_pig_image(event):
    #                 yield r

    def _is_trigger_keyword(self, message: str, keywords: List[str]) -> bool:
        """
        精确匹配（is_exact_match=True）：消息去空白后与关键词完全相等
        模糊匹配（is_exact_match=False）：消息包含关键词
        """
        msg = message.strip()
        if self.is_exact_match:
            return msg in keywords
        return any(kw in message for kw in keywords)