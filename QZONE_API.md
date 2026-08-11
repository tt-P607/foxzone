# QQ 空间 API 接口文档

本文档记录 FoxZone 插件所使用的 QQ 空间 HTTP API，包括 Cookie 获取流程、全部端点说明、参数规范及已知注意事项。

> **参考来源**：本文档根据 [astrbot_plugin_qzone](https://github.com/jokerwho/astrbot_plugin_qzone) 最新实现对照更新，最后同步时间：2025 年。

---

## 一、Cookie 获取流程

### 1.1 流程概述

```
请求方 → 适配器 get_cookies action → 解析 Cookie 字符串 → 缓存至本地 JSON → 使用 Cookie 访问 QQ 空间 API
```

FoxZone 通过 `src.app.plugin_system.api.adapter_api` 获取已启动的 QQ 适配器实例，
调用其统一的 `get_cookies` action 取回 Cookie 字符串。
适配器签名留空时遍历所有已启动且支持 API 透传的适配器，首个成功即返回；
也可在 `config.toml [cookie].adapter_signature` 显式指定。无需额外开启 HTTP 服务器。

### 1.2 Cookie 域名

| 字段 | 值 |
|------|-----|
| 域名 | `user.qzone.qq.com` |

### 1.3 适配器获取 Cookie

**调用**：经适配器统一透传 `get_cookies` action（各 QQ 适配器同命令名），
参数为 `{"domain": "user.qzone.qq.com"}`。

**响应结构**：
```json
{
  "status": "ok",
  "data": {
    "cookies": "uin=o123456789; skey=@abcdefgh; p_skey=xyz..."
  }
}
```

**Cookie 字段说明**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `uin` | QQ 号，带 `o` 前缀 | `o123456789` |
| `skey` | 登录凭证，带 `@` 前缀 | `@Abc12345` |
| `p_skey` | 用于计算 gtk2，二级域名 key | `AbcDef...` |

> **注意**：`uin` 在 Cookie 中以 `o` 开头，使用时需去掉前缀 `o` 取数字部分。

### 1.4 gtk2 计算（g_tk）

所有 QQ 空间 API 请求均需携带 `g_tk` 参数，由 `p_skey` 通过以下算法计算：

```python
def generate_gtk(p_skey: str) -> str:
    hash_val = 5381
    for char in p_skey:
        hash_val += (hash_val << 5) + ord(char)
    return str(hash_val & 0x7FFFFFFF)
```

---

## 二、通用请求头

所有 API 请求建议携带以下请求头（模拟 Chrome 浏览器）：

```http
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36
Referer: https://user.qzone.qq.com/<uin>
Origin: https://user.qzone.qq.com
Host: user.qzone.qq.com
Connection: keep-alive
```

---

## 三、API 端点详细说明

### 3.1 发布说说

**端点**：`POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6`

**查询参数**：
| 参数 | 值 |
|------|-----|
| `g_tk` | gtk2 计算值 |

**表单参数**：
| 参数 | 说明 |
|------|------|
| `con` | 说说正文内容 |
| `hostuin` | Bot QQ 号 |
| `format` | 固定 `json` |
| `feedversion` | 固定 `1` |
| `ver` | 固定 `1` |
| `ugc_right` | 固定 `1` |
| `to_sign` | 固定 `0` |
| `code_version` | 固定 `1` |
| `syn_tweet_verson` | 固定 `1` |
| `paramstr` | 固定 `1` |
| `who` | 固定 `1` |
| `qzreferrer` | `https://user.qzone.qq.com/<uin>` |
| `pic_bo` | 图片 bo 值，多图以 `,` 分隔（可选） |
| `richtype` | 带图时为 `1`（可选） |
| `richval` | 图片 richval，多图以 `\t` 分隔（可选） |

**成功响应**：
```json
{"code": 0, "tid": "<new_tid>", ...}
```

**错误码**：
| code | 说明 |
|------|------|
| `0` | 成功 |
| `-3000` | Cookie 失效，需重新获取 |

---

### 3.2 获取说说列表

**端点**：`GET https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6`

**查询参数**：
| 参数 | 说明 |
|------|------|
| `g_tk` | gtk2 计算值 |
| `uin` | 目标 QQ 号 |
| `ftype` | 固定 `0`（全部说说） |
| `sort` | 固定 `0`（最新在前） |
| `pos` | 起始位置，默认 `0` |
| `num` | 获取数量 |
| `replynum` | 获取的评论数，建议 `999` |
| `code_version` | 固定 `1` |
| `format` | 固定 `json` |
| `need_comment` | 固定 `1`（含评论） |
| `need_private_comment` | 固定 `1`（含私密评论，可选） |

**成功响应结构**：
```json
{
  "code": 0,
  "logininfo": {"name": "昵称", "uin": 123456},
  "msglist": [
    {
      "tid": "说说ID",
      "content": "说说内容",
      "created_time": 1700000000,
      "commentlist": [
        {
          "tid": "评论ID",
          "uin": "评论者QQ",
          "name": "评论者昵称",
          "content": "评论内容",
          "list_3": []
        }
      ],
      "pic": [{"url1": "图片URL"}]
    }
  ]
}
```

---

### 3.3 获取好友动态流

**端点**：`GET https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more`

**查询参数**：
| 参数 | 说明 |
|------|------|
| `g_tk` | gtk2 计算值 |
| `uin` | Bot QQ 号 |
| `scope` | 固定 `0` |
| `view` | 固定 `1` |
| `filter` | 固定 `all` |
| `flag` | 固定 `1` |
| `applist` | 固定 `all` |
| `pagenum` | 页码（实测可能无效），默认 `1` |
| `count` | 获取数量 |
| `format` | 固定 `json` |
| `useutf8` | 固定 `1` |
| `outputhtmlfeed` | 固定 `1` |

---

### 3.4 发表评论

**端点**：`POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds`

**查询参数**：
| 参数 | 值 |
|------|-----|
| `g_tk` | gtk2 计算值 |

**表单参数**：
| 参数 | 说明 |
|------|------|
| `topicId` | `<目标QQ>_<说说tid>__1` |
| `uin` | Bot QQ 号 |
| `hostUin` | 说说主人 QQ 号 |
| `feedsType` | 固定 `100` |
| `inCharset` | 固定 `utf-8` |
| `outCharset` | 固定 `utf-8` |
| `plat` | 固定 `qzone` |
| `source` | 固定 `ic` |
| `platformid` | 固定 `52` |
| `format` | 固定 `fs` |
| `ref` | 固定 `feeds` |
| `content` | 评论内容 |

**成功响应**：响应文本通常为非标准格式，解析为 JSON 后 `code=0` 表示成功。

---

### 3.5 回复评论（二级评论）

> 字段语义与请求特征以浏览器 DevTools 实际抓包为准对齐。任何字段偏差都会触发 QZone 反爬伪装错误 `-10049`。

**端点**：`POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds`

**查询参数**：
| 参数 | 值 |
|------|-----|
| `g_tk` | gtk2 计算值 |

**表单参数**（顺序按浏览器实际请求）：
| 参数 | 说明 |
|------|------|
| `topicId` | `<说说主人QQ>_<说说tid>__1` |
| `feedsType` | 固定 `100` |
| `inCharset` | 固定 `utf-8` |
| `outCharset` | 固定 `utf-8` |
| `plat` | 固定 `qzone` |
| `source` | 固定 `ic` |
| `hostUin` | 说说主人 QQ 号 |
| `isSignIn` | 固定空字符串 `""`（**必传**） |
| `platformid` | 固定 `52` |
| `uin` | Bot QQ 号 |
| `format` | 固定 `fs` |
| `ref` | 固定 `feeds` |
| `content` | **必须包含 @ 提及格式**：`@{uin:<被回复者QQ>,nick:<被回复者昵称>,auto:1} <实际内容>` |
| `commentId` | **顶层一级评论的 tid**（不是被回复的二级评论 tid） |
| `commentUin` | **Bot 自身 QQ 号（操作者 uin）**，不是评论作者 QQ |
| `richval` | 固定 `""` |
| `richtype` | 固定 `""` |
| `private` | 固定 `"0"` |
| `paramstr` | 固定 `"1"`（不是 `"2"`） |
| `qzreferrer` | `https://user.qzone.qq.com/<bot_uin>`（**不带 `/main`**） |

**特殊请求头**（按浏览器实际请求对齐，缺失会触发反爬 -10049）：
```http
Accept: */*
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
Sec-CH-UA: "Chromium";v="138", "Not(A:Brand";v="99", "Google Chrome";v="138"
Sec-CH-UA-Mobile: ?0
Sec-CH-UA-Platform: "Windows"
Referer: https://user.qzone.qq.com/<bot_uin>
Origin: https://user.qzone.qq.com
```

**响应格式**（`format=fs`）：
QZone 返回的是 frame 桥接 HTML 而非纯 JSON，需用正则从中提取 `frameElement.callback({...})` 的 JSON 片段：

```regex
frameElement\.callback\s*\(\s*(\{[\s\S]*?\})\s*\)
```

`code=0` 表示成功；`code=-10049` 表示触发反爬（实为请求特征异常被识别，并非真限流）；`code=-3000` 表示 cookie 失效。

**⚠️ 字段语义（关键）**：
- `commentUin` 在 reply 接口里是"**操作者 uin**"（即 Bot 自身），不是被回复评论的作者 QQ。写错会被 QZone 反爬识别为请求特征异常，返回 `-10049 使用人数过多`。
- 被回复者的 QQ 与昵称必须以 `@{uin:xxx,nick:xxx,auto:1} ` 前缀写入 `content` 字段，否则同样触发反爬。
- `commentId` 必须是**顶层一级评论的 tid**。当用户回复的是二级评论时，需要从评论树里向上溯源到顶层评论。

---

### 3.6 点赞说说

**端点**：`POST https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app`

**查询参数**：
| 参数 | 值 |
|------|-----|
| `g_tk` | gtk2 计算值 |

**表单参数**：
| 参数 | 说明 |
|------|------|
| `opuin` | 操作者（Bot）QQ 号 |
| `unikey` | `http://user.qzone.qq.com/<目标QQ>/mood/<tid>` |
| `curkey` | 同 `unikey` |
| `appid` | 固定 `311`（说说应用 ID） |
| `from` | 固定 `1` |
| `typeid` | 固定 `0` |
| `abstime` | 当前 Unix 时间戳 |
| `fid` | 说说 tid |
| `active` | 固定 `0` |
| `format` | 固定 `json` |
| `fupdate` | 固定 `1` |

---

### 3.7 上传图片

**端点**：`POST https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image`

**查询参数**：
| 参数 | 值 |
|------|-----|
| `g_tk` | gtk2 计算值 |

**表单参数**：
| 参数 | 说明 |
|------|------|
| `filename` | 固定 `filename` |
| `uploadtype` | 固定 `1` |
| `albumtype` | 固定 `7` |
| `exttype` | 固定 `0` |
| `skey` | Cookie 中的 skey |
| `zzpaneluin` | Bot QQ 号 |
| `p_uin` | Bot QQ 号 |
| `uin` | Bot QQ 号 |
| `p_skey` | Cookie 中的 p_skey |
| `output_type` | 固定 `json` |
| `charset` | 固定 `utf-8` |
| `output_charset` | 固定 `utf-8` |
| `upload_hd` | 固定 `1` |
| `hd_width` | 固定 `2048` |
| `hd_height` | 固定 `10000` |
| `hd_quality` | 固定 `96` |
| `base64` | 固定 `1` |
| `picfile` | 图片的 base64 编码 |
| `refer` | 固定 `shuoshuo` |

---

## 四、响应解析说明

QQ 空间 API 响应为非标准 JSON，存在以下格式变体：

1. **纯 JSON**：直接解析
2. **JSONP 回调**：`_preloadCallback({...})` 或 `callback({...})`，需提取括号内内容
3. **混合非标准 JSON**：含 `undefined` 等非法值，需替换为 `null` 再解析

解析步骤：
1. 尝试匹配 JSONP 回调正则 `callback\s*\(\s*(\{.*\})\s*\)`
2. 若失败，提取第一个 `{` 到最后一个 `}` 之间的内容
3. 将 `undefined` 替换为 `null`
4. 用 json5 解析（容错）

---

## 五、错误码对照表

| code | 说明 | 处理方式 |
|------|------|---------|
| `0` | 成功 | — |
| `-1` | 未知错误 | 记录日志 |
| `-3000` | Cookie 失效 / 登录过期 | 清除本地缓存，重新从适配器获取 |
| `-403` / `403` | 权限不足（非好友/隐私设置） | 记录日志，跳过 |

---

## 六、已知问题与注意事项

1. **回复端点域名**：回复（二级评论）与评论（一级评论）接口均使用 `user.qzone.qq.com` 子域（详见 §3.5）。

2. **`commentId` vs `parent_tid`**：旧版回复参数 `parent_tid` 已不再有效，必须改用 `commentId` + `commentUin` 组合。

3. **Cookie 缓存策略**：Cookie 缓存于 `data/foxzone/cookies/cookies-<qq>.json`。当 API 返回 code=-3000 时，需调用 `CookieService.clear_cache()` 清除缓存并重新获取。

4. **适配器可用性**：Cookie 获取依赖已启动的 QQ 适配器透传 `get_cookies` action。若本地缓存失效，请确保至少一个适配器在线；也可在 `config.toml [cookie].adapter_signature` 显式指定。

5. **Chrome UA 必要性**：部分 QQ 空间接口会检查 User-Agent，建议所有请求均使用最新 Chrome UA，避免被反爬限制。

6. **`uin` 字段格式**：Cookie 中的 `uin` 带 `o` 前缀（如 `o123456`），在所有 API 参数中均使用去掉 `o` 前缀的纯数字形式。


---

## 七、外部回查实现要点

外部回查（`autopilot/external.py` 的 `external_followup_once` + `process_reply_batch`）用于检测他人对 Bot 评论的接力回复并自动回应。以下要点直接关系到接口能否调通：

### 7.1 单条说说详情：`emotion_cgi_msgdetail_v6`

- 端点：`GET https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6`
  - 子域必须是 `taotao.qq.com`，不是 `taotao.qzone.qq.com`，写错会返回 `code=-10004`。
- 必传参数仅 4 个：`uin`、`tid`、`format=jsonp`、`g_tk`。任何额外字段都会被反爬识别为异常请求，返回 `-10004`。
- 响应是 JSONP，需先剥掉 `_Callback(...)` 或 `frameElement.callback(...)` 外壳再 JSON 解析。
- 用途：评论一旦超出 `msglist_v6` 时间线最近 5 条一级评论范围，必须靠该接口拿到完整评论树才能解析 `parent_tid` 的根节点。

### 7.2 msglist_v6 内嵌评论 `tid` 是局部序号

`msglist_v6` 内嵌 `list_3` 楼中楼的 `tid` 是该说说内的局部序号（如 `"1"`、`"9"`），不是 hex 全局 tid；`msgdetail_v6` 才返回 hex 全局 tid。`reply` 接口的 `commentId` 必须是**顶层一级评论的 hex tid**——若溯源后仍是局部序号形态（`core/comment_tree.py` 的 `is_local_seq_tid` 预检），说明缺一级父节点，直接跳过避免触发 -10049。

### 7.3 限流处理（`-10049`）

- 外部接力路径：`reply` 触发 `-10049` 时**单条**跳过（标记该评论已处理，避免下轮重复触发），循环继续处理本批后续条目。
- 好友监控评论路径：触发限流即**终止整批**，剩余项保持未标记以便下轮再试。
- 两种策略由 `autopilot/engine.py` 的 `BatchPolicy.stop_batch_on_rate_limit` 参数区分。

### 7.4 串行化发送（runtime 实例锁）

发送循环通过 `QZoneRuntime.reply_send_lock`（`asyncio.Lock`）串行化。runtime 是插件级单例，实例级锁即可在插件内全局生效。LLM 决策不持锁，仍可并发。

### 7.5 `InteractionLog` 一致性

持久化状态（`interaction_log.json` 等）由插件级单例 `QZoneRuntime` 统一持有。
