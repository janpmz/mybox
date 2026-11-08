.PHONY: build test service-test service-reboot-test integration backend-integration resources-test ports-test network-test protected-test interface-test reboot-prepare reboot-test dns-test clean desktop desktop-run desktop-test
.DEFAULT_GOAL := build

desktop:
	$(MAKE) -C desktop build

desktop-run:
	$(MAKE) -C desktop run

desktop-test:
	$(MAKE) -C desktop test

COMMON = Shared/RuntimeLocation.swift Sources/Support.swift Sources/HostProcess.swift Sources/NetworkPolicy.swift Sources/ServiceFirewall.swift
CLI = $(COMMON) Shared/BoxList.swift Sources/Box.swift Sources/BoxAccess.swift Sources/Resources.swift Sources/Completion.swift Sources/Network.swift Sources/FirewallClient.swift Sources/PortState.swift Sources/Ports.swift Sources/Deletion.swift
FIREWALL = $(COMMON) Sources/Firewall.swift Sources/FirewallRuntime.swift Sources/FirewallWatcher.swift Sources/FirewallManagerMain.swift Sources/ServiceApproval.swift
FORWARDER = Sources/Support.swift Sources/PortState.swift Sources/GuestPortRelay.swift Sources/PortForwarder.swift
SERVICE_COMMON = Shared/RuntimeLocation.swift Sources/Support.swift service/ServiceSupport.swift
SERVICE = $(SERVICE_COMMON) service/Transfer.swift service/Broker.swift service/BrokerMain.swift
ACCESS = $(SERVICE_COMMON) service/Access.swift service/AccessMain.swift
SETUP = $(SERVICE_COMMON) service/Install.swift service/PackageInstall.swift service/InstallMain.swift

build: .build/mybox .build/mybox-worker .build/mybox-firewall-manager .build/mybox-port-forwarder .build/mybox-authenticate .build/mybox-service .build/mybox-access .build/mybox-setup

.build/mybox-service: $(SERVICE) Makefile
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $(SERVICE) -o $@

.build/mybox-access: $(ACCESS) Makefile
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $(ACCESS) -o $@

.build/mybox-setup: $(SETUP) Makefile
	mkdir -p .build/module-cache
	xcrun swiftc -O -module-cache-path .build/module-cache $(SETUP) -o $@

.build/mybox-service-tests: $(SERVICE_COMMON) service/Transfer.swift service/Broker.swift service/Access.swift service/Install.swift service/PackageInstall.swift Sources/BoxAccess.swift tests/ServiceTestsMain.swift
	mkdir -p .build/module-cache
	xcrun swiftc -module-cache-path .build/module-cache $^ -o $@

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

test: build .build/mybox-test .build/mybox-firewall-test .build/mybox-service-tests
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

.PHONY: package package-test
package:
	./packaging/build-package.sh

package-test:
	python3 tests/package_layout.py dist/MyBox-0.1.0-arm64.pkg
