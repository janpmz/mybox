.PHONY: build test integration network-test protected-test interface-test reboot-prepare reboot-test dns-test clean

COMMON = Sources/Support.swift Sources/HostProcess.swift Sources/NetworkPolicy.swift
CLI = $(COMMON) Sources/Box.swift Sources/Completion.swift Sources/Network.swift Sources/FirewallClient.swift
FIREWALL = $(COMMON) Sources/Firewall.swift Sources/FirewallRuntime.swift Sources/FirewallWatcher.swift Sources/FirewallManagerMain.swift

build: .build/mybox .build/mybox-firewall-manager

.build/mybox: $(CLI)
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $(CLI) -o .build/mybox

.build/mybox-firewall-manager: $(FIREWALL)
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $(FIREWALL) -o .build/mybox-firewall-manager

.build/mybox-test: $(CLI)
	mkdir -p .build/module-cache
	xcrun swiftc -D MYBOX_TESTING -module-cache-path .build/module-cache $(CLI) -o .build/mybox-test

.build/mybox-firewall-test: $(FIREWALL)
	mkdir -p .build/module-cache
	xcrun swiftc -D MYBOX_TESTING -module-cache-path .build/module-cache $(FIREWALL) -o .build/mybox-firewall-test

test: build .build/mybox-test .build/mybox-firewall-test
	python3 -m unittest discover -s tests -p 'test_*.py' -v

integration: build
	python3 tests/integration.py

network-test:
	python3 tests/network_isolation.py

protected-test: build
	python3 tests/protected_network.py

interface-test: build
	python3 tests/interface_changes.py

reboot-prepare: build
	python3 tests/reboot.py prepare

reboot-test:
	python3 tests/reboot.py verify

dns-test:
	python3 tests/dns.py

clean:
	rm -rf .build
