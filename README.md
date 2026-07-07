# GLM Coding Plan 自动抢购

每天 10:00 自动抢购智谱 GLM Coding Plan (含 personal / team 等所有 plan 类型)。
GUI 界面,cookie 登录,到支付页自动停下并提醒。

> ⚠️ 个人使用,自动化可能违反智谱 ToS,建议用备用账号。
> 详细风险见末尾 [风险提示](#风险提示)。

---

## 安装

```bash
pip install -r requirements.txt
```

需要系统装有 **Edge** 或 Chrome。脚本默认指向:
`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`

如果路径不同,改 `buyer.py` 里的 `set_browser_path()`。

## 启动

```bash
python app.py
```

## 第一次使用

### 1. 导出 cookie

在 Edge / Chrome 登录 [bigmodel.cn](https://www.bigmodel.cn),按 F12 打开 DevTools:

**Cookies (document.cookie 部分)**:
```
> document.cookie
"acw_tc=...; SERVERID=...; ..."
```
手动解析成 `[{name, value, path}]` 列表。

**localStorage 部分**:
```
> JSON.stringify(localStorage)
{"token":"...", "userId":"...", ...}
```

把两部分合并成 `cookie.json`:
```json
{
  "domain": "bigmodel.cn",
  "cookies": [
    {"name": "acw_tc", "value": "0a1234...", "path": "/"},
    {"name": "SERVERID", "value": "abc123", "path": "/"}
  ],
  "localStorage": {
    "token": "eyJhbGc...",
    "userId": "8a7b...",
    "user-token": "...",
    "current_idp": "sso",
    "current_type": "phone"
  }
}
```

> 💡 简化做法:把 DevTools 里 `Application → Cookies` 全选复制,
> 加上 `localStorage` 的 JSON dump,合并就行。脚本的"从剪贴板导入"按钮
> 接受这个 JSON 格式。

### 2. 在 GUI 里

1. 点 **"选择…"** 或 **"从剪贴板导入"** 加载 cookie
2. 点 **"✓ 验证"** 确认 cookie 能解析
3. 选 **计费周期**:
   - **连续包月** (推荐·灵活) — 随时退订,无锁定
   - **连续包季** (9折) — 锁定 3 个月
   - **连续包年** (8折) — 锁定 12 个月,最省
4. 点套餐卡片 (Lite / Pro / Max / 标准版 / 高级版) 选要抢的 tier,被勾选的卡片会显示 ✓
5. 留着窗口,不动 — 9:59 脚本自动唤醒 + 10:00 点击

也可以点 **"⚡ 立即抢购"** 手动触发 (测试 cookie / 流程用)。

> 💡 想临时关掉定时?改 `config.json` 里 `"schedule_enabled": false`,
> 重启 GUI 生效。

## 抢票时机策略

库存固定 10:00:00 上线,瞬间秒没。脚本使用 **9:59 提前唤醒** 策略:

```
09:59:00  调度线程唤醒 + 启动 Edge + 打开 plan 页 + 注入 cookie
09:59:30  页面加载完成,按钮仍是 disabled "暂时售罄"
09:59:30 → 10:00:00
          每 50ms 轮询按钮状态 (≈20 次/秒)
10:00:00.100 (实测)  按钮瞬间从 disabled 变 enabled "立即订阅"
                     立即 .click() — 不用任何延迟
10:00:00.XYZ  跳转到支付页 → 弹窗 + 声音 + 系统通知
```

**为什么不 10:00 整点才启动?**
启动 Edge + 注入 cookie + 打开 plan 页 ≈ 2-5s。10:00 才开始 → 按到按钮最早 10:00:05,
那时库存大概率没了。

**为什么不在 9:59 立刻点?**
9:59 时按钮还是 disabled "暂时售罄",点也是无效请求(可能反过来引起风控)。
最佳是等按钮由 disable → enable 的瞬间点,延迟 < 100ms。

## 工作流程

```
注入 cookie → 打开 plan 页 → 找"开通"按钮 → 点击
   ↓
到支付页 → 打开浏览器 + 弹窗 + 声音 + 系统通知
   ↓
用户扫码支付 → 点"我已支付,完成" → 结束
```

**自动化停在支付页**,绝不自动扣款 — 钱的事用户自己来。

## 文件

| 文件 | 作用 |
|---|---|
| `app.py` | GUI 入口 + 调度线程 (9:59 唤醒逻辑) |
| `buyer.py` | 浏览器自动化 + 按钮启用轮询 |
| `config.json` | 自动生成,保存 cookie 路径 + 选中的 tier |
| `cookie.json` | 用户凭证,手动放进去或从剪贴板导入 |
| `buyer.log` | 运行日志 |

## 拟人化处理 (降低被风控概率)

- 关闭 `navigator.webdriver` 指纹
- 随机视口尺寸 (1920×1080 / 1536×864 / ...)
- 页面打开后随机停留 1–3 秒 + 模拟滚动
- 点击前随机抖动 50–200ms
- 点击后停顿 300–800ms,模拟读取
- **按钮禁用检测**:9:59 提前进入页面,等 enabled 瞬间立刻点 — 没有机械的整点延迟

## 风险提示

智谱 ToS 大概率禁止自动化。检测机制:
- **IP 频率**:同 IP 短时间多次请求 → 验证码墙
- **设备指纹**:`navigator.webdriver` 等机器人标记
- **行为特征**:点击时间太精准 / 无滚动 / 无停顿
- **登录异常**:多设备 / 异地

实际触发顺序:验证码墙 → 限流 → 账号警告 → 封号(罕见)。

缓解:用**备用账号**测试,cookie 过期前及时续期,定时只在 10 点触发,
不要全天 polling。

## License

MIT (仅供学习)。