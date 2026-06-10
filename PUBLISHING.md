# Publishing Checklist

1. Repository:

```text
https://github.com/ShiinaBaka/fujifilm-stock-monitor
```

2. Push this directory:

```bash
cd fujifilm-stock-monitor
git init
git add .
git commit -m "Initial release"
git branch -M main
git remote add origin https://github.com/ShiinaBaka/fujifilm-stock-monitor.git
git push -u origin main
```

3. Recommended server install command:

```bash
git clone https://github.com/ShiinaBaka/fujifilm-stock-monitor.git
cd fujifilm-stock-monitor
bash install.sh
nano ~/.config/fujifilm-stock-monitor/env
systemctl --user start fujifilm-stock-monitor.service
```

Do not commit real notification keys. Keep them only in:

```text
~/.config/fujifilm-stock-monitor/env
```
