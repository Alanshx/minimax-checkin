# MiniMax 每日签到

电脑不用开机，云端每天帮你签到，签到时刻在**北京时间 08:00–08:30 之间随机**。

## 当前架构

```
平台定时任务 07:52  ──dispatch──▶  GitHub Actions (checkin.yml)
                                        │
                                        ├─ 从仓库 Secret 读凭据（唯一真源）
                                        ├─ 签到 + 续期
                                        └─ 续期后原地写回 Secret
```

| 角色 | 职责 | 碰凭据吗 |
|---|---|---|
| **GitHub Actions** | 唯一执行签到、唯一续期、唯一写回 | ✅ 唯一持有者 |
| 平台定时任务 | 07:52 准时 dispatch、08:13 查状态汇报、08:40 失败补发 | ❌ 完全不碰 |
| 仓库 Secret `MINIMAX_CREDENTIALS` | 凭据唯一真源 | — |
| 仓库 Secret `PAT_TOKEN` | 让工作流有权限把新凭据写回 Secret | — |

> 平台侧**绝对不碰** MiniMax 凭据。原因见下面的铁律 —— 多留一份副本就会互相消耗
> refresh_token，这正是之前每天 `invalid_grant` 的根因。

---

## 关键前提：refresh_token 是一次性的

OAuth 续期时 **refresh_token 会轮换，旧的那个立刻作废**。

MiniMax 服务端的原话是：

```
this refresh token can no longer be used, start a new authorization
```

推论有三条，**照做就不会断链**：

1. **一份活凭据只能存在于「刚用过它的那台机器」上。**
   把凭据复制到第二个地方去用，第一个地方立刻就废了。
2. **凭据必须原地轮换、立刻写回。**
   续期后不写回，下次运行就是 `invalid_grant`。
3. **access_token 有效期约 1 小时。**
   每天签到时几乎必然触发一次续期，所以第 2 条是日常必做动作。

### 踩过的坑（2026-10-24 修复）

之前的实现同时存在三处破口，结果是连着两天 `invalid_grant`：

| 破口 | 后果 | 现在怎么修 |
|---|---|---|
| 平台沙箱存一份凭据，GitHub 缓存又存一份 | 两份互相消耗 refresh_token，谁先续期谁就把对方作废 | 凭据只在仓库 Secret 里，平台不碰 |
| `保存状态` 步骤带 `if: always()` | 失败时把**已失效**的凭据又存回缓存，下次运行继续捞它 → 自我强化的死循环 | 缓存里**只存 state.json**，不存凭据 |
| 恢复缓存优先于 Secret | Secret 里明明有新的，缓存里那份旧的反而被优先使用 | 每次运行**无条件**用 Secret 覆盖本地文件 |

修完之后的关键动作只有一个：**续期后原地写回 Secret**。

---

## 方式一：GitHub Actions（当前在用）

### 一键安装

```bash
gh auth login          # 首次，登录 GitHub
./bootstrap.sh         # 建仓库、推代码、设 Secret 全自动
```

`bootstrap.sh` 会引导你完成 OAuth 授权（**手机浏览器也行**，不需要电脑开着），
然后把凭据存成仓库 Secret `MINIMAX_CREDENTIALS`。

### 手动安装

1. 新建一个仓库，把本目录内容推上去
2. 跑一次 `python3 minimax_checkin.py login`（或用手机完成授权）
3. 把 `python3 minimax_checkin.py export` 的输出存成仓库 Secret：
   `Settings → Secrets and variables → Actions → New repository secret`
   - Name: `MINIMAX_CREDENTIALS`
   - Secret: 整段 JSON
4. 再建一个 Secret `PAT_TOKEN`，内容是一个**只对本仓库有 `Secrets: Read and write`
   权限**的 PAT（`GITHUB_` 开头的名字是 GitHub 保留的，不能用）
5. 可选：再建一个 Secret `MINIMAX_NOTIFY_URL` 填推送地址
6. 打开 Actions 页签，手动触发一次 `checkin.yml` 验证

### 凭据是怎么流动的

```
Secret MINIMAX_CREDENTIALS
   ↓ 初始化凭据（每次运行都无条件覆盖本地文件）
~/.minimax/checkin/credentials.json
   ↓ 签到；access_token 过期就自动续期，refresh_token 随之轮换并落盘
~/.minimax/checkin/credentials.json（已是新一代）
   ↓ 写回凭据（gh secret set，原样写回）
Secret MINIMAX_CREDENTIALS
```

### 缓存这一段现在是干什么的

**只缓存 `state.json`**（今天随机选中的那个分钟），**不缓存任何凭据**。

`state.json` 存在缓存里，是为了让同一天的多次运行复用同一个随机时刻，
从而做到「一天只签一次」。凭据一旦进缓存，就会出现第二份 refresh_token 副本 ——
这是必须避开的坑。

### 关于 GitHub 的 cron 触发

> ⚠️ GitHub 的 `schedule`（cron）触发器**是会触发的**，但经常大幅延迟。
> 实测 08:07 的 cron 拖到了 **12:19** 才跑（延迟 4 小时 12 分），
> 08:37 的那条拖到 12:56（延迟 4 小时 19 分）。
>
> 所以：**准时的触发靠外部调度器在 07:52 调 `workflow_dispatch`**，
> 仓库里这两条 cron 只是兜底 —— 万一外部调度器挂了，延迟到窗口之后的 cron 跑起来时，
> `--catch-up` 会立刻补签（宁可晚签也别漏签）。

---

## 方式二：平台定时任务（只发令版）

如果你用平台定时任务，**它只负责发令，不负责签到**。

```bash
# 07:52 主任务：触发 GitHub 工作流
curl -sS -X POST -H "Authorization: Bearer $GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<你的仓库>/actions/workflows/checkin.yml/dispatches \
  -d '{"ref":"main"}'

# 08:13 复查：读 run 状态并汇报
# 08:40 兜底：今天还没有成功 run 就再 dispatch 一次
```

平台侧**不要**跑 `minimax_checkin.py checkin`，也不要往平台 secret 里存 MiniMax 凭据 ——
那会变成第二份持有者。

### 签到时刻怎么随机的

脚本第一次在当天运行时，会在 `08:00-08:30` 之间随机选一个分钟写进
`state.json`，然后 `--wait` 睡到那一刻才真正签到。
同一天再跑多少次都读同一个值，所以**一天只签一次**。

主任务 07:52 醒 → 随机选中 08:0x 到 08:3x 之间的某一分钟 → 睡到点 → 签到。

---

## 调整

| 想改 | 改哪 |
|---|---|
| 签到时间 | `--window` 参数，默认 `08:00-08:30`（**随机**，不是固定点） |
| 区域 | `--region`（`cn` / `io` / `com`） |
| 时区 | `--timezone`（IANA 名字，如 `Asia/Shanghai`） |
| 推送 | `--notify` 或环境变量 `MINIMAX_NOTIFY_URL` |
| GitHub 方式的时间窗 | 仓库 Variable `SIGNIN_WINDOW` |
| 调度时间 | 平台任务改 cron，或改 workflow 里的 `schedule:` |

## 免责

本项目仅供个人学习与自动化使用，请遵守 MiniMax 的服务条款，不要高频调用。
