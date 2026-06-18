# Fujifilm Stock Monitor

A small, dependency-free Python monitor for Fujifilm Mall category stock.

Default target:

```text
https://mall-jp.fujifilm.com/shop/c/c306010/
```

It watches the product cards on the page. If a product changes from `sold out` to `in stock`, it sends a notification.

## Features

- No third-party Python packages.
- Gentle default schedule: once per hour plus 0-10 minutes random jitter.
- Reuses cookies during the process lifetime.
- Sends restock notifications.
- Sends failure notifications after repeated failures.
- Can stop permanently after the first restock notification.
- Can send at most one restock notification per product per calendar month.
- Supports ServerChan, ntfy.sh, and generic JSON webhooks.
- Runs as a systemd user service.

## One-Command Install

On the server:

```bash
git clone https://github.com/ShiinaBaka/fujifilm-stock-monitor.git
cd fujifilm-stock-monitor
bash install.sh
```

Then edit the config:

```bash
nano ~/.config/fujifilm-stock-monitor/env
```

For ServerChan, set:

```bash
SERVERCHAN_SENDKEY=YOUR_SENDKEY
```

Start it:

```bash
systemctl --user start fujifilm-stock-monitor.service
```

## Test

Run one check:

```bash
~/.local/share/fujifilm-stock-monitor/fujifilm_stock_monitor.py --once --print-products --ipv4
```

Watch logs:

```bash
journalctl --user -u fujifilm-stock-monitor.service -f
```

Check service status:

```bash
systemctl --user status fujifilm-stock-monitor.service
```

## Maintenance Helper

The install script also installs a small helper:

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl status
```

Useful commands:

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl health
~/.local/share/fujifilm-stock-monitor/fujifilmctl check
~/.local/share/fujifilm-stock-monitor/fujifilmctl logs
~/.local/share/fujifilm-stock-monitor/fujifilmctl test-push
~/.local/share/fujifilm-stock-monitor/fujifilmctl pause
~/.local/share/fujifilm-stock-monitor/fujifilmctl resume
```

If monthly per-product notification is enabled and you missed a restock for one product, allow that product to notify again:

```bash
~/.local/share/fujifilm-stock-monitor/fujifilmctl restore g16587294
```

## Notification Options

### ServerChan

```bash
SERVERCHAN_SENDKEY=SCTxxxxxxxxxxxxxxxx
```

### ntfy.sh

```bash
STOCK_NTFY_TOPIC=my-private-topic-name
```

Subscribe to that topic in the ntfy app or web UI.

### Generic Webhook

```bash
STOCK_WEBHOOK_URL=https://example.com/webhook
```

The payload is:

```json
{"text": "title\nmessage"}
```

## Defaults

The installed service runs through `run.sh` with defaults equivalent to:

```bash
python3 fujifilm_stock_monitor.py \
  --interval 3600 \
  --jitter 600 \
  --failure-alert-after 3 \
  --failure-alert-repeat 24 \
  --state-file ~/.config/fujifilm-stock-monitor/state.json \
  --ipv4
```

Meaning:

- Check every 60-70 minutes.
- Notify after 3 consecutive failures.
- If still failing, notify again every 24 additional failures.

## Manual Usage

Check once:

```bash
python3 fujifilm_stock_monitor.py --once --print-products --ipv4
```

Monitor a subset of products:

```bash
python3 fujifilm_stock_monitor.py --name-regex '1パック|モノクローム'
```

Use another Fujifilm category:

```bash
python3 fujifilm_stock_monitor.py \
  --url 'https://mall-jp.fujifilm.com/shop/c/c306030/' \
  --require-text 'チェキスクエア用フィルム'
```

Stop permanently after the first restock notification:

```bash
python3 fujifilm_stock_monitor.py \
  --stop-marker ~/.config/fujifilm-stock-monitor/stopped.json
```

After a restock notification, later starts will exit without requesting the store page while that marker exists.

Send at most one restock notification per product per calendar month:

```bash
python3 fujifilm_stock_monitor.py \
  --monthly-marker-dir ~/.config/fujifilm-stock-monitor/monthly
```

The monitor writes a `YYYY-MM.done` marker with the product URLs already notified that month. Other products continue to be monitored and can still send notifications.

```ini
[Timer]
OnCalendar=*-*-01 00:00:00 Asia/Tokyo
Persistent=true
```

## Resume After Missing the Restock

If the monitor stopped after notifying but you did not manage to buy the item:

```bash
~/.local/share/fujifilm-stock-monitor/resume.sh
```

This command:

- Backs up the current state and stop marker.
- Removes the active stop marker.
- Resets the remembered stock state.
- Restarts the service and performs an immediate check.

Resetting the state is important. Removing only the marker may not produce another notification while the item remains continuously in stock.

## Uninstall

```bash
bash uninstall.sh
```

The uninstall script keeps `~/.config/fujifilm-stock-monitor/` so your state and notification keys are not removed accidentally.

## Notes

Use this responsibly. Keep the interval gentle, avoid parallel checks, and do not use it to create load on the site.

## License

MIT
