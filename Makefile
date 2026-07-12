.PHONY: test check package

test:
	python3 -m unittest discover -s tests -v
	sh tests/test_install.sh
	sh tests/test_bootstrap.sh

check: test
	python3 -m py_compile src/remora.py
	sh -n bin/remora bootstrap.sh install.sh uninstall.sh scripts/package-release.sh
	REMORA_CONFIG="$(CURDIR)/config.example.toml" ./bin/remora agents >/dev/null
	REMORA_CONFIG="$(CURDIR)/config.example.toml" ./bin/remora dry-run >/dev/null

package: check
	./scripts/package-release.sh
