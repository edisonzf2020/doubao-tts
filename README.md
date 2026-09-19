# 豆包 TTS 客户端 + OpenAI 兼容服务

逆向豆包网页端 VoiceGenie 语音合成接口的 Python 客户端，附带一个兼容 OpenAI
`/v1/audio/speech` 协议的 HTTP 服务。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

只用命令行客户端时，`fastapi` / `uvicorn` 可以不装。

## 配置 Cookie（必须）

需要豆包登录态 Cookie。用**两份文件**保存，内容始终自动同步：

| 文件 | 格式 | 用途 |
| --- | --- | --- |
| `.cookie` | 原始 Cookie 头字符串 | 从 DevTools 复制的格式，**手动更新的入口** |
| `.doubao_cookie` | JSON 数组 | 带过期时间，用于算剩余天数 |

只需提供任一份，另一份会自动生成。

### 方式一：复制 Cookie 头（最简单）

1. 浏览器登录 [豆包](https://www.doubao.com)
2. `F12` → **Network** → 点任意请求 → 在 Request Headers 里复制 **Cookie** 整行
3. 保存：

```bash
python renew_cookie.py --set-cookie "粘贴的内容"
# 或直接写文件
printf '%s\n' "粘贴的内容" > .cookie
```

### 方式二：浏览器扩展导出 JSON

用 Cookie-Editor 等扩展导出 `doubao.com` 的**全部** Cookie 为 JSON，存为 `.doubao_cookie`。

> ⚠️ **不要只挑几个字段**——登录态依赖 `sessionid_ss`、`sid_ucp_v1`、
> `session_tlb_tag`、`ttwid` 等多个 httpOnly 字段。

Cookie 有效期约 30 天——但本项目支持**自动续期**，配好后无需反复导出。

---

## 一、命令行

```bash
# 基础用法（默认 mp3）
python doubao_tts.py "你好，世界" -o hello.mp3

# 指定音色
python doubao_tts.py "欢迎使用豆包" -s yangguang -o welcome.mp3

# 语速倍率 1.5 倍、音调升 3 个半音
python doubao_tts.py "快速朗读测试" --speed 1.5 --pitch 3 -o fast.mp3

# 其他格式
python doubao_tts.py "测试" --format wav -o out.wav

# 查看/搜索音色
python doubao_tts.py --list-speakers
python doubao_tts.py --search 东北
```

## 二、Python API

```python
from doubao_tts import DoubaoTTS, TTSConfig

# 简单使用
tts = DoubaoTTS()
result = tts.synthesize_sync("你好，世界")
if result.success:
    open("output.mp3", "wb").write(result.audio_data)

# 自定义配置
config = TTSConfig(
    speaker="zh_male_yangguang_conversation_v4_wvae_bigtts",
    speech_rate=1.2,   # 倍率，1.0 为正常
    pitch=0,           # 半音，-12 ~ 12
    format="mp3",      # mp3 / ogg_opus / wav / pcm
)
result = DoubaoTTS(config).synthesize_sync("自定义语音测试")

# 链式调用
result = (DoubaoTTS()
          .set_speaker("rap")
          .set_speed(1.3)
          .synthesize_sync("说唱风格的文本"))
```

---

## 三、OpenAI 兼容服务

### 启动

推荐用 `.env` 配置：

```bash
cp .env.example .env
# 用编辑器把 DOUBAO_TTS_API_KEY 改成你自己的密钥
#   生成: python3 -c "import secrets; print('sk-' + secrets.token_urlsafe(32))"

python openai_server.py
```

也可以直接用环境变量（优先级高于 `.env`）：

```bash
export DOUBAO_TTS_API_KEY="sk-your-secret"
python openai_server.py
```

> ⚠️ 这个服务持有你的豆包登录态。未设置 `DOUBAO_TTS_API_KEY` 时只绑定
> `127.0.0.1`；若同时把 `DOUBAO_TTS_HOST` 设为非回环地址，服务会拒绝启动。

### 环境变量

完整注释版本见 [`.env.example`](.env.example)。加载顺序：
**系统环境变量 > `.env` > 代码默认值**。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DOUBAO_TTS_API_KEY` | 无 | Bearer Token；未设置则不鉴权且仅回环可达 |
| `DOUBAO_TTS_HOST` | `127.0.0.1` | 监听地址 |
| `DOUBAO_TTS_PORT` | `8000` | 监听端口 |
| `DOUBAO_TTS_CONCURRENCY` | `8` | 并发上限，防止触发豆包风控 |
| `DOUBAO_TTS_MAX_INPUT` | `4096` | 单次请求文本长度上限 |
| `DOUBAO_TTS_KEEPALIVE` | `1` | cookie 自动保温；`0`/`false`/`off` 关闭 |
| `DOUBAO_TTS_KEEPALIVE_INTERVAL_H` | `12` | 保温检查间隔（小时） |
| `DOUBAO_TTS_KEEPALIVE_THRESHOLD_D` | `25` | 剩余天数低于此值才续期 |

### 用官方 OpenAI 客户端调用

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-your-secret")

client.audio.speech.create(
    model="tts-1",           # 豆包无模型概念，接受任意值
    voice="taozi",           # 必须命中音色表，见下
    input="你好，世界",
    response_format="mp3",
    speed=1.0,
).stream_to_file("out.mp3")
```

### 参数映射

| OpenAI 参数 | 本服务行为 |
| --- | --- |
| `model` | **接受并忽略**（豆包无模型概念），默认 `tts-1` |
| `input` | 合成文本，上限 `DOUBAO_TTS_MAX_INPUT` |
| `voice` | **必须命中音色表**，不在表中返回 422；可传简称 / speaker_id / 中文名 |
| `response_format` | `mp3` / `opus`→`ogg_opus` / `wav` / `pcm`；`aac`、`flac` 无对应，明确报错 |
| `speed` | 倍率；OpenAI 允许 0.25~4.0，豆包只到 0.5~2.0，超出部分会被截断 |
| `instructions` | 接受并忽略（豆包无此能力） |
| `pitch` | **本服务扩展**，-12~12 半音，OpenAI 协议无此参数 |

`voice` 接受三种写法：内置简称（`taozi`、`rap`）、speaker_id
（`zh_male_chaawangqiang`、`ICL_4b1feb0ced67`）或中文名（`成都妹妹`）。

### 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/audio/speech` | 语音合成，OpenAI 兼容 |
| GET | `/v1/models` | 模型列表（固定别名） |
| GET | `/v1/audio/voices` | 音色列表（本服务扩展，支持 `?q=` `?tab=` `?limit=`） |
| GET | `/health` | 健康检查，含 cookie 剩余天数与音色加载状态 |

### 并发模型

每个请求独占一条 WebSocket，彼此无共享状态，因此天然并发安全。
信号量把同时在途的连接数限制在 `DOUBAO_TTS_CONCURRENCY`，超出的请求排队而非失败。

---

## 可用音色

完整音色表在 **`voices.json`**（当前 304 个，另加 7 个内置简称），
由 `fetch_voices.py` 从豆包 `/alice/user_voice/recommend` 拉取。

```bash
# 重新拉取（音色会随豆包更新变动）
python fetch_voices.py

# 查看/搜索
python doubao_tts.py --list-speakers
python doubao_tts.py --search 四川
```

常用简称：

| 简称 | 完整 ID | 描述 |
| --- | --- | --- |
| `taozi` | `zh_female_wenroutaozi_uranus_bigtts` | 温柔桃子（前端默认） |
| `vv` | `zh_female_vv_uranus_bigtts` | 女声 vv |
| `shuangkuai` | `zh_female_shuangkuaisisi_moon_bigtts` | 爽快女声 |
| `yangguang` | `zh_male_yangguang_conversation_v4_wvae_bigtts` | 阳光男声 |
| `rap` | `zh_male_rap_mars_bigtts` | 说唱男声 |
| `en_female` | `en_female_sarah_conversation_bigtts` | 英文女声 |
| `en_male` | `en_male_adam_conversation_bigtts` | 英文男声 |

> ⚠️ 服务端对无法识别的 speaker ID 会**静默回退**到默认音色而不报错。
> 因此 HTTP 服务会强制校验 `voice` 必须命中音色表，不在表中直接返回 422。

---

## Cookie 保温（自动续期）

核心登录态有效期约 **30 天**，但字节的心跳端点
`/passport/token/beat/v2/` 会把它**滚动续期**回 ~29.85 天。
只要定期调用，登录态就不会过期，**不需要反复重新导出**。

```bash
# 查看状态（两份文件 + 剩余天数）
python renew_cookie.py --status

# 对齐两份文件 + 按需续期（剩余 < 25 天才续）
python renew_cookie.py

# cookie 失效后重新保存
python renew_cookie.py --set-cookie "新的 Cookie 头"

# cron 每天跑一次，永不过期
0 4 * * * cd /path/to/doubao-tts && .venv/bin/python renew_cookie.py --quiet >> renew.log 2>&1
```

**HTTP 服务启动时会自动对齐 + 续期**，之后每 12 小时检查一次，
剩余不足 25 天就自动续期，并**同时写回两份文件**。

### 两份文件不一致时怎么处理

不靠猜哪个新，而是**让服务端判定**：先用 `.cookie` 调心跳验证，
失败就换 `.doubao_cookie` 再试，哪份通过就用它覆盖另一份，都失败则报错。

> 心跳端点**自带节流**：同一 session 刚续过不会重发新 cookie，
> 所以「本次未下发新 cookie」是正常结果，不是失败。
> 只有当核心登录态**真的已失效**时才需要重新导出。

---

## 协议说明

### 端点

```
wss://frontier-audio-web-ws.doubao.com/api/v2/sami/voicegenie
```

> 旧端点 `wss://ws-samantha.doubao.com/samantha/audio/tts`（JSON + aac）仍存活，
> 但豆包前端只把它用于**音色试听**，配额极紧，跑正文朗读会很快被
> `710022002 block`。本项目已迁移到上面的新端点。

### 消息格式

Protobuf，非 JSON。`data.speech.gateway.WebSocketRequest` 字段：
`token=1 appkey=2 namespace=3 version=4 event=5 payload=6 data=7 task_id=8 seq_id=9 session_id=10`。
本项目内置极简 protobuf 编解码，**无需 protobuf 依赖**。

### 事件流程

```
→ StartTask                        ← TaskStarted      (status_code=20000000)
→ StartSession  {tts:{speaker,…}}  ← SessionStarted
→ BidirectionalTTS  {text:"…"}
→ EndTTS
                                   ← TTSSentenceStart
                                   ← TTSResponse × N   (data 字段携带音频)
                                   ← TTSSentenceEnd
                                   ← TTSEnded
```

## 测试

```bash
# 离线测试（不需要 cookie / 网络，克隆后立即可跑）
.venv/bin/python test_smoke.py       # protobuf 编解码、格式映射、参数 clamp、导入
.venv/bin/python test_voices.py      # 音色解析白名单
.venv/bin/python test_dotenv.py      # .env 加载与优先级

# 联网测试（需要活跃 cookie，会触网续期，跑完自动恢复 cookie）
.venv/bin/python test_cookie_sync.py # 双文件对齐 5 场景
```

所有测试都自带清理：会临时改写的 `.env` / `.cookie` 在结束时无条件恢复。

## 注意事项

1. 仅供学习研究使用，请勿用于商业用途
2. 接口可能随时变更
3. 服务持有你的登录态，对外暴露前务必设置 API Key
4. 建议合理控制请求频率，避免触发风控

## License

MIT
