.PHONY: build test service-test service-reboot-test integration backend-integration resources-test ports-test network-test protected-test interface-test reboot-prepare reboot-test dns-test clean

COMMON = Sources/Support.swift Sources/HostProcess.swift Sources/NetworkPolicy.swift Sources/ServiceFirewall.swift
CLI = $(COMMON) Sources/Box.swift Sources/Resources.swift Sources/Completion.swift Sources/Network.swift Sources/FirewallClient.swift Sources/PortState.swift Sources/Ports.swift
FIREWALL = $(COMMON) Sources/Firewall.swift Sources/FirewallRuntime.swift Sources/FirewallWatcher.swift Sources/FirewallManagerMain.swift Sources/ServiceApproval.swift
FORWARDER = Sources/Support.swift Sources/PortState.swift Sources/GuestPortRelay.swift Sources/PortForwarder.swift

build: .build/mybox .build/mybox-worker .build/mybox-firewall-manager .build/mybox-port-forwarder .build/mybox-authenticate

.build/mybox-authenticate: Sources/Authenticate.swift
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $< -o $@
	/usr/bin/codesign --force --sign - --options runtime --identifier io.github.janpmz.my-container-box.authenticate $@

.build/mybox-port-forwarder: $(FORWARDER)
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $(FORWARDER) -o .build/mybox-port-forwarder

.build/mybox-worker: $(CLI)
	mkdir -p .build/module-cache
	xcrun swiftc -O -D MYBOX_SERVICE_WORKER -module-cache-path .build/module-cache $(CLI) -o $@

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

service-test integration: build
	python3 tests/private_service_live.py

service-reboot-test: build
	python3 tests/private_service_live.py --after-reboot

backend-integration: build .build/mybox-test
	python3 tests/integration.py

resources-test: build .build/mybox-test
	python3 tests/resources_integration.py

ports-test: build .build/mybox-test
	python3 tests/ports_integration.py

network-test:
	python3 tests/network_isolation.py

protected-test: build .build/mybox-test
	python3 tests/protected_network.py

interface-test: build .build/mybox-test
	python3 tests/interface_changes.py

reboot-prepare: build .build/mybox-test
	python3 tests/reboot.py prepare

reboot-test: build .build/mybox-test
	python3 tests/reboot.py verify

dns-test:
	python3 tests/dns.py

clean:
	rm -rf .build
