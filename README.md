# Fujifilm Stock Monitor

一个轻量、无第三方 Python 依赖的 Fujifilm Mall 商品补货监控脚本。

默认监控：

```text
https://mall-jp.fujifilm.com/shop/c/c306010/
```

脚本会解析分类页里的商品卡片。当商品从“无货”变成“有货”时发送推送。

## 功能

- 不需要第三方 Python 包。
- 默认温和频率：每小时一次，外加 0-10 分钟随机延迟。
- 进程内复用 Cookie。
- 支持补货推送。
- 支持连续失败后的失效报警。
- 支持第一次补货推送后永久停止。
- 支持每款商品每个自然月最多推送一次，其他商品继续监控。
- 支持 Server 酱、ntfy.sh 和通用 JSON Webhook。
- 支持 systemd user service 后台运行。
- 自带 `fujifilmctl` 维护工具，方便查状态、看日志、测试推送和恢复某款商品。
- 安装时带交互式向导，可直接生成 `config.json`。
- 支持一个配置文件定义多个监控任务。

## 一键安装

在服务器上运行：

```bash
git clone https://github.com/ShiinaBaka/fujifilm-stock-monitor.git
cd fujifilm-stock-monitor
bash install.sh
```

编辑配置：

```bash
nano ~/.config/fujifilm-stock-monitor/env
```

如果使用 Server 酱：

```bash
SERVERCHAN_SENDKEY=YOUR_SENDKEY
```

安装脚本也会生成：

```bash
~/.config/fujifilm-stock-monitor/config.json
```

服务启动时会优先使用这个多任务配置。

启动服务：

```bash
systemctl --user start fujifilm-stock-monitor.service
```

## 测试

运行一次检查：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilm_stock_monitor.py --once --print-products --ipv4
```

查看日志：

```bash
journalctl --user -u fujifilm-stock-monitor.service -f
```

查看服务状态：

```bash
systemctl --user status fujifilm-stock-monitor.service
```

## 常见任务速查

我想确认服务是否正常：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl health
```

我想看现在监控了什么：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl config
```

我想立刻检查一次：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl check
```

我想测试 Server 酱推送：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl test-push
```

我想看本月哪些商品已经推送过：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl notified
```

我没抢到某款，想让它本月再次推送：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl restore g16587294
```

我想暂停/恢复监控：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl pause
~/.local/share/fujifilm-stock-monitor/fujifilmctl resume
```

## 维护工具

安装脚本会同时安装：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl status
```

常用命令：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl status
~/.local/share/fujifilm-stock-monitor/fujifilmctl health
~/.local/share/fujifilm-stock-monitor/fujifilmctl config
~/.local/share/fujifilm-stock-monitor/fujifilmctl check
~/.local/share/fujifilm-stock-monitor/fujifilmctl logs
~/.local/share/fujifilm-stock-monitor/fujifilmctl notified
~/.local/share/fujifilm-stock-monitor/fujifilmctl test-push
~/.local/share/fujifilm-stock-monitor/fujifilmctl pause
~/.local/share/fujifilm-stock-monitor/fujifilmctl resume
```

如果启用了“每款商品每月最多推送一次”，但某款没抢到，可以只恢复这一款本月再次推送：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl restore g16587294
```

如果想清空本月所有商品去重记录：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl clear-notified
```

## 推送渠道

### Server 酱

```bash
SERVERCHAN_SENDKEY=SCTxxxxxxxxxxxxxxxx
```

### ntfy.sh

```bash
STOCK_NTFY_TOPIC=my-private-topic-name
```

在 ntfy app 或网页端订阅这个 topic 即可。

### 通用 Webhook

```bash
STOCK_WEBHOOK_URL=https://example.com/webhook
```

请求体格式：

```json
{"text": "title\nmessage"}
```

## 默认行为

安装后的服务通过 `run.sh` 启动。如果存在 `~/.config/fujifilm-stock-monitor/config.json`，会使用多任务配置；否则使用传统环境变量模式，默认等价于：

```bash
python3 fujifilm_stock_monitor.py \
  --interval 3600 \
  --jitter 600 \
  --failure-alert-after 3 \
  --failure-alert-repeat 24 \
  --state-file ~/.config/fujifilm-stock-monitor/state.json \
  --ipv4
```

含义：

- 每 60-70 分钟检查一次。
- 连续失败 3 次后报警。
- 如果仍然失败，每多失败 24 次再报警一次。

## 手动使用

检查一次：

```bash
python3 fujifilm_stock_monitor.py --once --print-products --ipv4
```

只监控部分商品：

```bash
python3 fujifilm_stock_monitor.py --name-regex '1パック|モノクローム'
```

监控其他 Fujifilm 分类：

```bash
python3 fujifilm_stock_monitor.py \
  --url 'https://mall-jp.fujifilm.com/shop/c/c306030/' \
  --require-text 'チェキスクエア用フィルム'
```

第一次补货推送后永久停止：

```bash
python3 fujifilm_stock_monitor.py \
  --stop-marker ~/.config/fujifilm-stock-monitor/stopped.json
```

写入永久停止标记后，后续启动会直接退出，不再请求商店页面。

每款商品每个自然月最多推送一次：

```bash
python3 fujifilm_stock_monitor.py \
  --monthly-marker-dir ~/.config/fujifilm-stock-monitor/monthly
```

脚本会写入 `YYYY-MM.done`，里面记录本月已经推送过的商品 URL。已推送的那款本月不再重复推送，其他商品仍然继续监控。

## 多任务配置

可以在一个 `config.json` 里定义多个监控任务：

```json
{
  "notifications": {
    "serverchan_sendkey": "SCTxxxxxxxxxxxxxxxx"
  },
  "defaults": {
    "interval": 3600,
    "jitter": 600,
    "failure_alert_after": 3,
    "failure_alert_repeat": 24,
    "ipv4": true
  },
  "tasks": [
    {
      "name": "mini 相纸",
      "url": "https://mall-jp.fujifilm.com/shop/c/c306010/",
      "require_text": "チェキ用フィルム",
      "state_file": "state/mini.json",
      "monthly_marker_dir": "monthly/mini"
    },
    {
      "name": "WIDE 400",
      "url": "https://mall-jp.fujifilm.com/shop/c/cinswide/",
      "require_text": "“チェキ” instax WIDE 400",
      "state_file": "state/wide.json",
      "stop_marker": "stopped/wide.json"
    }
  ]
}
```

手动运行多任务配置：

```bash
python3 fujifilm_stock_monitor.py --config ~/.config/fujifilm-stock-monitor/config.json --once --print-products
```

仓库里也提供了 `config.example.json`。

## 自动测试

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 商品解析。
- 每款商品每月只推一次。
- 其他商品继续监控。
- 推送失败时不写去重标记。
- 多任务配置一次性检查。

## 没抢到时恢复

如果监控已经推送过，但你没抢到，可以恢复：

```bash
~/.local/share/fujifilm-stock-monitor/resume.sh
```

这个命令会：

- 备份当前状态和停止标记。
- 删除当前停止标记。
- 重置已记住的库存状态。
- 重启服务并立刻检查一次。

如果只是某一款商品没抢到，更推荐用：

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl restore g16587294
```

## 卸载

```bash
bash uninstall.sh
```

卸载脚本会保留 `~/.config/fujifilm-stock-monitor/`，避免误删状态和通知密钥。

## 注意

请负责任地使用。保持温和检查频率，避免并发请求，不要给网站制造负担。

## License

MIT
