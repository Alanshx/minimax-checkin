# MiniMax 每日签到

电脑不用开机，云端每天帮你签到，签到时刻在**北京时间 08:00–08:30 之间随机**。

## 两种部署方式

| 方式 | 适合 | 说明 |
|---|---|---|
| **平台定时任务**（推荐） | 大多数人 | 零依赖、无需 GitHub 仓库，平台直接跑 |
| **GitHub Actions** | 想在 Actions 页面看运行记录 | 需要仓库 + 一个 PAT |

两种方式都遵守同一条铁律（见下方「关键前提」）。

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

> 之前的设计（凭据存 GitHub Secret + Actions 缓存双份）会因为两份凭据互相消耗
> refresh_token 而失效。现在改成**单点持有**：谁签到谁独占，续期后原地写回。

---

## 方式一：平台定时任务（推荐）

### 步骤

1. 跑一次登录，拿到凭据：

   ```bash
   python3 minimax_checkin.py login
   python3 minimax_checkin.py export --raw > credentials.json
   ```

2. 把 `credentials.json` 存进平台的加密 secret 存储，命名为 `MINIMAX_CREDENTIALS`。
3. 建一个定时任务，每天 07:52（北京时间）执行，任务内容：

   ```bash
   mkdir -p ~/.minimax/checkin
   # 首次从 secret 播种；之后复用本地文件（本地那份才是活的）
   [ -f ~/.minimax/checkin/credentials.json ] || printf '%s' "$MINIMAX_CREDENTIALS" > ~/.minimax/checkin/credentials.json
   chmod 600 ~/.minimax/checkin/credentials.json

   python3 minimax_checkin.py checkin --window 08:00-08:30 --wait --catch-up --json

   # 续期后凭据会变，必须写回 secret
   # （用 secret 工具 update，值用单引号包裹）
   ```

4. 再建一个兜底任务，每天 08:40 执行同样的命令但**去掉 `--wait`**：
   万一主任务挂了，窗口过了还能补签（宁可晚签也别漏签 400 分）。

### 签到时刻怎么随机的

脚本第一次在当天运行时，会在 `08:00-08:30` 之间随机选一个分钟写进
`~/.minimax/checkin/state.json`，然后 `--wait` 睡到那一刻才真正签到。
同一天再跑多少次都读同一个值，所以**一天只签一次**。

主任务 07:52 醒 → 随机选中 08:0x 到 08:3x 之间的某一分钟 → 睡到点 → 签到。

---

## 方式二：GitHub Actions

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
4. 可选：再建一个 Secret `MINIMAX_NOTIFY_URL` 填推送地址
5. 打开 Actions 页签，手动触发一次 `checkin.yml` 验证

### 缓存这一段是干什么的

和上面的铁律同源：OAuth 续期会轮换 refresh_token，旧的那个立刻作废。
只存在 Secret 里不更新，第二次运行就会拿一个已作废的 refresh_token 去换令牌，
直接 `invalid_grant` 死掉。

所以 workflow 用 `actions/cache` 做**滚动缓存**：
每次运行前恢复上次的凭据，运行后把续期后的新凭据存回去。
用默认 `GITHUB_TOKEN` 就行，不需要额外的 PAT。

> ⚠️ **注意**：GitHub 的 `schedule`（cron）触发器在免费个人账号上**实测不触发**，
> 即使把仓库设为 public 也一样（最小对照 workflow 同样零触发）。
> 所以实际靠外部调度器调 `workflow_dispatch`，仓库里保留的 cron 只是额外保险。

---

## 调整

| 想改 | 改哪 |
|---|---|
| 签到时间 | `--window` 参数，默认 `08:00-08:30`（**随机**，不是固定点） |
| 区域 | `--region`（`cn` / `io` / `com`） |
| 时区 | `--timezone`（IANA 名字，如 `Asia/Shanghai`） |
| 推送 | `--notify` 或环境变量 `MINIMAX_NOTIFY_URL` |
| GitHub 方式的时间窗 | 仓库 Variable `SIGNIN_WINDOW` |

## 免责

非 MiniMax 官方工具，仅供个人学习自用。是否违反服务条款由使用者自行承担。
