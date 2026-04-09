import os
import re
import sys
import time
import json
import random
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

import aiohttp
from aiohttp import ClientTimeout, TCPConnector, ClientError

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import AstrBotConfig, logger


# ─────────────────────────────────────────────
#  插件元数据
# ─────────────────────────────────────────────
@register(
    "astrbot_plugin_pig",
    "SakuraMikku",
    "猪猪表情包(随机发送/关键词触发/自动更新)",
    "0.1.1",
    "https://github.com/wuxinTLH/astrbot_plugin_pig",
)
class PigPlugin(Star):
    """猪猪表情包插件 - 优化版"""

    # ══════════════════════════════════════════
    #  常量定义
    # ══════════════════════════════════════════

    # 默认配置
    DEFAULT_CONFIG = {
        "cooldown_period": 5.0,          # 指令冷却时间(秒)
        "load_to_local": False,           # 是否缓存图片到本地
        "update_cycle": 0,                # 自动更新周期(天), 0=禁用
        "is_match_all_msg": False,        # 是否监听所有消息触发关键词
        "match_keywords": ["猪", "祝", "🐷", "🐖", "猪猪", "小猪"],
        "max_retries": 3,                 # 网络请求最大重试次数
        "request_timeout": 20,            # 请求超时时间(秒)
        "concurrent_limit": 4,            # 并发下载限制
        "image_cache_ttl": 7 * 24 * 3600, # 图片缓存有效期(秒)
    }

    # 图片数据源
    PIG_DATA_URL = "https://cdn.jsdelivr.net/gh/wuxinTLH/pig-images@main/list.json"
    PIG_BASE_URL = "https://cdn.jsdelivr.net/gh/wuxinTLH/pig-images@main/"

    # 反爬验证关键词
    VERIFY_KEYWORDS = frozenset({
        "automated bot check", "cloudflare", "just a moment",
        "checking your browser", "ddos-guard", "ray id",
        "机器人验证", "please enable javascript"
    })

    # 请求头
    BASE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
    }

    # ══════════════════════════════════════════
    #  初始化
    # ══════════════════════════════════════════

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)

        # ── 配置加载（带类型安全转换）──
        def _safe_get(cfg, key, default, cast_type):
            try:
                val = cfg.get(key, default)
                return cast_type(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        self.cooldown_period: float = _safe_get(config, "cooldown_period", 5.0, float)
        self.load_to_local: bool = _safe_get(config, "load_to_local", False, bool)
        self.update_cycle: int = _safe_get(config, "update_cycle", 0, int)
        self.is_match_all_msg: bool = _safe_get(config, "is_match_all_msg", False, bool)
        self.match_keywords: List[str] = config.get("match_keywords", self.DEFAULT_CONFIG["match_keywords"]) or []
        self.max_retries: int = min(_safe_get(config, "max_retries", 3, int), 5)
        self.request_timeout: int = _safe_get(config, "request_timeout", 20, int)
        self.concurrent_limit: int = _safe_get(config, "concurrent_limit", 4, int)
        self.image_cache_ttl: int = _safe_get(config, "image_cache_ttl", 7 * 24 * 3600, int)

        # ── 目录初始化 ──
        try:
            data_dir = Path(StarTools.get_data_dir("astrbot_plugin_pig"))
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            data_dir = Path(__file__).parent.resolve() / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

        self.base_dir = Path(__file__).parent.resolve()
        self.cache_dir = data_dir / "cache"
        self.img_dir = self.cache_dir / "images"
        self.list_file = self.cache_dir / "list.json"

        for p in (self.cache_dir, self.img_dir):
            try:
                p.mkdir(parents=True, exist_ok=True)
                if sys.platform.startswith("linux"):
                    os.chmod(str(p), 0o755)
            except Exception:
                logger.debug(f"[PIG] 目录创建跳过: {p}")

        # ── 内存状态 ──
        self.pig_images: List[Dict[str, str]] = []
        self._image_cache: Dict[str, Tuple[List[Dict], float]] = {"data": None, "expire": 0}
        self._cooldown: Dict[str, float] = {}  # identity -> last_call_time
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._download_semaphore: Optional[asyncio.Semaphore] = None
        self._update_task: Optional[asyncio.Task] = None
        self._cache_lock = asyncio.Lock()

    # ══════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════

    async def initialize(self):
        """插件初始化"""
        logger.info("[PIG] 猪猪表情包插件初始化")

        # HTTP Session
        ssl_ctx = False if self.config.get("insecure_skip_verify", False) else True
        conn = TCPConnector(
            ssl=ssl_ctx,
            limit=self.concurrent_limit * 2,
            limit_per_host=self.concurrent_limit,
            ttl_dns_cache=300,
        )
        timeout = ClientTimeout(total=self.request_timeout, connect=8, sock_read=15)
        self._http_session = aiohttp.ClientSession(
            connector=conn,
            timeout=timeout,
            headers=self.BASE_HEADERS,
        )
        self._download_semaphore = asyncio.Semaphore(self.concurrent_limit)

        # 加载图片列表
        await self._load_pig_list()

        # 启动定时更新任务（如果启用）
        if self.update_cycle > 0:
            self._update_task = asyncio.create_task(self._update_cycle_task())
            logger.info(f"[PIG] 自动更新已启用，周期: {self.update_cycle}天")

        # 启动缓存清理协程
        asyncio.create_task(self._cache_cleanup_loop())

    async def terminate(self):
        """插件卸载"""
        # 取消定时任务
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass

        # 关闭 HTTP Session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

        logger.info("[PIG] 猪猪表情包插件已卸载")

    # ══════════════════════════════════════════
    #  定时任务
    # ══════════════════════════════════════════

    async def _update_cycle_task(self):
        """定时更新图片列表任务 - 修复日期计算问题"""
        while True:
            try:
                # ✅ 使用 datetime 正确计算到次日零点的时间
                now = datetime.now()
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sleep_seconds = max(0, (tomorrow - now).total_seconds())
                
                logger.debug(f"[PIG] 距离下次更新还有 {sleep_seconds:.0f} 秒")
                await asyncio.sleep(sleep_seconds)

                # 执行更新
                logger.info("[PIG] 执行定时图片列表更新")
                await self._load_pig_list(force=True)

                # 如果配置了多日周期，额外等待
                if self.update_cycle > 1:
                    extra_sleep = (self.update_cycle - 1) * 86400
                    await asyncio.sleep(extra_sleep)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[PIG] 定时更新异常: {e}")
                await asyncio.sleep(300)  # 出错后等待5分钟重试

    async def _cache_cleanup_loop(self):
        """定期清理过期缓存图片"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时检查一次
                await asyncio.to_thread(self._evict_expired_images)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[PIG] 缓存清理异常: {e}")

    def _evict_expired_images(self):
        """清理磁盘上过期的缓存图片"""
        if not self.img_dir.exists():
            return
        now = time.time()
        cleaned = 0
        for img_file in self.img_dir.glob("*.jpg"):
            try:
                if now - img_file.stat().st_mtime > self.image_cache_ttl:
                    img_file.unlink(missing_ok=True)
                    cleaned += 1
            except Exception:
                pass
        if cleaned:
            logger.info(f"[PIG] 清理过期缓存图片 {cleaned} 张")

    # ══════════════════════════════════════════
    #  工具函数
    # ══════════════════════════════════════════

    def _get_identity(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识（用于冷却）"""
        user_id = getattr(event, "user_id", None) or \
                  getattr(getattr(event, "sender", None), "user_id", None) or \
                  getattr(getattr(event, "from_user", None), "id", None)
        
        group_id = getattr(event, "group_id", None) or \
                   getattr(getattr(event, "session", None), "group_id", None)
        
        if group_id:
            return f"group:{group_id}"
        return f"user:{user_id}" if user_id else f"temp:{int(time.time() // 600)}"

    def _is_on_cooldown(self, identity: str) -> Tuple[bool, int]:
        """检查冷却状态"""
        now = time.time()
        last = self._cooldown.get(identity, 0)
        remaining = self.cooldown_period - (now - last)
        if remaining > 0:
            return True, max(0, int(remaining) + 1)
        return False, 0

    def _clean_text(self, text: str) -> str:
        """清理消息文本 - 修复正则表达式"""
        if not isinstance(text, str):
            return ""
        # ✅ 修复: 正确匹配 <at>...</at> 标签
        text = re.sub(r"<at[^>]*>.*?</at>", "", text, flags=re.I | re.S)
        text = re.sub(r"\[At:[^\]]+\]", "", text)
        text = text.strip().lstrip("/\\／﹨")
        return re.sub(r"\s+", " ", text).strip()

    def _is_trigger_keyword(self, message: str) -> bool:
        """检查消息是否包含触发关键词 - 优化匹配效率"""
        # ✅ 使用 any() 更简洁高效
        return any(kw in message for kw in self.match_keywords)

    def _sanitize_filename(self, filename: str) -> str:
        """安全化文件名，防止路径遍历"""
        # 提取纯文件名
        name = Path(filename).name
        # 移除危险字符
        safe_name = re.sub(r'[\\/*?:"<>|]', "_", name)
        # 限制长度 + 添加哈希防止冲突
        if len(safe_name) > 100:
            name_part, ext = os.path.splitext(safe_name)
            safe_name = name_part[:80] + "_" + hashlib.md5(safe_name.encode()).hexdigest()[:16] + ext
        return safe_name

    def _get_local_image_path(self, url: str) -> Path:
        """根据 URL 生成安全的本地缓存路径 - 增强路径安全"""
        # ✅ 使用 pathlib.resolve() 防止路径遍历
        filename = self._sanitize_filename(url.split("/")[-1])
        local_path = (self.img_dir / filename).resolve()
        base_path = self.img_dir.resolve()
        
        # 双重校验路径安全
        if not str(local_path).startswith(str(base_path) + os.sep):
            logger.warning(f"[PIG] 路径遍历攻击尝试: {local_path}")
            return None
        return local_path

    # ══════════════════════════════════════════
    #  HTTP 请求
    # ══════════════════════════════════════════

    async def _fetch_json(self, url: str) -> Optional[List[Dict]]:
        """获取 JSON 数据"""
        for attempt in range(self.max_retries):
            try:
                async with self._http_session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            return data
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 10))
                        await asyncio.sleep(retry_after)
                        continue
            except asyncio.CancelledError:
                raise
            except (ClientError, json.JSONDecodeError) as e:
                logger.warning(f"[PIG] 请求失败 (尝试{attempt+1}): {e}")
            if attempt < self.max_retries - 1:
                await asyncio.sleep(min(2 ** attempt, 8))
        return None

    async def _download_image(self, url: str, local_path: Path) -> bool:
        """下载图片到本地 - 带并发控制"""
        async with self._download_semaphore:
            for attempt in range(self.max_retries):
                try:
                    async with self._http_session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        content = await resp.read()
                        if len(content) < 512:  # 最小图片大小校验
                            continue
                        
                        # ✅ 原子写入: 先写临时文件，再重命名
                        tmp_path = local_path.with_suffix(".tmp")
                        tmp_path.write_bytes(content)
                        tmp_path.replace(local_path)
                        
                        if sys.platform.startswith("linux"):
                            try:
                                os.chmod(str(local_path), 0o644)
                            except Exception:
                                pass
                        return True
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[PIG] 下载失败 (尝试{attempt+1}): {e}")
                await asyncio.sleep(min(2 ** attempt, 5))
            return False

    # ══════════════════════════════════════════
    #  图片列表管理
    # ══════════════════════════════════════════

    async def _load_pig_list(self, force: bool = False) -> bool:
        """加载/刷新图片列表 - 添加内存缓存"""
        async with self._cache_lock:
            # ✅ 内存缓存: 避免重复请求
            now = time.time()
            cache = self._image_cache
            if not force and cache["data"] and now < cache["expire"]:
                self.pig_images = cache["data"]
                return True

            # 尝试读取本地缓存文件
            if not force and self.list_file.exists():
                try:
                    data = json.loads(self.list_file.read_text(encoding="utf-8"))
                    if isinstance(data, list) and data:
                        self.pig_images = data
                        cache["data"] = data
                        cache["expire"] = now + 300  # 5分钟内存缓存
                        return True
                except Exception as e:
                    logger.warning(f"[PIG] 读取本地列表失败: {e}")

            # 从网络获取
            data = await self._fetch_json(self.PIG_DATA_URL)
            if data and isinstance(data, list):
                self.pig_images = data
                # ✅ 原子写入本地缓存
                try:
                    tmp = self.list_file.with_suffix(".tmp")
                    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    tmp.replace(self.list_file)
                except Exception as e:
                    logger.warning(f"[PIG] 保存本地列表失败: {e}")
                
                cache["data"] = data
                cache["expire"] = now + 300
                logger.info(f"[PIG] 图片列表已加载/更新，共 {len(data)} 张")
                return True
            
            logger.error("[PIG] 图片列表加载失败")
            return False

    # ══════════════════════════════════════════
    #  图片获取逻辑 - 统一缓存处理
    # ══════════════════════════════════════════

    async def _ensure_image_cached(self, img_info: Dict[str, str]) -> Optional[str]:
        """
        ✅ 统一缓存函数: 确保图片已缓存到本地
        返回: 本地文件路径 或 None
        """
        url = img_info.get("url") or img_info.get("image_url")
        if not url:
            return None

        # 如果未启用本地缓存，直接返回原链接
        if not self.load_to_local:
            return url

        local_path = self._get_local_image_path(url)
        if not local_path:
            return url

        # 本地已存在且有效
        if local_path.exists() and local_path.stat().st_size > 1024:
            return str(local_path)

        # 下载并缓存
        if await self._download_image(url, local_path):
            return str(local_path)
        
        # 下载失败，返回原链接兜底
        return url

    async def _get_random_pig_image(self) -> Optional[str]:
        """获取随机猪猪图片 - 修复随机去重逻辑"""
        if not self.pig_images:
            await self._load_pig_list()
        
        if not self.pig_images:
            return None

        # ✅ 使用 random.sample 确保不重复且简洁
        max_try = min(3, len(self.pig_images))
        candidates = random.sample(range(len(self.pig_images)), max_try)
        
        for idx in candidates:
            img_info = self.pig_images[idx]
            local_path = await self._ensure_image_cached(img_info)
            if local_path:
                return local_path
        
        return None

    # ══════════════════════════════════════════
    #  命令处理
    # ══════════════════════════════════════════

    async def _send_random_pig(self, event: AstrMessageEvent):
        """发送随机猪猪图片"""
        identity = self._get_identity(event)
        
        # 冷却检查
        on_cd, remain = self._is_on_cooldown(identity)
        if on_cd:
            yield event.plain_result(f"🐷 冷却中，请 {remain} 秒后再试~")
            return

        # 记录冷却时间
        self._cooldown[identity] = time.time()

        # 获取图片
        yield event.plain_result("🐷 正在挑选猪猪...")
        img_path = await self._get_random_pig_image()
        
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
        else:
            yield event.plain_result("😢 猪猪跑丢了，请稍后再试~")

    async def _handle_update_command(self, event: AstrMessageEvent, force: bool = False):
        """处理更新命令"""
        yield event.plain_result("🔄 正在更新猪猪列表...")
        success = await self._load_pig_list(force=True)
        if success:
            yield event.plain_result(f"✅ 更新成功！当前共 {len(self.pig_images)} 张猪猪~")
        else:
            yield event.plain_result("❌ 更新失败，请检查网络连接")

    # ══════════════════════════════════════════
    #  命令入口
    # ══════════════════════════════════════════

    @filter.command("pig")
    @filter.command("猪猪")
    async def pig_command(self, event: AstrMessageEvent, sub: str = None):
        """
        猪猪命令入口
        用法:
          /pig          - 随机发送猪猪
          /pig update   - 手动更新列表
          /更新         - 同上
        """
        # ✅ 简化子命令解析逻辑 - 合并为单个布尔表达式
        is_update = (sub and sub.lower() in ("update", "更新")) or \
                    (not sub and event.message_str.strip().lower() in ("/更新", "更新"))
        
        if is_update:
            async for r in self._handle_update_command(event):
                yield r
        else:
            async for r in self._send_random_pig(event):
                yield r

    @filter.command("更新")
    async def update_command(self, event: AstrMessageEvent):
        """别名: 更新猪猪列表"""
        async for r in self._handle_update_command(event, force=True):
            yield r

    # ══════════════════════════════════════════
    #  关键词触发（可选）
    # ══════════════════════════════════════════

    @filter.event_message_type("message")
    async def on_message(self, event: AstrMessageEvent):
        """监听消息触发关键词 - 仅在配置启用时生效"""
        if not self.is_match_all_msg:
            return
        
        message = self._clean_text(getattr(event, "message_str", "") or "")
        if not message or not self._is_trigger_keyword(message):
            return
        
        # 避免响应自己的消息或命令
        if message.startswith(("/", "／")):
            return
            
        # 发送猪猪（不占用命令冷却）
        img_path = await self._get_random_pig_image()
        if img_path and os.path.exists(img_path):
            await event.send(MessageChain().image(img_path))

    # ══════════════════════════════════════════
    #  帮助信息
    # ══════════════════════════════════════════

    @filter.command("pig help")
    @filter.command("猪猪帮助")
    async def help_command(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """🐷 猪猪表情包插件帮助
        【命令】
        /pig          - 随机发送一张猪猪
        /pig update   - 手动更新猪猪列表
        /更新         - 同上
        /pig help     - 显示此帮助

        【配置】
        cooldown_period: 指令冷却时间(秒)
        load_to_local: 是否缓存图片到本地
        update_cycle: 自动更新周期(天), 0=禁用
        is_match_all_msg: 是否监听关键词自动回复
        match_keywords: 触发关键词列表

        【说明】
        • 图片源: jsDelivr CDN
        • 支持本地缓存减少流量
        • 自动处理月末/年末日期计算
        
        作者: SakuraMikku
        仓库: https://github.com/wuxinTLH/astrbot_plugin_pig"""
        yield event.plain_result(help_text)