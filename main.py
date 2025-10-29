import time
import asyncio
import json
import os
import random
import shutil
import urllib.parse

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.io import download_image_by_url


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.0.3")
class PigRandomImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        # 修复：使用参数config直接获取配置，而非self.config
        self.cooldown_period = config.get("cooldown_period", 5)  # 原self.config.get改为config.get
        self.load_to_local = config.get("load_to_local", False)  # 同步修改此处，保持一致性
        self.last_called_times = {}
        self.max_retries = 2
        self.pig_images = []
        
        # 本地图片存储目录
        self.local_img_dir = os.path.join(os.path.dirname(__file__), "imgs", "pig")
        
        # 初始化：加载配置→创建目录
        self._load_pig_from_json()
        self._create_local_dir()

    def _create_local_dir(self):
        """创建本地目录，失败则关闭本地加载"""
        if not self.load_to_local:
            return
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            logger.info(f"本地图片目录初始化完成：{self.local_img_dir}")
        except OSError as e:
            self.load_to_local = False
            logger.error(f"手动创建./imgs/pig目录！创建失败：{str(e)}，已切换为仅网络加载")

    def _load_pig_from_json(self):
        """加载图片配置，处理URL编码和文件名"""
        json_path = os.path.join(os.path.dirname(__file__), "list.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
            raw_images = json_data.get("images", [])
            
            for img in raw_images:
                thumbnail = img.get("thumbnail", "").lstrip("/")
                if not thumbnail:
                    logger.warning(f"跳过空thumbnail图片：{img.get('title', '未知')}")
                    continue
                
                # URL编码（处理中文路径）
                base_url = "https://pighub.top/"
                unencoded_url = f"{base_url}{thumbnail}"
                parsed_url = urllib.parse.urlparse(unencoded_url)
                encoded_path = urllib.parse.quote(parsed_url.path)
                encoded_full_url = urllib.parse.urlunparse(
                    (parsed_url.scheme, parsed_url.netloc, encoded_path, "", "", "")
                )
                
                # 处理本地文件名（优先用list.json的filename）
                img_filename = img.get("filename")
                if not img_filename:
                    file_ext = os.path.splitext(thumbnail)[-1] or ".jpg"
                    img_filename = f"{img.get('title', '未知图片')}{file_ext}"
                
                self.pig_images.append({
                    "title": img.get("title", "随机猪图"),
                    "full_url": encoded_full_url,
                    "filename": img_filename
                })
            
            logger.info(f"图片配置加载成功，共{len(self.pig_images)}张图片（v0.0.3，无PIL依赖）")
        
        except Exception as e:
            logger.error(f"加载list.json失败：{e}", exc_info=True)

    async def initialize(self):
        logger.info("猪图插件（v0.0.3，无PIL依赖）初始化完成，发送/pig获取图片")
        logger.info(f"当前配置：冷却时间{self.cooldown_period}秒 | 本地加载{self.load_to_local}")

    def _is_on_cooldown(self, command_name: str) -> tuple[bool, float]:
        """冷却时间检查"""
        current_time = time.time()
        last_called = self.last_called_times.get(command_name, 0)
        elapsed_time = current_time - last_called
        return elapsed_time < self.cooldown_period, max(0, self.cooldown_period - elapsed_time)

    def _is_valid_image_suffix(self, filename: str) -> bool:
        """通过文件后缀判断是否为图片（支持常见格式）"""
        valid_suffixes = (".jpg", ".jpeg", ".png", ".gif", ".bmp")
        return filename.lower().endswith(valid_suffixes)

    async def _get_local_image(self, selected_img):
        """本地优先逻辑（无PIL依赖，用后缀判断文件类型）"""
        img_filename = selected_img["filename"]
        local_img_path = os.path.join(self.local_img_dir, img_filename)
        
        # 1. 本地存在且后缀符合图片格式：直接使用
        if os.path.exists(local_img_path) and self._is_valid_image_suffix(img_filename):
            logger.info(f"使用本地图片：{img_filename}")
            return local_img_path
        
        # 2. 本地无/后缀无效：下载并保存
        logger.info(f"本地图片缺失/后缀无效，开始下载：{img_filename}")
        temp_path = await download_image_by_url(selected_img["full_url"])
        if not temp_path:
            logger.error(f"网络下载失败，无法获取{img_filename}")
            return None
        
        # 检查临时文件后缀（避免保存非图片文件）
        temp_filename = os.path.basename(temp_path)
        if not self._is_valid_image_suffix(temp_filename):
            os.remove(temp_path)
            logger.error(f"下载的{img_filename}后缀非图片格式，已清理")
            return None
        
        # 保存到本地
        try:
            shutil.copy2(temp_path, local_img_path)
            logger.info(f"图片保存到本地：{local_img_path}")
            return local_img_path
        except Exception as e:
            logger.error(f"保存本地失败：{str(e)}，将使用临时文件")
            return temp_path

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        command_name = "pig"
        # 冷却检查
        on_cooldown, remaining = self._is_on_cooldown(command_name)
        if on_cooldown:
            yield event.plain_result(f"冷却中～还需{remaining:.0f}秒")
            return
        
        if not self.pig_images:
            yield event.plain_result("无可用猪图数据")
            return
        
        selected_img = random.choice(self.pig_images)
        img_title = selected_img["title"]

        # 本地加载逻辑（无PIL依赖）
        if self.load_to_local:
            try:
                img_path = await self._get_local_image(selected_img)
                if img_path:
                    yield event.image_result(img_path)
                    self.last_called_times[command_name] = time.time()
                    return
                logger.warning("本地加载逻辑异常，切换为网络加载")
            except Exception as e:
                logger.error(f"本地加载出错：{str(e)}，切换为网络加载")
        
        # 网络加载逻辑（无PIL依赖）
        for attempt in range(self.max_retries):
            try:
                logger.info(f"网络加载尝试{attempt+1}/{self.max_retries}：{img_title}")
                temp_path = await download_image_by_url(selected_img["full_url"])
                
                # 检查临时文件后缀
                temp_filename = os.path.basename(temp_path)
                if not self._is_valid_image_suffix(temp_filename):
                    os.remove(temp_path)
                    if attempt >= self.max_retries - 1:
                        yield event.plain_result(f"获取{img_title}失败：非图片格式")
                    continue
                
                yield event.image_result(temp_path)
                self.last_called_times[command_name] = time.time()
                return
            except Exception as e:
                error_msg = str(e)[:30]
                logger.error(f"网络加载失败：{error_msg}")
                if attempt >= self.max_retries - 1:
                    yield event.plain_result(f"获取{img_title}失败：{error_msg}...")
            await asyncio.sleep(2 **attempt)
        self.last_called_times[command_name] = time.time()

    @filter.command("pig")
    async def pig_command(self, event: AstrMessageEvent):
        async for result in self._get_random_pig_image(event):
            yield result

    async def terminate(self):
        logger.info("猪图插件（v0.0.3，无PIL依赖）已卸载")