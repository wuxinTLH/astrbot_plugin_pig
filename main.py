import logging
import time
import traceback
import asyncio
import json
import os
import random
import ssl
import urllib.parse
import httpx
import certifi
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.core.utils.io import download_image_by_url


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.0.1")
class PigRandomImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.logger = logging.getLogger(__name__)
        self.config = config

        # 基础配置
        self.cooldown_period = self.config.get("cooldown_period", 30)
        self.last_called_times = {}
        self.max_retries = 2
        self.pig_images = []  # 存储加载后的图片数据

        # 读取list.json，处理中文编码和路径问题
        self._load_pig_from_json()

    def _load_pig_from_json(self):
        json_path = os.path.join(os.path.dirname(__file__), "list.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
            raw_images = json_data.get("images", [])

            for img in raw_images:
                # 处理thumbnail路径
                thumbnail = img.get("thumbnail", "").lstrip("/")
                if not thumbnail:
                    self.logger.warning(f"跳过空thumbnail的图片：{img.get('title', '未知图片')}")
                    continue

                # 处理中文URL编码
                base_url = "https://pighub.top/"
                unencoded_url = f"{base_url}{thumbnail}"
                parsed_url = urllib.parse.urlparse(unencoded_url)
                encoded_path = urllib.parse.quote(parsed_url.path)
                encoded_full_url = urllib.parse.urlunparse(
                    (parsed_url.scheme, parsed_url.netloc, encoded_path, "", "", "")
                )

                # 打印编码后的URL到控制台(用于debug)
                # self.logger.info(f"编码后URL：{encoded_full_url}（对应图片：{img.get('title')}）")

                self.pig_images.append({
                    "title": img.get("title", "随机猪图"),
                    "full_url": encoded_full_url,
                    "id": img.get("id", ""),
                    "filename": img.get("filename", "")
                })

            self.logger.info(f"读取list.json成功，共加载{len(self.pig_images)}张图片（已处理中文编码）")

        except FileNotFoundError:
            self.logger.error(f"未找到list.json，路径：{json_path}")
        except json.JSONDecodeError:
            self.logger.error(f"list.json格式错误，请检查JSON语法")
        except Exception as e:
            self.logger.error(f"加载list.json失败：{e}", exc_info=True)

    async def initialize(self):
        self.logger.info("猪图插件初始化完成，发送/pig获取随机猪图")

    def _is_on_cooldown(self, command_name: str) -> tuple[bool, float]:
        current_time = time.time()
        last_called = self.last_called_times.get(command_name, 0)
        elapsed_time = current_time - last_called
        is_cooling = elapsed_time < self.cooldown_period
        remaining_time = max(0, self.cooldown_period - elapsed_time)
        return is_cooling, remaining_time

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        command_name = "pig"
        # 冷却检查
        on_cooldown, remaining_time = self._is_on_cooldown(command_name)
        if on_cooldown:
            yield event.plain_result(f"冷却中～还需{remaining_time:.0f}秒获取猪图")
            return

        # 检查图片数据
        if not self.pig_images:
            yield event.plain_result("无可用猪图数据，请检查list.json")
            return

        # 随机选图
        selected_img = random.choice(self.pig_images)
        img_url = selected_img["full_url"]
        img_title = selected_img["title"]

        # 在消息中返回编码后的URL(用于控制台信息)
        # yield event.plain_result(f"正在获取[{img_title}]，编码后URL：{img_url}")

        # 下载+发送图片（修复参数错误）
        for attempt in range(self.max_retries):
            try:
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                self.logger.info(f"尝试{attempt + 1}/{self.max_retries}：下载{img_title}，URL：{img_url}")
                temp_path = await download_image_by_url(img_url)
                yield event.image_result(temp_path)  # 仅发送图片
                self.last_called_times[command_name] = time.time()
                return

            except TypeError as e:
                # 专门捕获参数错误（如image_result不支持caption）
                self.logger.error(f"发送图片参数错误：{e}（可能不支持caption参数）")
                # 只发送图片，不附带标题
                if temp_path:
                    yield event.image_result(temp_path)
                    self.last_called_times[command_name] = time.time()
                    return
            except Exception as e:
                error_msg = str(e)[:50]
                self.logger.error(f"下载{img_title}失败：{error_msg}")
                if attempt >= self.max_retries - 1:
                    yield event.plain_result(f"获取{img_title}失败：{error_msg}...")
            await asyncio.sleep(2 ** attempt)
        self.last_called_times[command_name] = time.time()

    @filter.command("pig")
    async def pig_command(self, event: AstrMessageEvent):
        async for result in self._get_random_pig_image(event):
            yield result

    async def terminate(self):
        self.logger.info("猪图插件已卸载")
