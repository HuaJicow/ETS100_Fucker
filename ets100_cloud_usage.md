# ETS100 云端模式 Python 实现使用文档

本文档配套 `ets100_cloud.py` 使用。脚本实现了 Fuck_ets100 项目云端模式的核心流程：登录 ETS100 账号、获取作业列表、下载作业资源包、解压并解析答案。

> 注意：云端登录可能会顶掉 ETS100 官方客户端的登录状态。不要在考试、录音、提交作业等过程中运行本工具。

## 1. 文件说明

- `ets100_cloud.py`：主程序，单文件 Python 实现。
- `requirements.txt`：依赖列表，目前只需要 `pyzipper`，用于解压加密 ZIP。

默认状态与缓存目录位于脚本旁边的：

```text
.ets100_cloud/
```

其中会保存设备码、token、缓存 ZIP 和解压后的资源。除非你明确使用 `--save-password`，脚本不会保存明文密码。

## 2. 环境准备

建议使用 Python 3.10 或更高版本。

在 `outputs` 目录下安装依赖：

```powershell
python -m pip install -r requirements.txt
```

## 3. 登录账号

```powershell
python ets100_cloud.py login --phone 你的手机号 --password 你的密码
```

登录成功后，脚本会：

- 生成或读取稳定的 `device_code`
- 调用 `/user/login`
- 如果返回 `code=30014`，自动调用 `/user/rebind-code` 绑定设备
- 调用 `/m/ecard/list` 获取父账号 ID
- 保存 token、父账号 ID、设备码等状态

如果你希望保存密码用于自己的自动化流程，可以显式加上：

```powershell
python ets100_cloud.py login --phone 你的手机号 --password 你的密码 --save-password
```

## 4. 查看作业列表

当前作业：

```powershell
python ets100_cloud.py list
```

历史作业：

```powershell
python ets100_cloud.py list --status 2
```

过期作业：

```powershell
python ets100_cloud.py list --status 3
```

输出示例：

```text
[0] Unit 3 综合练习 (4 resources)
    - 听选信息: /resource/xxx.zip
    - 信息转述: /resource/yyy.zip
```

记住左侧的作业序号，后续下载解析会用到。

## 5. 下载并解析某个作业

下载并解析第 0 个作业：

```powershell
python ets100_cloud.py fetch --homework-index 0 --out answers.json
```

如果 CDN 证书校验失败，可在确认风险后加：

```powershell
python ets100_cloud.py fetch --homework-index 0 --out answers.json --insecure-cdn
```

输出文件是 JSON，结构大致为：

```json
{
  "title": "作业名称",
  "sections": [
    {
      "caption": "题型名称",
      "structure_type": "collector.picture",
      "questions": [
        {
          "question_text": "题目",
          "answers": ["答案1", "答案2"]
        }
      ]
    }
  ]
}
```

## 6. 一键下载并解析全部作业

下载并解析当前作业列表里的全部作业：

```powershell
python ets100_cloud.py fetch-all
```

未指定 `--out` 时，默认输出到当前运行目录的 `results` 文件夹，文件名格式为：

```text
YYYY-MM-DD_all_answers.json
```

如果 CDN 证书校验失败：

```powershell
python ets100_cloud.py fetch-all --insecure-cdn
```

默认情况下，某个作业下载或解析失败会停止，并且不会生成输出文件。想跳过失败项并继续后面的作业，同时保存包含 `errors` 字段的部分结果：

```powershell
python ets100_cloud.py fetch-all --continue-on-error
```

历史作业或过期作业也可以批量拉取：

```powershell
python ets100_cloud.py fetch-all --status 2
python ets100_cloud.py fetch-all --status 3
```

输出 JSON 包含：

- `papers`：成功解析的作业答案
- `errors`：失败的作业序号、名称和错误信息
- `fetched_count` / `error_count`：成功与失败数量

## 7. 输出答案图片

抓取单个作业并同时生成答案图片：

```powershell
python ets100_cloud.py fetch --homework-index 0 --images
```

抓取全部作业并为每个作业生成一张答案图片：

```powershell
python ets100_cloud.py fetch-all --images
```

图片默认输出到 JSON 同目录下的 `images` 文件夹。也可以指定图片目录：

```powershell
python ets100_cloud.py fetch-all --images --image-dir results\answer_images
```

如果已经有 fetch 生成的 JSON，不想重新联网下载，可以直接从 JSON 生成图片：

```powershell
python ets100_cloud.py render-images results\YYYY-MM-DD_all_answers.json --image-dir results\answer_images
```

图片输出规则：

- 每个考试生成一张 PNG，不会把多个考试堆到一张图里。
- 普通题型输出题目类型、题目和第一个答案。
- 没有题目的题型只输出题目类型和答案。
- `单词朗读` 使用 `original_text`，会把已知 IPA 音标转换为对应英文单词，并保留原文中已有的英文单词。
- `课内语篇朗读` 使用 `original_text` 作为答案。
- `故事复述` 保留 `question_text` 作为文章标题，只取 `answers[0]` 作为答案。

## 8. 解析本地 content.json

如果你已经有一个解压后的 `content.json`，可以直接解析：

```powershell
python ets100_cloud.py parse-local path\to\content.json --group-name 测试题型 --out parsed.json
```

不指定 `--out` 时，结果会直接打印到终端。

## 9. 自定义状态与缓存目录

默认状态目录在脚本旁边。你也可以指定独立目录：

```powershell
python ets100_cloud.py --root D:\ets100_state login --phone 你的手机号 --password 你的密码
python ets100_cloud.py --root D:\ets100_state list
python ets100_cloud.py --root D:\ets100_state fetch --homework-index 0 --out answers.json
```

同一个 `--root` 目录会复用同一个设备码和登录状态。

## 10. 常见问题

### 提示未登录

先运行：

```powershell
python ets100_cloud.py login --phone 你的手机号 --password 你的密码
```

### 提示 CDN SSL verification failed

ETS100 CDN 证书可能存在主机名不匹配。确认风险后使用：

```powershell
python ets100_cloud.py fetch --homework-index 0 --out answers.json --insecure-cdn
```

### 提示 ZIP encryption is unsupported

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

### 作业序号越界

先重新运行：

```powershell
python ets100_cloud.py list
```

然后选择列表中存在的 `--homework-index`。

### token 失效

重新登录即可：

```powershell
python ets100_cloud.py login --phone 你的手机号 --password 你的密码
```

## 11. 支持的题型解析

脚本按 `content.json` 的 `structure_type` 解析以下类型：

- `collector.role`：问答、听选、提问类，读取 `info.question[].std[].value`
- `collector.3q5a`：3 问 5 答类，读取多个标准答案
- `collector.choose`：选择题，读取选项与正确答案
- `collector.picture`：信息转述，读取 `info.std[].value`
- `collector.fill`：填空题，读取 `info.std[].value`
- `collector.dialogue`：回答问题，读取 `info.question[].std[].value`
- `collector.read`：朗读原文，无标准答案

## 12. 风险提示

- 使用云端模式会登录 ETS100 接口，可能导致官方客户端被顶号。
- 明文密码只会在你使用 `--save-password` 时保存。
- `--insecure-cdn` 会跳过 CDN TLS 校验，只建议在下载失败时临时使用。
- 本工具仅供学习与合法授权场景使用。
