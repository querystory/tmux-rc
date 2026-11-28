// Adds what `cap add ios` cannot to the generated Xcode project: the notification service
// extension (overlay/ios/App/Notify) as a second target embedded in the app, and the app's
// push entitlement. Run by `make ios`.
const fs = require("node:fs"), xcode = require("xcode");
const { appId } = require("./capacitor.config.js");
const file = "ios/App/App.xcodeproj/project.pbxproj", project = xcode.project(file).parseSync();

const objects = project.hash.project.objects, mine = (settings) => settings?.PRODUCT_NAME === '"Notify"';
// The template has no target dependencies yet, and addTarget only fills sections that exist.
objects.PBXTargetDependency ||= {};
objects.PBXContainerItemProxy ||= {};
const target = project.addTarget("Notify", "app_extension", "Notify", `${appId}.notify`);
project.addBuildPhase(["NotificationService.swift"], "PBXSourcesBuildPhase", "Sources", target.uuid);
const group = project.addPbxGroup(["NotificationService.swift"], "Notify", "Notify");
project.addToPbxGroup(group.uuid, project.getFirstProject().firstProject.mainGroup);

const configs = Object.values(objects.XCBuildConfiguration).map((config) => config.buildSettings);
const app = {}; // the app's own settings, which the extension must match to be embedded
for (const settings of configs.filter((settings) => settings?.PRODUCT_BUNDLE_IDENTIFIER && !mine(settings))) {
  settings.CODE_SIGN_ENTITLEMENTS = "App/App.entitlements";
  for (const key of ["CODE_SIGN_STYLE", "CURRENT_PROJECT_VERSION", "IPHONEOS_DEPLOYMENT_TARGET", "MARKETING_VERSION", "SWIFT_VERSION", "TARGETED_DEVICE_FAMILY"]) app[key] = settings[key];
}
for (const settings of configs.filter(mine)) {
  Object.assign(settings, app, { GENERATE_INFOPLIST_FILE: "YES", INFOPLIST_FILE: "Notify/Info.plist", INFOPLIST_KEY_CFBundleDisplayName: '"tmux-rc"' });
}
fs.writeFileSync(file, project.writeSync());
