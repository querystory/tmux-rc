# The native shell (docs/design/native-app.md). The Xcode and Gradle projects are generated,
# never committed: `cap add` writes them from the Capacitor templates package-lock.json pins,
# and overlay/ holds the few files that are ours. TMUXRC_APP_ID sets the bundle id.
.PHONY: ios android sim apk archive

node_modules: package-lock.json
	npm ci --ignore-scripts
	touch $@

ADD = rm -rf $@ && npx cap add $@

android: node_modules
	$(ADD)

# The entitlement is what turns the Push Notifications capability on when Xcode signs; the
# Info.plist key answers App Store Connect's export question (HTTPS only) for every build.
ios: node_modules
	$(ADD)
	cp -R overlay/ios/. ios/
	perl -pi -e 's|^(\s*)(CODE_SIGN_STYLE = )|$$1CODE_SIGN_ENTITLEMENTS = App/App.entitlements;\n$$1$$2|' ios/App/App.xcodeproj/project.pbxproj
	perl -0pi -e 's|<dict>|<dict>\n\t<key>ITSAppUsesNonExemptEncryption</key>\n\t<false/>|' ios/App/App/Info.plist
	grep -c CODE_SIGN_ENTITLEMENTS ios/App/App.xcodeproj/project.pbxproj | grep -qx 2
	grep -q ITSAppUsesNonExemptEncryption ios/App/App/Info.plist

XCODEBUILD = xcodebuild -project ios/App/App.xcodeproj -scheme App

# Unsigned, for the simulator: proves the project and our Swift compile.
sim: ios
	$(XCODEBUILD) -configuration Debug -sdk iphonesimulator -derivedDataPath build/ios CODE_SIGNING_ALLOWED=NO build

apk: android
	cd android && ./gradlew --no-daemon assembleDebug

# Signed by Xcode's automatic signing with an App Store Connect API key, then uploaded
# (export.plist's destination). BUILD is the build number, which each upload must raise.
ifneq ($(filter archive,$(MAKECMDGOALS)),)
$(foreach v,TMUXRC_APP_ID APPLE_TEAM_ID ASC_KEY_ID ASC_ISSUER_ID ASC_KEY_P8 BUILD,$(if $($v),,$(error $v is not set)))
endif
ASC = -allowProvisioningUpdates -authenticationKeyPath $(ASC_KEY_PATH) -authenticationKeyID $(ASC_KEY_ID) -authenticationKeyIssuerID $(ASC_ISSUER_ID)
archive: ios
	$(XCODEBUILD) -configuration Release -destination generic/platform=iOS -archivePath build/App.xcarchive \
		DEVELOPMENT_TEAM=$(APPLE_TEAM_ID) CURRENT_PROJECT_VERSION=$(BUILD) $(ASC) archive
	xcodebuild -exportArchive -archivePath build/App.xcarchive -exportOptionsPlist export.plist -exportPath build/export $(ASC)
