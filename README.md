# astrbot_plugin_pig

AstrBot框架插件，用于QQ官方机器人随机发送猪相关图片。

## 概述

`astrbot_plugin_pig` 是一款适用于AstrBot框架的插件，可让QQ官方机器人响应 `/pig` 指令，随机发送猪相关图片。插件从配置列表中读取图片资源，支持基础冷却机制以防止滥用。使用的库不需要额外安装。

## 仓库地址

[https://github.com/wuxinTLH/astrbot_plugin_pig](https://github.com/wuxinTLH/astrbot_plugin_pig)

## 兼容性

- **插件版本**：v0.0.6
- **适配AstrBot框架版本**：v4.1.4+（已验证可用）
- **支持平台**：QQ官方机器人（基于QQ Official Webhook适配器）  
  *其他平台（微信、Discord等）暂未测试*

## 图库来源

所有图片资源均来自 [pighub.top](http://pighub.top)。本插件仅负责随机选择和展示图片，不本地存储任何图片资源。后期版本考虑可以配置本地存储或网络缓存的设置。

## 安装步骤

1. 克隆或下载本仓库；
2. 将插件文件解压至AstrBot插件目录：  
   默认路径：`/AstrBot/data/plugins/astrbot_plugin_pig/`
   或直接在astrbot的插件中选择安装本地插件(使用.zip格式)
3. 确保插件目录包含两个核心文件：  
   - `main.py`（插件核心逻辑）
   - `list.json`（图片配置文件）
4. 在插件管理界面启用本插件。


