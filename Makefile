PREFIX ?= $(HOME)/.local
BINDIR = $(PREFIX)/bin
APPDIR = $(PREFIX)/share/applications
DESKTOP = io.github.stickup.desktop

.PHONY: install uninstall test

install:
	install -d $(BINDIR) $(APPDIR)
	ln -sfn $(CURDIR)/bin/stickup $(BINDIR)/stickup
	sed 's|@BINDIR@|$(BINDIR)|' data/$(DESKTOP).in > $(APPDIR)/$(DESKTOP)
	update-desktop-database $(APPDIR)

uninstall:
	rm -f $(BINDIR)/stickup $(APPDIR)/$(DESKTOP)
	update-desktop-database $(APPDIR)

test:
	python3 -m unittest discover -s tests
