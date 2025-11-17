import time
import asyncio
import json
import os
import random
import shutil
import urllib.parse
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.core.utils.io import download_image_by_url


@register("astrbot_plugin_pig", "SakuraMikku", "随机发送猪相关图片", "0.0.7") 
class PigRandomImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        # 配置项（带类型转换和默认）
        try:
            self.cooldown_period = float(config.get("cooldown_period", 5))
        except Exception:
            self.cooldown_period = 5.0
        self.load_to_local = bool(config.get("load_to_local", False))
        try:
            self.max_retries = int(config.get("max_retries", 2))
        except Exception:
            self.max_retries = 2

        # 新增：更新周期（按天），0 表示不自动更新，1 表示每天零点更新
        try:
            self.update_cycle = int(config.get("update_cycle", 0))
            if self.update_cycle < 0:
                self.update_cycle = 0
        except Exception:
            self.update_cycle = 0

        self.last_called_times = {}
        self.pig_images = []
        # 使用 os.path 保持与原 import 一致
        base_dir = os.path.dirname(__file__)
        self.local_img_dir = os.path.join(base_dir, "imgs", "pig")
        self.json_path = os.path.join(base_dir, "list.json")  # 提取JSON路径为实例变量

        # 并发下载限制，避免资源耗尽
        self._download_semaphore = asyncio.Semaphore(3)

        # 后台调度任务句柄
        self._scheduler_task = None

        self._create_local_dir()
        # 初始化时先加载本地数据，后续在initialize中可能更新
        self._load_pig_from_json()

    def _create_local_dir(self):
        if not self.load_to_local:
            return
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            logger.info(f"本地图片目录初始化完成：{self.local_img_dir}")
        except OSError as e:
            self.load_to_local = False
            logger.error(f"创建图片目录失败：{str(e)}，已切换为仅网络加载")

    def _sanitize_filename(self, name: str, default: str = "image") -> str:
        """
        清理文件名，防止路径穿越与包含非法字符。
        """
        if not name:
            name = default
        # 去掉空字节
        name = name.replace("\x00", "")
        # 替换路径分隔符
        name = name.replace("/", "_").replace("\\", "_")
        allowed = set("-_.() ")
        cleaned_chars = []
        for ch in name:
            if ch.isalnum() or ch in allowed:
                cleaned_chars.append(ch)
                continue
            o = ord(ch)
            if 0x4e00 <= o <= 0x9fff:
                cleaned_chars.append(ch)
                continue
            cleaned_chars.append("_")
        cleaned = "".join(cleaned_chars).strip()
        MAX_LEN = 200
        if len(cleaned) > MAX_LEN:
            cleaned = cleaned[:MAX_LEN]
        if cleaned == "":
            cleaned = default
        return cleaned

    def _quote_path_preserving_slashes(self, path: str) -> str:
        """
        对 path 的每个 segment 单独进行 quote，保留 slash。
        """
        segments = path.split("/")
        quoted = [urllib.parse.quote(seg) for seg in segments]
        return "/".join(quoted)

    def _is_valid_url(self, url: str) -> bool:
        try:
            p = urllib.parse.urlparse(url)
            return p.scheme in ("http", "https") and bool(p.netloc)
        except Exception:
            return False

    def _load_pig_from_json(self):
        """加载图片配置，处理URL编码和文件名"""
        if not os.path.exists(self.json_path):
            logger.info("list.json 不存在，跳过本地加载")
            self.pig_images = []
            return

        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
        except Exception as e:
            logger.error(f"加载list.json失败（解析或读取错误）：{e}")
            self.pig_images = []
            return

        raw_images = json_data.get("images", []) if isinstance(json_data, dict) else []
        self.pig_images.clear()  # 清空现有数据
        for img in raw_images:
            if not isinstance(img, dict):
                continue
            thumbnail = img.get("thumbnail", "")
            if not thumbnail:
                logger.warning(f"跳过空thumbnail图片：{img.get('title', '未知')}")
                continue

            thumbnail = str(thumbnail).lstrip("/")

            # 处理为绝对 URL：若已经是完整 URL 则使用，否则拼接 base
            base_url = "https://pighub.top/"
            if self._is_valid_url(thumbnail):
                unencoded_url = thumbnail
            else:
                unencoded_url = urllib.parse.urljoin(base_url, thumbnail)

            try:
                parsed = urllib.parse.urlparse(unencoded_url)
                encoded_path = self._quote_path_preserving_slashes(parsed.path)
                encoded_full_url = urllib.parse.urlunparse(
                    (parsed.scheme, parsed.netloc, encoded_path, parsed.params, parsed.query, parsed.fragment)
                )
            except Exception:
                logger.debug(f"构建图片 URL 失败，跳过：{unencoded_url}")
                continue

            # 处理本地文件名（优先用list.json的filename）
            img_filename = img.get("filename")
            if img_filename:
                img_filename = self._sanitize_filename(str(img_filename))
            else:
                file_ext = os.path.splitext(thumbnail)[-1] or ".jpg"
                if not file_ext or "." not in file_ext:
                    file_ext = ".jpg"
                title_part = img.get("title", "未知图片")
                img_filename = self._sanitize_filename(f"{title_part}{file_ext}")

            valid_suffixes = (".jpg", ".jpeg", ".png", ".gif", ".bmp")
            if not img_filename.lower().endswith(valid_suffixes):
                img_filename = img_filename + ".jpg"

            self.pig_images.append({
                "title": img.get("title", "随机猪图"),
                "full_url": encoded_full_url,
                "filename": img_filename,
                "id": img.get("id")
            })

        logger.info(f"图片配置加载成功，共{len(self.pig_images)}张图片（v0.0.7，支持远程更新）")

    async def _fetch_remote_images(self):
        """从远程接口获取最新图片列表"""
        url = "https://pighub.top/api/images?limit=10000&sort=latest"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.error(f"远程请求失败，状态码：{response.status}")
                    return None
        except Exception as e:
            logger.error(f"远程请求异常：{str(e)}")
            return None

    def _apply_remote_data_if_needed(self, remote_data):
        """
        比较远程数据与本地 list.json，必要时原子写入并重新加载。
        返回 True 表示执行了更新，False 表示未更新或失败。
        """
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
            if not imgs or not isinstance(imgs, list):
                return set()
            return {item.get("id") for item in imgs if isinstance(item, dict) and "id" in item}

        local_ids = extract_ids(local_data)
        remote_ids = extract_ids(remote_data)

        need_update = False
        if not local_data:
            need_update = True
        else:
            if local_ids != remote_ids or len(local_data.get("images", [])) != len(remote_data.get("images", [])):
                need_update = True

        if not need_update:
            return False

        # 原子写入：写临时文件后 replace
        tmp_path = f"{self.json_path}.tmp_{int(time.time())}_{random.randint(0, 10**9)}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(remote_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.json_path)
            logger.info("本地list.json已更新（远程变化），已保存到 %s", self.json_path)
            # 重新加载内存数据
            self._load_pig_from_json()
            return True
        except Exception as e:
            logger.error("更新本地 list.json 失败（已记录）。")
            logger.debug("更新 list.json 失败详情：%s", e)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

    async def initialize(self):
        """
        初始化：尝试一次性从远程获取并更新本地 list.json（如果需要）。
        同时根据 update_cycle 启动后台调度器（若配置 > 0）。
        """
        remote_data = await self._fetch_remote_images()
        if remote_data:
            try:
                updated = self._apply_remote_data_if_needed(remote_data)
                if updated:
                    logger.info("初始化时已从远程更新本地列表")
                else:
                    logger.info("本地list.json与远程数据一致，无需更新（初始化检查）")
            except Exception as e:
                logger.error(f"处理远程数据时出错：{str(e)}")

        # 启动调度器（仅当 update_cycle > 0）
        if self.update_cycle and self.update_cycle > 0:
            # 若已有任务，先取消（防止重复）
            if self._scheduler_task and not self._scheduler_task.done():
                try:
                    self._scheduler_task.cancel()
                except Exception:
                    pass
            # 创建后台任务，但不 await，运行独立循环
            self._scheduler_task = asyncio.create_task(self._update_cycle_task())
            logger.info("已启动 list.json 后台更新调度器（周期：%d 天）", self.update_cycle)
        else:
            logger.info("未启用 list.json 后台自动更新（update_cycle=0）")

        logger.info("猪图插件（v0.0.7，支持远程更新与定时检查）初始化完成，发送/pig获取图片")
        logger.info(f"当前配置：冷却时间{self.cooldown_period}秒 | 本地加载{self.load_to_local} | 更新周期{self.update_cycle}天")

    def _is_on_cooldown(self, command_name: str) -> tuple[bool, float]:
        current_time = time.time()
        last_called = self.last_called_times.get(command_name, 0)
        elapsed_time = current_time - last_called
        return elapsed_time < self.cooldown_period, max(0, self.cooldown_period - elapsed_time)

    def _is_valid_image_suffix(self, filename: str) -> bool:
        valid_suffixes = (".jpg", ".jpeg", ".png", ".gif", ".bmp")
        return filename.lower().endswith(valid_suffixes)

    async def _get_local_image(self, selected_img):
        img_filename = selected_img.get("filename")
        if not img_filename:
            return None
        local_img_path = os.path.join(self.local_img_dir, img_filename)

        # 防止路径穿越：确保解析后的绝对路径以本地图片目录为前缀
        try:
            local_abs = os.path.abspath(local_img_path)
            base_abs = os.path.abspath(self.local_img_dir)
            if not (local_abs == base_abs or local_abs.startswith(base_abs + os.sep)):
                logger.warning("检测到可疑的本地路径，拒绝访问：%s", local_img_path)
                return None
        except Exception:
            logger.warning("本地路径解析失败，跳过本地加载：%s", local_img_path)
            return None

        if os.path.exists(local_abs) and self._is_valid_image_suffix(local_abs):
            logger.info(f"使用本地图片：{img_filename}")
            return local_abs

        logger.info(f"本地图片缺失，开始下载：{img_filename}")
        url = selected_img.get("full_url", "")
        if not self._is_valid_url(url):
            logger.warning("图片 URL 无效，无法下载：%s", url)
            return None

        # 并发限制
        async with self._download_semaphore:
            try:
                temp_path = await download_image_by_url(url)
            except Exception as e:
                logger.error(f"调用 download_image_by_url 出错：{e}")
                temp_path = None

        if not temp_path:
            logger.error(f"网络下载失败，无法获取{img_filename}")
            return None

        temp_filename = os.path.basename(temp_path)
        if not self._is_valid_image_suffix(temp_filename):
            try:
                os.remove(temp_path)
            except Exception:
                pass
            logger.error(f"下载文件非图片格式，已清理：{temp_filename}")
            return None

        # 尝试将临时文件复制到目标路径（原子替换）
        try:
            os.makedirs(self.local_img_dir, exist_ok=True)
            tmp_dest = os.path.join(self.local_img_dir, f".tmp_{int(time.time())}_{random.randint(0,10**9)}")
            shutil.copy2(temp_path, tmp_dest)
            os.replace(tmp_dest, local_abs)
            logger.info(f"图片保存到本地：{local_abs}")
            return local_abs
        except Exception as e:
            logger.error(f"保存本地失败：{str(e)}，将使用临时文件")
            if os.path.exists(temp_path):
                return temp_path
            return None

    async def _download_with_retries(self, url: str, title: str):
        """
        基于 download_image_by_url 的重试封装，带指数退避与随机抖动。
        返回临时文件路径或 None。
        """
        if not self._is_valid_url(url):
            logger.warning("尝试下载无效URL：%s", url)
            return None

        attempt = 0
        while attempt < max(1, self.max_retries):
            attempt += 1
            async with self._download_semaphore:
                try:
                    logger.info(f"网络加载尝试{attempt}/{self.max_retries}：{title}")
                    temp_path = await download_image_by_url(url)
                    if not temp_path:
                        raise RuntimeError("download returned None")
                    temp_filename = os.path.basename(temp_path)
                    if not self._is_valid_image_suffix(temp_filename):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                        raise RuntimeError("非图片格式")
                    return temp_path
                except Exception as e:
                    short_err = str(e)[:120]
                    logger.debug(f"下载尝试失败（{attempt}/{self.max_retries}）：{short_err}")
                    if attempt >= max(1, self.max_retries):
                        logger.error(f"获取{title}失败：{short_err}")
                        return None
                    backoff = (2 ** attempt) + random.random()
                    await asyncio.sleep(backoff)
        return None

    async def _get_random_pig_image(self, event: AstrMessageEvent):
        command_name = "pig"
        on_cooldown, remaining = self._is_on_cooldown(command_name)
        if on_cooldown:
            yield event.plain_result(f"冷却中～还需{remaining:.0f}秒")
            return

        if not self.pig_images:
            yield event.plain_result("无可用猪图数据")
            return

        # 随机选择图片并尝试返回（允许多次候选尝试）
        tried = set()
        max_candidates = min(len(self.pig_images), 3)
        for _ in range(max_candidates):
            idx = random.randrange(len(self.pig_images))
            if idx in tried and len(tried) < len(self.pig_images):
                continue
            tried.add(idx)
            selected_img = self.pig_images[idx]
            img_title = selected_img.get("title", "随机猪图")

            # 优先本地
            if self.load_to_local:
                try:
                    img_path = await self._get_local_image(selected_img)
                    if img_path:
                        yield event.image_result(img_path)
                        self.last_called_times[command_name] = time.time()
                        return
                    logger.debug("本地加载失败，切换为网络加载")
                except Exception as e:
                    logger.error(f"本地加载出错：{str(e)}，切换为网络加载")

            # 网络下载尝试
            temp_path = await self._download_with_retries(selected_img.get("full_url", ""), img_title)
            if temp_path:
                yield event.image_result(temp_path)
                self.last_called_times[command_name] = time.time()
                # 尝试异步保存到本地（不阻塞主流程）
                if self.load_to_local:
                    asyncio.create_task(self._save_to_local_cache_async(temp_path, selected_img.get("filename")))
                return
            else:
                continue

        # 所有候选都失败
        yield event.plain_result("获取猪图失败，请稍后重试")
        self.last_called_times[command_name] = time.time()

    async def _save_to_local_cache_async(self, downloaded_path: str, target_filename: str):
        """
        后台尝试将已下载临时文件保存为本地缓存（best-effort）。
        """
        if not downloaded_path:
            return
        try:
            if not target_filename:
                target_filename = os.path.basename(downloaded_path)
            safe_name = self._sanitize_filename(str(target_filename))
            os.makedirs(self.local_img_dir, exist_ok=True)
            dest_path = os.path.join(self.local_img_dir, safe_name)
            tmp_dest = os.path.join(self.local_img_dir, f".tmp_{int(time.time())}_{random.randint(0, 10**9)}")
            shutil.copy2(downloaded_path, tmp_dest)
            os.replace(tmp_dest, dest_path)
            logger.info("后台缓存图片至本地：%s", dest_path)
        except Exception as e:
            logger.debug("后台保存本地缓存失败：%s", e)
            try:
                if 'tmp_dest' in locals() and os.path.exists(tmp_dest):
                    os.remove(tmp_dest)
            except Exception:
                pass

    async def _update_cycle_task(self):
        """
        后台调度循环：在“每天零点”触发首次检查，之后根据 self.update_cycle 每隔 N 天执行一次检查。
        使用本地时间的零点判定（与系统时区一致）。
        若任务被取消，会干净退出。
        """
        try:
            # 若 update_cycle <= 0，则不应进入此函数，但仍作防御检查
            if not self.update_cycle or self.update_cycle <= 0:
                return

            while True:
                # 计算距离下一个本地零点的秒数
                now = time.time()
                lt = time.localtime(now)
                try:
                    # 构造次日 0:0:0 的时间戳
                    next_midnight_tuple = (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)
                    next_midnight = time.mktime(next_midnight_tuple)
                    sleep_seconds = max(0, int(next_midnight - now) + 1)  # +1 秒确保跨过零点
                except Exception:
                    # 在极少数情况下，直接退回到 60 秒后重试
                    sleep_seconds = 60

                logger.info("后台更新调度：等待 %s 秒 到下一个本地零点以执行更新检查", sleep_seconds)
                # 等待到下一个零点或被取消
                try:
                    await asyncio.sleep(sleep_seconds)
                except asyncio.CancelledError:
                    logger.info("后台更新调度已被取消（等待零点期间）")
                    break

                # 到达零点，执行一次更新检查
                try:
                    logger.info("后台更新调度：开始在零点检查远程列表更新")
                    remote_data = await self._fetch_remote_images()
                    if remote_data:
                        updated = self._apply_remote_data_if_needed(remote_data)
                        if updated:
                            logger.info("后台更新调度：检测到远程变化并已更新本地 list.json")
                        else:
                            logger.info("后台更新调度：远程数据与本地一致，未做更新")
                    else:
                        logger.warning("后台更新调度：未能获取远程数据，跳过本次更新")
                except asyncio.CancelledError:
                    logger.info("后台更新调度已被取消（执行更新期间）")
                    break
                except Exception as e:
                    logger.error("后台更新调度在执行更新时发生错误（已记录）")
                    logger.debug("后台更新出错详情：%s", e)

                # 若 update_cycle > 1，则在完成零点更新后，等待 (update_cycle - 1) 天再到下一个零点
                if self.update_cycle <= 1:
                    # 直接循环，下一次又等待到下一个零点
                    continue
                else:
                    # 计算额外天数的秒数（整天）
                    extra_days = self.update_cycle - 1
                    extra_seconds = extra_days * 24 * 3600
                    logger.info("后台更新调度：按周期等待额外 %d 天 (%d 秒) 再次检查", extra_days, extra_seconds)
                    try:
                        await asyncio.sleep(extra_seconds)
                    except asyncio.CancelledError:
                        logger.info("后台更新调度已被取消（周期等待期间）")
                        break
        finally:
            logger.info("后台更新调度任务退出")

    @filter.command("pig")
    async def pig_command(self, event: AstrMessageEvent):
        """/pig 随机发送一张猪猪表情包"""
        async for result in self._get_random_pig_image(event):
            yield result

    async def terminate(self):
        # 取消后台调度任务（若存在）
        if self._scheduler_task:
            try:
                self._scheduler_task.cancel()
                # 等待任务结束（短超时）
                try:
                    await asyncio.wait_for(self._scheduler_task, timeout=5)
                except asyncio.TimeoutError:
                    logger.debug("等待后台调度任务退出超时，任务可能仍在终止中")
                except asyncio.CancelledError:
                    pass
            except Exception as e:
                logger.debug("取消后台调度任务时发生错误：%s", e)
            finally:
                self._scheduler_task = None

        logger.info("猪图插件（v0.0.7，支持远程更新与定时检查）已卸载")