# ETS100_Fucker

ETS100 的 Python 命令行实现，支持登录 ETS100 账号、获取作业列表、批量抓取答案、导出 JSON，并按作业渲染可读答案图片等功能。

> 注意：云端登录可能会顶掉 ETS100 官方客户端的登录状态。不要在考试、录音、提交作业等过程中运行本工具。

## 功能

- 登录 ETS100 账号并保存本地登录状态
- 获取当前、历史、过期作业列表
- 下载并解析 ETS100 作业资源包
- 支持单个作业或全部作业批量抓取
- 输出结构化答案 JSON
- 每个考试单独渲染一张答案 PNG
- 适配单词朗读、语篇朗读、语篇听读、角色扮演、故事复述等题型
- 支持从已有 JSON 离线重新生成答案图片

## 环境

建议使用 Python 3.10 或更高版本。

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

依赖包括：

- `pyzipper`：解压 ETS100 加密资源包
- `Pillow`：渲染答案图片

## 快速开始

登录账号：

```powershell
python ets100_cloud.py login --phone <手机号> --password <密码>
```

查看当前作业：

```powershell
python ets100_cloud.py list
```

抓取全部当前作业答案，并生成图片：

```powershell
python ets100_cloud.py fetch-all --images
```

未指定 `--out` 时，默认输出到：

```text
results/YYYY-MM-DD_all_answers.json
```

答案图片默认输出到 JSON 同目录下的 `images` 文件夹；也可以指定目录：

```powershell
python ets100_cloud.py fetch-all --images --image-dir results\answer_images
```

## 常用命令

查看历史作业：

```powershell
python ets100_cloud.py list --status 2
```

查看过期作业：

```powershell
python ets100_cloud.py list --status 3
```

抓取指定作业：

```powershell
python ets100_cloud.py fetch --homework-index 0 --out answers.json
```

抓取全部作业：

```powershell
python ets100_cloud.py fetch-all
```

从已有 JSON 重新生成图片：

```powershell
python ets100_cloud.py render-images results\YYYY-MM-DD_all_answers.json --image-dir results\answer_images
```

解析本地 `content.json`：

```powershell
python ets100_cloud.py parse-local path\to\content.json --group-name 题型名称 --out parsed.json
```

如果 CDN 证书校验失败，并且你接受风险，可以加：

```powershell
--insecure-cdn
```

## 输出说明

JSON 输出包含作业标题、题型、题目、答案、原文等结构化字段。

图片输出会尽量保留适合阅读的内容：

- 每个考试生成一张 PNG
- 普通题型输出题型、题目和第一个答案
- `单词朗读` 使用 `original_text`，并处理已知音标
- `语篇朗读` 输出原文
- `语篇听读` 会跳过听力对话稿，并将答案文本第一行作为题目
- `角色扮演` 合并为一个题型块，多题按序号排列
- `故事复述` 保留文章标题，只取第一个答案


## 本地状态

脚本默认在当前目录下创建：

```text
.ets100_cloud/
```

该目录会保存：

- 登录 token
- ETS100 设备码
- 作业资源 ZIP 缓存
- 解压后的资源文件

> 每个独立的 ETS100 设备码只能获取一次，请妥善保管。

## 文档

更详细的使用说明见：

[ets100_cloud_usage.md](ets100_cloud_usage.md)

## 免责声明

本项目仅供学习研究及授权账号范围内使用。请勿用于未授权访问、绕过平台限制、传播受版权保护内容或其他违规用途。使用本工具造成的账号、数据或法律风险由使用者自行承担。
