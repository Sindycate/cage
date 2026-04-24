PREFIX  ?= $(HOME)/.local
BINDIR  ?= $(PREFIX)/bin
LIBDIR  ?= $(PREFIX)/share/cage

VERSION  := $(shell grep '^CAGE_VERSION=' cage | cut -d'"' -f2)
REGISTRY := ghcr.io/sindycate/cage

DIST_FILES = cage cage-setup.sh cage-profiles.sh cage-netgate.sh netgate-proxy.py mcp-bridge.py mcp-relay host-cmd-bridge.py host-cmd-relay docker-compose.yml \
             Dockerfile Dockerfile.codex \
             entrypoint.sh entrypoint-codex.sh

.PHONY: install uninstall build rebuild pull version

install:
	mkdir -p $(LIBDIR) $(BINDIR)
	cp $(DIST_FILES) $(LIBDIR)/
	cp -r netgate $(LIBDIR)/
	chmod +x $(LIBDIR)/cage $(LIBDIR)/cage-setup.sh $(LIBDIR)/cage-profiles.sh $(LIBDIR)/cage-netgate.sh $(LIBDIR)/netgate-proxy.py $(LIBDIR)/mcp-bridge.py $(LIBDIR)/mcp-relay $(LIBDIR)/host-cmd-bridge.py $(LIBDIR)/host-cmd-relay
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

pull:
	docker pull $(REGISTRY)/claude-code:$(VERSION)
	docker tag $(REGISTRY)/claude-code:$(VERSION) claude-code:$(VERSION)
	docker pull $(REGISTRY)/codex:$(VERSION)
	docker tag $(REGISTRY)/codex:$(VERSION) codex:$(VERSION)

version:
	@echo $(VERSION)
