PREFIX  ?= $(HOME)/.local
BINDIR  ?= $(PREFIX)/bin
LIBDIR  ?= $(PREFIX)/share/cage

VERSION := $(shell grep '^CAGE_VERSION=' cage | cut -d'"' -f2)

DIST_FILES = cage netgate-proxy.py docker-compose.yml \
             Dockerfile Dockerfile.codex \
             entrypoint.sh entrypoint-codex.sh

.PHONY: install uninstall build rebuild version

install:
	mkdir -p $(LIBDIR) $(BINDIR)
	cp $(DIST_FILES) $(LIBDIR)/
	cp -r netgate $(LIBDIR)/
	chmod +x $(LIBDIR)/cage $(LIBDIR)/netgate-proxy.py
	ln -sf $(LIBDIR)/cage $(BINDIR)/cage
	mkdir -p $(HOME)/.config/cage
	@echo "Installed cage $(VERSION) to $(BINDIR)/cage"
	@echo "Make sure $(BINDIR) is in your PATH."

uninstall:
	rm -f $(BINDIR)/cage
	rm -rf $(LIBDIR)
	@echo "Uninstalled cage. Config at ~/.config/cage/ preserved."

build:
	docker compose build

rebuild:
	docker compose build --no-cache

version:
	@echo $(VERSION)
