##  Installation
```bash
sudo mkdir -p /usr/local/bin
sudo cp scripts/test-monitor.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/test_monitor.sh

sudo cp systemd/test-monitor.service /etc/systemd/system/
sudo cp systemd/test-monitor.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now test-monitor.timer


```
