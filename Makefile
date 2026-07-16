PREFIX  ?= $(HOME)/.local
BINDIR  ?= $(PREFIX)/bin
LIBDIR  ?= $(PREFIX)/share/cage

VERSION  := $(shell grep '^CAGE_VERSION=' cage | cut -d'"' -f2)
REGISTRY := ghcr.io/sindycate/cage

.PHONY: install uninstall build rebuild pull version

install:
	CAGE_INSTALL_DIR="$(LIBDIR)" CAGE_BIN_DIR="$(BINDIR)" ./install.sh --from-source

uninstall:
	CAGE_INSTALL_DIR="$(LIBDIR)" CAGE_BIN_DIR="$(BINDIR)" ./install.sh --uninstall

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
