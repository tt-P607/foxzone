# QQ 空间 API 接口文档

本文档记录 FoxZone 插件所使用的 QQ 空间 HTTP API，包括 Cookie 获取流程、全部端点说明、参数规范及已知注意事项。

> **参考来源**：本文档内容对照 [astrbot_plugin_qzone](https://github.com/jokerwho/astrbot_plugin_qzone) 与 QQ 空间真实请求行为整理。

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
    "cookies": "<cookie_string>"
  }
}
```

**Cookie 字段说明**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `uin` | QQ 号，带 `o` 前缀 | `o<bot_uin>` |
| `skey` | 登录凭证，具体值由适配器提供 | `<skey>` |
| `p_skey` | 用于计算 gtk2，二级域名 key | `<p_skey>` |

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
  "logininfo": {"name": "昵称", "uin": "<uin>"},
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
| `pagenum` | 首页为 `1`，续页使用服务端 `data.main` 状态中的值，不自行加一 |
| `count` | 获取数量 |
| `format` | `jsonp`，回调为 `_preloadCallback` |
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

**成功响应**：解析回执后，整数 `code=0` 且可选 `subcode=0` 才表示成功。无法解析的回执属于待核实结果。

---

### 3.5 回复评论（楼中楼）

楼中楼回复与普通一级评论使用不同端点。FoxZone 的 `reply` 使用：

**端点**：`POST https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_addreply_ugc`

**查询参数**：`g_tk`。

**表单字段**：
| 参数 | 当前语义 |
|------|----------|
| `uin` | 当前机器人账号 |
| `hostUin` | 说说主人账号 |
| `hostuin` | 当前机器人账号 |
| `topicId` | `<说说主人>_<说说tid>`，不附加 `__1` |
| `commentId` | 所属父级一级评论 ID；回复楼中楼时仍定位到该父级 |
| `commentUin` | 父级一级评论作者账号，不是机器人账号或被回复子评论作者 |
| `content` | 回复正文；指定被回复对象时前置 `@{uin:<目标账号>,nick:<昵称>,auto:1} ` |
| `inCharset` / `outCharset` | `utf-8` |
| `ref` | 空字符串 |
| `code_version` | `1` |
| `format` | `fs` |
| `qzreferrer` | `https://user.qzone.qq.com/<说说主人>` |

昵称在构造提及内容时先将 `%`、`,`、`}` 分别转义为 `%25`、`%2C`、`%7D`，之后由表单编码执行一次传输编码。普通评论仍使用 §3.4 的 `emotion_cgi_re_feeds` 及其旧表单语义；两种写入路径独立，不在失败时切换端点重发。

解析器接受 JSON、JSONP 或 frame callback 载荷。只有整数 `code=0`、可选 `subcode=0` 且存在有效 `data.id` 才作为带新回复 ID 的成功回执；数值 `0` 是有效 ID，不要求十六进制格式。响应缺失新 ID、无法解析或传输中断都不能据此判断未投递。

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
3. **对象字面量**：通过 json5 读取单引号、未引用键等；载荷中的 `undefined` token 映射为 `null`，字符串正文保持原样

解析步骤：
1. 优先解析 JSON 对象；否则定位 JSONP 或 frame callback
2. 跟踪引号、转义及括号深度，提取单个完整对象，拒绝残缺或多余可执行载荷
3. 仅转换字符串之外的 `undefined` token，使用 orjson / json5 读取数据，不执行远端 JavaScript
4. 校验 `code`、`subcode` 与接口返回结构；解析失败不作为写入成功

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

1. **回复端点**：普通评论使用 `user.qzone.qq.com` 代理下的 `emotion_cgi_re_feeds`；楼中楼回复使用 §3.5 的 `h5.qzone.qq.com` 代理及独立 `emotion_cgi_addreply_ugc`。

2. **回复身份**：`commentId` / `commentUin` 对应父级一级评论 ID 与作者。楼中楼目标作者另通过提及正文表达；不得将子评论 ID 或机器人账号替代父级身份。

3. **Cookie 缓存策略**：Cookie 缓存于 `data/foxzone/cookies/cookies-<qq>.json`。当 API 返回 code=-3000 时，需调用 `CookieService.clear_cache()` 清除缓存并重新获取。

4. **适配器可用性**：Cookie 获取依赖已启动的 QQ 适配器透传 `get_cookies` action。若本地缓存失效，请确保至少一个适配器在线；也可在 `config.toml [cookie].adapter_signature` 显式指定。

5. **Chrome UA 必要性**：部分 QQ 空间接口会检查 User-Agent，建议所有请求均使用最新 Chrome UA，避免被反爬限制。

6. **`uin` 字段格式**：Cookie 中的 `uin` 带 `o` 前缀（如 `o123456`），在所有 API 参数中均使用去掉 `o` 前缀的纯数字形式。


---

## 七、外部回查实现要点

外部回查（`autopilot/external.py` 的 `external_followup_once` + `process_reply_batch`）用于检测他人对 Bot 评论的接力回复并自动回应。以下要点直接关系到接口能否调通：

### 7.1 单条说说详情：`emotion_cgi_msgdetail_v6`

