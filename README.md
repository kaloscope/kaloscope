<div align="center">

<img src="frontend/static/logo.svg" alt="logo" width="128">

# Kaloscope

_以可视化工作流驱动的本地媒体库管理工具_

[![GitHub Release](https://img.shields.io/github/v/release/kaloscope/kaloscope?label=Release&color=green)](https://github.com/kaloscope/kaloscope/releases)
[![GitHub Stars](https://img.shields.io/github/stars/kaloscope/kaloscope?logo=github&label=Stars&style=flat&color=yellow)](https://github.com/kaloscope/kaloscope/stargazers)
[![Docker Pulls](https://img.shields.io/docker/pulls/kaloscope/kaloscope?logo=docker&label=Docker%20Pulls&color=2496ED)](https://hub.docker.com/r/kaloscope/kaloscope)
[![GPLv3 License](https://img.shields.io/badge/License-GPLv3-BD0000)](LICENSE)
[![Telegram Group](https://img.shields.io/badge/Telegram-696969?logo=telegram)](https://t.me/kaloscope_official)

[![xyflow Version](https://img.shields.io/badge/xyflow-v1.6.0-1A192B?logo=xyflow)](https://xyflow.com/)
[![Svelte Version](https://img.shields.io/badge/Svelte-v5.56.2-FF3E00?logo=svelte)](https://svelte.dev/)
[![Sanic Version](https://img.shields.io/badge/Sanic-v25.12.1-FF0D68?logo=sanic)](https://sanic.dev/)
[![Python Version](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python)](https://www.python.org/)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/kaloscope/kaloscope)

| <img align="center" src="screenshots/workflow-complex.png" /> | <img align="center" src="screenshots/dashboard.png" /> |
| ------------------------------------------------------------- | ------------------------------------------------------ |

</div>

## 项目简介

Kaloscope 是一款基于可视化工作流引擎的本地媒体库管理工具。其资源搜索与元数据刮削等能力均由可编辑的工作流来驱动，可灵活对接任意资源站点与元数据来源。

## 功能特性

### :wrench: 工作流

- 提供基于节点的可视化工作流编辑器，拖拽即可搭建业务流程
- 内置 HTTP 请求、Python 脚本、条件分支、循环控制等节点类型
- 支持从 GitHub 仓库导入社区工作流模板，快速复用已有方案
- 支持定时触发，可按计划自动执行工作流

### :mag: 资源搜索

- 索引器完全由工作流驱动，可对接任意资源站点
- 支持关键词搜索、详情预览、登录认证等完整交互流程
- 支持全局搜索，可同时聚合多个索引器的结果

### :inbox_tray: 下载管理

- 支持 [aria2](https://aria2.github.io/)、[qBittorrent](https://www.qbittorrent.org/)、[Transmission](https://transmissionbt.com/) 等下载器
- 下载器配置通过 YAML 定义，可按需扩展适配器
- 支持下载计划，可按关键词和过滤规则自动抓取并下发下载任务
- 支持手动添加磁力链接或种子文件

### :clapper: 媒体库管理

- 支持电影、电视剧等多种媒体库类型
- 支持实时监控文件系统，自动识别新加入的媒体文件
- 支持从 [NFO](http://wikipedia.org/wiki/.nfo) 文件中提取并解析元数据

### :arrow_forward: 在线播放

- 内置视频播放器，支持 FLV、HLS、MP4 格式
- 支持实时转码播放，按需将视频转为 HLS 流，可按画质限制输出
- 支持多种硬件加速编码（NVENC、VAAPI、VideoToolbox 等）
- 支持弹幕显示与移动端样式全屏播放
- 支持记录播放进度和续播

### :busts_in_silhouette: 用户权限

- 支持多用户，并区分管理员与普通用户角色
- 可按媒体库和索引器分配访问权限
- 支持个人偏好设置与头像自定义

### :iphone: PWA 支持

- 支持以 [PWA](https://web.dev/explore/progressive-web-apps) 方式安装到桌面或移动设备
- PWA 主题颜色可随应用内主题同步切换

## 相关链接

- [官网和文档](https://kaloscope.org)
- [工作流模板仓库](https://github.com/kaloscope/workflows)
- [Docker Hub 镜像](https://hub.docker.com/r/kaloscope/kaloscope)
- [Telegram 社群](https://t.me/kaloscope_official)

## 贡献者

感谢所有为本项目提交代码、文档、反馈和建议的贡献者。

[![Contributors](https://contrib.rocks/image?repo=kaloscope/kaloscope)](https://github.com/kaloscope/kaloscope/graphs/contributors)

## 星标历史

[![Star History Chart](https://api.star-history.com/chart?repos=kaloscope/kaloscope&type=date&legend=top-left)](https://www.star-history.com/?repos=kaloscope%2Fkaloscope&type=date&legend=top-left)

## 特别鸣谢

- 弹弹play开放平台

  感谢[`弹弹play开放平台`](https://doc.dandanplay.com/open/)提供的弹幕服务接口支持。弹幕服务接口的相关代理实现见 [`kaloscope/danmaku`](https://github.com/kaloscope/danmaku) 仓库。

- 第三方依赖与开源社区

  本项目构建在众多优秀的开源项目之上，感谢所有开发者与贡献者的持续投入。完整的第三方依赖列表及对应开源协议见 [`LICENSES.md`](LICENSES.md) 文件。

## 免责声明

- 本项目仅供个人学习与技术交流使用，禁止用于商业目的或传播违法内容
- 社区或第三方工作流可能包含任意代码或网络请求，使用者需自行审查验证其安全性与合法性
- 因使用本项目引发的一切法律责任、风险与损失，均由使用者自行承担，开发者不承担任何连带责任

## 开源协议

本项目基于 [GPLv3](LICENSE) 开源协议发布。
