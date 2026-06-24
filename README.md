# astrbot_plugin_kai_call

Kai 语音通话插件 —— 让 AI 能主动给用户"打电话"，实现实时语音对话。

## 功能

- AI 通过 Function Tool 主动发起通话，向 QQ 发送通话链接
- 用户点击链接后进入通话页面，通过麦克风实时语音交流
- 支持 STT（语音转文字）→ LLM → TTS（文字转语音）全链路
- 通话结束后自动生成摘要发回聊天
- 来电铃声 + 呼吸动画界面

## 工作流程

```
AI 决定打电话 → 生成通话链接发到QQ → 用户点击接听
→ 浏览器录音 → STT转文字 → LLM生成回复 → TTS合成语音 → 播放
→ 挂断 → 生成摘要发回QQ
```

## 安装

1. 将本插件放入 AstrBot 插件目录
2. Docker 部署需要映射通话端口（默认 8899）
3. 在 AstrBot 管理面板配置公网地址和端口

## 配置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `server_host` | 公网IP或域名，用于生成通话链接 | `""` (必填) |
| `call_port` | 通话HTTP服务端口 | `8899` |
| `call_timeout` | 等待接听超时（秒） | `120` |
| `context_rounds` | 注入的QQ近期对话轮数 | `10` |
| `summary_provider_id` | 摘要使用的LLM提供商（留空用默认） | `""` |

## 依赖

- AstrBot >= 4.14
- NapCat（QQ机器人框架）
- MiniMax API（用于 TTS 合成）
- aiohttp

## Docker 端口映射

```yaml
ports:
  - "8899:8899"
```

确保 `server_host` 填写你的公网IP或域名，用户需要能从外网访问通话页面。

## TTS 配置

通话语音合成使用 MiniMax TTS API，需要在 AstrBot 中配置 MiniMax 作为 provider 并填入 API Key。

## License

MIT