- 端点：`GET https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6`。FoxZone 将基础 URL 中的代理域改为 `taotao.qq.com`。
- 请求包含 `uin`、`tid`、`t1_source=1`、`not_trunc_con=1`、`need_right=1`、`not_adapt_outpic=1` 和 `g_tk`。评论分页开启时再附加 `pos` / `num`；关闭时不带这两个字段。不要把其他端点/参数组合曾遇到的 `-10004` 归因于固定参数数量，也不能据此推断已在真实服务端修复。
- 响应解析接受 JSON、JSONP 或 frame callback，并校验返回 `tid` 与请求匹配。详情字段含正文、图片、服务端评论统计、`commentlist` 及其中可用的 `list_3`；这不构成完整楼中楼保证。

### 7.2 一级评论分页与完整性

- `fetch_feed_detail` 默认以 `pos=0`、`num=20` 开始，最多读取 20 页；只在已读一级评论数与已知服务端统计相等时标记 `comments_complete=True`。空页本身不能证明完整；短页、重复页、页数预算耗尽或后续页读取失败都不会被当成完整。
- 跨页按父评论 ID、评论 ID、作者组成的身份去重。`comment_total` / `comment_count` 保存服务端统计值，`comments` 是当前取得的扁平列表；服务端统计为 `0` 与未知值 `None` 均不会被替换成列表长度。
- `comments_complete` 只表示一级评论的计数完整性；`list_3` 内联回复的完整性当前标记为 `inline_replies_complete=False`。分页预算不是全部评论或楼中楼完整性的承诺。

### 7.3 回复索引与评论身份

- 批量回复的 `comment_index`、好友动态决策的 `reply_comment_index` 均为本次输入列表的 1 基序号。生成器将序号映射为本次输入的完整身份键；好友动态写入前会重新读取详情，要求所选身份唯一，并核实作者与父评论身份。
- 评论 ID 是不透明值：`0` 有效，不要求 hex。一级评论与 `list_3` 子回复可能跨层重复 ID，因此单独按 ID 或昵称匹配不够；二级回复需解析到父级一级评论身份。
- `qzone_post_comment` 接受精确目标 `comment_id` / `comment_uin`；二级回复另需 `parent_comment_id` / `parent_comment_uin`。不完整或无法唯一核实的身份不会退化成普通评论。

### 7.4 好友动态解析与分页

- `feeds3_html_more` 的正文优先取非空 `qz_info_complete`，避免 `qz_info_cut` 摘要或评论区域污染正文。`content_complete` 为 `true` / `false` / `None` 分别表示已识别完整正文、折叠正文或未知；未知时保留当前正文，不保证长文完整。
- `None`、空列表和数值 `0` 保持不同含义：缺失列表/统计不等于空列表/零。评论统计来自服务端字段，不由当前可见评论数量反推。
- 每轮监控从首页初始状态开始，原样接续服务端 `main` 状态，省略字段沿用上一页值；`aisortNextTime` 映射为请求字段 `getAisort`。没有有效结束标志、下一页状态缺失、字段异常、完整游标停滞或超过本轮预算时停止并报告异常。单独变化的服务器时钟不算推进，不跨轮保存长游标，也不手工要求服务端页码递增一。
- 接续状态包括 `begintime`、`dayspac`、`pagenum`、`aisortEndTime`、`aisortOffset`、`aisortBeginTime`、`aisortNextTime`、`icServerTime`、`externparam`、`daylist`、`uinlist`，保留其中服务端不透明参数，不自行拆解或拼接。
- 图片优先级为当前解析路径提供的原图/大图字段（包括 `data-originurl`、`url3`、`url2`、`url1`、`smallurl`），过滤非法及非 HTTP(S) URL 并按 URL 去重。结果仅覆盖响应中可见的图片，不保证完整相册或 9 张以上图片。

### 7.5 未知写入结果与状态

- 普通评论保留 `emotion_cgi_re_feeds` 旧语义；楼中楼回复使用独立端点。未知回执、无效回执、缺少新回复 ID 或写后断线不返回成功，也不转换成普通 `False` 让调用方重试。
- 未知结果写入既有 `reply_tracker` 的 `pending` 状态，自动流程据此阻止重发；状态按机器人账号、说说主人、父评论 ID/作者及目标作者隔离。旧 `data` 记录继续作为 `legacy` 防重复标记，不伪造其归属。
- 需先人工只读核实 QQ 空间中的投递结果，不能凭连接异常或缺失回执判断未发送。此文档及离线测试不证明真实服务端已接收、展示或升级部署。

### 7.6 限流与串行化

- `-10049` 按明确拒绝限流处理，不在该写请求上切换普通评论/回复端点。外部接力场景跳过当前项并继续本批；好友动态评论场景终止本批，未处理项留待后续轮次。
- 普通异常重试策略由 `BatchSendEngine` 控制；未知结果使用独立 pending 路径，不进入通用重试。发送经插件级 runtime 的 `reply_send_lock` 串行化，LLM 决策不持有该锁。

### 7.7 `InteractionLog` 一致性

持久化状态（互动日志、回复跟踪等）由插件级单例 `QZoneRuntime` 持有。
