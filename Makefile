.PHONY: help install-k8sgpt install-k8sgpt-dry-run

help:
	@echo "Available targets:"
	@echo "  install-k8sgpt          Install k8sgpt and add to PATH (Linux/macOS/Windows)"
	@echo "  install-k8sgpt-dry-run  Dry-run installer (no changes)"

install-k8sgpt:
	@echo "[make] Installing k8sgpt..."
	@if command -v pwsh >/dev/null 2>&1; then \
		pwsh -NoProfile -File scripts/install-k8sgpt.ps1 ; \
	else \
		bash scripts/install-k8sgpt.sh ; \
	fi

install-k8sgpt-dry-run:
	@echo "[make] Dry-run k8sgpt installer..."
	@if command -v pwsh >/dev/null 2>&1; then \
		DRY_RUN=1 pwsh -NoProfile -File scripts/install-k8sgpt.ps1 ; \
	else \
		DRY_RUN=1 bash scripts/install-k8sgpt.sh ; \
	fi
