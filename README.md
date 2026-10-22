# MiniMax 每日签到（GitHub Actions）

电脑不用开机，GitHub 的 runner 每天帮你签到。

## 一键安装

```bash
gh auth login          # 首次，登录 GitHub
./bootstrap.sh         # 建仓库、推代码、设 Secret 全自动
```

`bootstrap.sh` 会引导你完成 OAuth 授权（**手机浏览器也行**，不需要电脑开着），
然后把凭据存成仓库 Secret `MINIMAX_CREDENTIALS`。

## 手动安装

1. 新建一个仓库，把本目录内容推上去
2. 跑一次 `python3 minimax_checkin.py login`（或用手机完成授权）
3. 把 `python3 minimax_checkin.py export` 的输出存成仓库 Secret：
   `Settings → Secrets and variables → Actions → New repository secret`
   - Name: `MINIMAX_CREDENTIALS`
   - Secret: 整段 JSON
4. 可选：再建一个 Secret `MINIMAX_NOTIFY_URL` 填推送地址
5. 打开 Actions 页签，手动触发一次 `checkin.yml` 验证

## 为什么需要「缓存」这一段

OAuth 续期时 **refresh_token 会轮换**，旧的那个立刻作废。
如果凭据只存在 Secret 里不更新，第二次运行就会拿一个已作废的
refresh_token 去换令牌，直接 `invalid_grant` 死掉。

所以 workflow 用 `actions/cache` 做**滚动缓存**：
每次运行前恢复上次的凭据，运行后把续期后的新凭据存回去。
用默认 `GITHUB_TOKEN` 就行，不需要额外的 PAT。

## 签到时间是随机的

默认在**北京时间 08:00–08:30 之间随机挑一个时刻**签到，不是每天固定同一个点。

实现方式：cron 每 5 分钟醒一次（UTC 00:00–00:30 = 北京 08:00–08:30），
脚本第一次醒来时随机选一个时刻写进 `state.json`，之后每次运行都读同一个值，
到点才签、签过就跳过。同一天无论跑几次都只签一次。

想换区间：改仓库 Variable `SIGNIN_WINDOW`（比如 `07:30-08:00`）。
注意 **cron 是 UTC**，换时区要同步改 `.github/workflows/checkin.yml` 里的 cron。

## 调整

| 想改 | 改哪 |
|---|---|
| 签到时间 | 仓库 Variable `SIGNIN_WINDOW`，默认 `08:00-08:30`（**随机**，不是固定点） |
| 区域 | 仓库 Variable `MINIMAX_REGION`（`cn` / `io` / `com`） |
| 时区 | 仓库 Variable `MINIMAX_TIMEZONE`（IANA 名字，如 `Asia/Shanghai`） |
| 推送 | 仓库 Secret `MINIMAX_NOTIFY_URL` |

## 免责

非 MiniMax 官方工具，仅供个人学习自用。是否违反服务条款由使用者自行承担。
