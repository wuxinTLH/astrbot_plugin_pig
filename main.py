import time
import asyncio
import json
import os
import random
import urllib.parse

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.io import download_image_by_url


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.0.1")
class PigRandomImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.cooldown_period = self.config.get("cooldown_period", 30)
        self.last_called_times = {}
        self.max_retries = 2
        self.pig_images = []
        
        self._load_pig_from_json()

    def _load_pig_from_json(self):
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
                
                base_url = "https://pighub.top/"
                unencoded_url = f"{base_url}{thumbnail}"
                parsed_url = urllib.parse.urlparse(unencoded_url)
                encoded_path = urllib.parse.quote(parsed_url.path)
                encoded_full_url = urllib.parse.urlunparse(
                    (parsed_url.scheme, parsed_url.netloc, encoded_path, "", "", "")
                )
                
                self.pig_images.append({
                    "title": img.get("title", "随机猪图"),
                    "full_url": encoded_full_url
                })
            
            logger.info(f"加载成功，共{len(self.pig_images)}张图片")
        
        except Exception as e:
            logger.error(f"加载list.json失败：{e}", exc_info=True)

    async def initialize(self):
        logger.info("猪图插件初始化完成，发送/pig获取图片")

    def _is_on_cooldown(self, command_name: str) -> tuple[bool, float]:
        current_time = time.time()
        last_called = self.last_called_times.get(command_name, 0)
        elapsed_time = current_time - last_called
        return elapsed_time < self.cooldown_period, max(0, self.cooldown_period - elapsed_time)

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        command_name = "pig"
        on_cooldown, remaining = self._is_on_cooldown(command_name)
        if on_cooldown:
            yield event.plain_result(f"冷却中～还需{remaining:.0f}秒")
            return
        
        if not self.pig_images:
            yield event.plain_result("无可用猪图数据")
            return
        
        selected_img = random.choice(self.pig_images)
        img_url = selected_img["full_url"]
        img_title = selected_img["title"]

        for attempt in range(self.max_retries):
            try:
                logger.info(f"尝试{attempt+1}/{self.max_retries}：下载{img_title}")
                temp_path = await download_image_by_url(img_url)
                yield event.image_result(temp_path)
                self.last_called_times[command_name] = time.time()
                return
            except Exception as e:
                error_msg = str(e)[:30]
                logger.error(f"下载失败：{error_msg}")
                if attempt >= self.max_retries - 1:
                    yield event.plain_result(f"获取{img_title}失败：{error_msg}...")
            await asyncio.sleep(2 ** attempt)
        self.last_called_times[command_name] = time.time()

    @filter.command("pig")
    async def pig_command(self, event: AstrMessageEvent):
        async for result in self._get_random_pig_image(event):
            yield result

    async def terminate(self):
        logger.info("猪图插件已卸载")