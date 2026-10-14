# Pixiv 小说动态与下载

使用配置中的 Cookie 查询当前登录账号的小说动态。每次由命令或 LLM 工具触发一次请求，将本次成功响应与上次成功响应比较，返回新增小说。

```text
GET https://www.pixiv.net/ajax/follow_latest/novel?p=1&mode=all&lang=zh
```

## 使用

1. 在 AstrBot 插件页加载插件并安装 `requirements.txt` 中的依赖。
2. 在插件配置中填写浏览器请求的完整 Cookie 值，不带 `Cookie:` 前缀或引号，保持单行。需要代理时配置网络代理。
3. 保存后重新加载插件，发送 `/pixiv检查`，或在 AI 对话中说“检查 Pixiv 小说有没有新动态”。

首次成功检查保存比较基线，不判断新增；之后检查返回新增小说的标题、作者、发布时间和链接。请求失败时保留上次成功结果。

| 命令 | 英文别名 | 功能 | LLM 工具 |
| --- | --- | --- | --- |
| `/pixiv检查` | `/pixivcheck` | 请求当前 Cookie 账号的小说动态并返回新增作品 | `pixiv_novel_check` |
| `/pixiv状态` | `/pixivstatus` | 查看上次成功检查时间、作品数量和最近错误 | `pixiv_novel_status` |
| `/pixiv下载 <链接>` | `/pixivdl` | 下载单篇小说或整个系列，完成后文字通知 | `pixiv_novel_download` |
| `/pixiv下载状态 [任务ID]` | `/pixivdlstatus` | 查询当前会话下载进度和保存结果 | `pixiv_novel_download_status` |
| `/pixiv帮助` | `/pixivhelp` | 查看帮助 | — |

工具使用 `@filter.llm_tool` 注册。检查和检查状态无需参数；下载工具使用 `url`，下载状态使用可选 `job_id`。命令前缀取决于 AstrBot 设置。默认仅管理员或聊天平台用户白名单可使用，所有调用共享当前 Cookie 账号的比较记录，结果返回调用所在会话。

## 小说下载

支持两种链接，自动判断类型：

```text
/pixiv下载 https://www.pixiv.net/novel/series/15917685
/pixiv下载 https://www.pixiv.net/novel/show.php?id=28347721
```

也可通过 AI 对话要求下载这些链接。工具立即返回下载任务 ID，后台执行，保存完成后在发起请求的会话发送文字通知，包含标题、章节数和保存目录。不会上传或发送文件。通过 `/pixiv下载状态` 查看进度和失败原因。

新增 `save_path` 配置，作为下载保存根目录。留空使用 `data/plugin_data/astrbot_plugin_pixiv_novel_sub/downloads`；支持绝对路径、`~` 和相对于 AstrBot 工作目录的路径。Docker 中填写容器内路径。修改后重新加载插件。

系列目录使用系列名和作者，包含全部可访问章节：

```text
保存路径/
└── 系列名_作者/
    ├── metadata.json
    ├── 第1章 章节名.txt
    ├── 第2章 章节名.txt
    └── ...
```

单篇目录使用小说名和作者：

```text
保存路径/
└── 小说名_作者/
    ├── metadata.json
    └── 第1章 小说名.txt
```

系列通过 `/ajax/novel/series_content/{id}?limit=30&last_order=0&order_by=asc&lang=zh` 读取目录，根据 `series.contentOrder` 推进分页、排序并编号，再逐章使用 `/ajax/novel/{id}?lang=zh` 下载正文。第 30 章之后继续分页，若原系列顺序存在缺号会保留原编号。系列名优先从首章或目录中获取；缺失时补请求 `/ajax/novel/series/{id}?lang=zh` 获取名称。

TXT 为 UTF-8 文本，保留完整正文和 Pixiv 自有标记（如 `[newpage]`、`[chapter:...]`）。`metadata.json` 是可读的格式化 JSON 文本，记录类型、ID、标题、作者、原链接、保存时间、章节总数、完成状态及每章标题、文件名、链接、日期、简介和标签。仅全部章节及 metadata 保存成功才通知完成。

同名目录已存在时添加作品 ID 和序号后缀另存，避免覆盖已有文件。Windows 文件名中的非法字符替换为 `_`，过长名称截短，完整标题保留在 metadata 中。章节文件和 metadata 都采用临时文件原子替换；中途失败保留已保存章节，并将 metadata 标为 `failed`，中止标为 `cancelled`。重新下载会创建新目录，不自动续下。

下载复用现有 Cookie、代理和使用权限，不添加轮询或订阅配置。下载按顺序执行，最多接收五个未完成任务，请求之间有固定的短间隔。系列分页超过 1000 页或游标未前进时停止并报错。下载状态仅当前会话且有权限的用户可查看，任务记录在重启后清空，保存文件继续保留。

## 配置

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `cookie` | 空 | 当前 Pixiv 登录账号的 Cookie，配置界面标记为 secret |
| `save_path` | 空 | 小说下载保存根目录，留空使用插件数据目录下的 downloads |
| `network_proxy.proxy_mode` | system | system 环境代理 / custom 自定义代理 / none 禁用环境代理 |
| `network_proxy.proxy_url` | 空 | HTTP 或 SOCKS 代理地址 |
| `admin_only` | true | 限制为管理员或白名单用户 |
| `allowed_users` | [] | 聊天平台用户白名单 |

请求固定使用 `p=1`、`mode=all`、`lang=zh`。身份来自 Cookie；请求所需的 `x-user-id` 可从 PHPSESSID 自动获取，无需单独配置。保留浏览器 User-Agent、Accept、Accept-Language、Referer 等请求头，压缩使用 gzip/deflate。

## 保存与比较

运行数据存放于 `StarTools.get_data_dir("astrbot_plugin_pixiv_novel_sub")`，通常为：

```text
data/plugin_data/astrbot_plugin_pixiv_novel_sub/state.json
```

状态保存上次完整成功 JSON 响应、当次小说 ID、累计已见 ID、账号摘要和成功时间。按小说 ID 判断新增，排列或标题变化不会算新增，旧作品离开第一页后重新出现也不会重复报告。合法空列表同样会保存。

请求错误、登录验证失败、非法 JSON 或状态保存失败时不推进比较基线。使用临时文件、flush/fsync 和原子替换保存状态，写入成功后才更新内存。状态损坏时保留文件并报告错误。重启保留比较记录，同一账号刷新 Cookie 保留基线，切换账号后重新建立基线。

旧版状态文件会沿用比较记录，移除订阅会话和待发送通知字段；不会主动发送消息。

只检测该接口第一页中出现的作品。如果两次调用之间有大量更新，已移出第一页的作品无法检测。首次进入动态范围的旧作品也可能被视为新增 ID。

Cookie 不写入源代码或状态文件，错误输出不包含请求凭据。Cookie 失效或浏览器验证过期时，在浏览器重新登录/验证后更新配置。真实账号请求需要配置后通过 `/pixiv检查` 验证。

## 本地验证

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py monitor.py downloader.py tests
```

测试使用 HTTP MockTransport、临时状态文件和 AstrBot 接口替身，不访问真实账号。

AstrBot 接口参考：[官方插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)、[StarTools 源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/star_tools.py)。

