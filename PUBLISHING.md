# Publishing Checklist

1. Create a GitHub repository, for example:

```text
fujifilm-stock-monitor
```

2. Push this directory:

```bash
cd fujifilm-stock-monitor
git init
git add .
git commit -m "Initial release"
git branch -M main
git remote add origin git@github.com:YOUR_NAME/fujifilm-stock-monitor.git
git push -u origin main
```

3. Replace `YOUR_NAME` in the README one-command install snippet with your GitHub username.

4. Recommended server install command after publishing:

```bash
git clone https://github.com/YOUR_NAME/fujifilm-stock-monitor.git
cd fujifilm-stock-monitor
bash install.sh
nano ~/.config/fujifilm-stock-monitor/env
systemctl --user start fujifilm-stock-monitor.service
```

Do not commit real notification keys. Keep them only in:

```text
~/.config/fujifilm-stock-monitor/env
```
