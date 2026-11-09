.PHONY: build run preview test clean

APP = .build/MyBox.app
SHARED = ../Shared/BoxList.swift
SOURCES = $(wildcard Sources/*.swift) $(SHARED)
SWIFTC = xcrun swiftc
FLAGS = -swift-version 6 -target arm64-apple-macosx14.0 -module-cache-path .build/module-cache

build: .build/app.stamp

.build/app.stamp: $(SOURCES) Resources/Info.plist scripts/MakeIcon.swift Makefile
	mkdir -p .build/module-cache $(APP)/Contents/MacOS $(APP)/Contents/Resources
	$(SWIFTC) $(FLAGS) -O $(SOURCES) -o $(APP)/Contents/MacOS/MyBox
	cp Resources/Info.plist $(APP)/Contents/Info.plist
	rm -rf $(APP)/Contents/Resources/Blueprint
	printf '%s\n' "$$(cd .. && pwd -P)/mybox" > $(APP)/Contents/Resources/cli-location.txt
	$(SWIFTC) $(FLAGS) scripts/MakeIcon.swift -o .build/make-icon
	.build/make-icon .build/AppIcon.iconset
	/usr/bin/iconutil -c icns .build/AppIcon.iconset -o $(APP)/Contents/Resources/AppIcon.icns
	/usr/bin/codesign --force --sign - --identifier io.github.janpmz.mybox.desktop $(APP)
	touch $@

run: build
	open $(APP)

preview: build
	open -n $(APP) --args --demo

test:
	mkdir -p .build/module-cache
	$(SWIFTC) $(FLAGS) $(SHARED) Sources/Models.swift Sources/Commands.swift Sources/ResourceCapacity.swift Sources/MyBoxClient.swift Tests/DesktopTests.swift -o .build/desktop-tests
	.build/desktop-tests

clean:
	rm -rf .build
