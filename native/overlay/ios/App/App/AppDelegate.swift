import Capacitor
import UIKit
import UserNotifications
import WebKit

// Replaces the Capacitor template's AppDelegate (docs/design/native-app.md). Everything the
// web app cannot do lives here: the device token, and a question answered from its
// notification while no page is running to answer it.

// Which of Apple's push hosts this build's token belongs to; the daemon's push to the other
// is refused and the token dropped. The signature decides it, not the build configuration: a
// build from Xcode carries its provisioning profile, and a TestFlight or App Store build,
// which is always production, carries none.
private let apnsEnvironment: String = {
    #if targetEnvironment(simulator)
    return "development"
    #else
    let profile = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision").flatMap { try? String(contentsOf: $0, encoding: .isoLatin1) }
    let sandbox = profile?.range(of: #"aps-environment</key>\s*<string>development"#, options: .regularExpression)
    return sandbox == nil ? "production" : "development"
    #endif
}()
private let daemonKey = "daemon" // the page that registered for push: where answers are sent

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // The delegate has to be in place before launch finishes: iOS starts the app in the
        // background to deliver an action, with no scene, so no webview and no Capacitor.
        // The actions themselves are registered per push, by the Notify extension.
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession, options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }

    private var shell: CAPBridgeViewController? {
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return windows.compactMap { $0.rootViewController as? CAPBridgeViewController }.first
    }

    // The push plugin hands this string to the page, which posts it to the daemon (web/push.js).
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        UserDefaults.standard.set(shell?.webView?.url, forKey: daemonKey)
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        NotificationCenter.default.post(name: .capacitorDidRegisterForRemoteNotifications, object: "\(apnsEnvironment):\(token)")
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationCenter.default.post(name: .capacitorDidFailToRegisterForRemoteNotifications, object: error)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound]) // another pane's question shows while the app is open
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        let action = response.actionIdentifier.split(separator: ":")
        guard action.count == 2, action[0] == "answer", let index = Int(action[1]), let nonce = info["nonce"] as? String,
              let url = UserDefaults.standard.url(forKey: daemonKey).flatMap({ URL(string: "/api/push/answer", relativeTo: $0) }) else {
            // A tap on the notification itself: the page follows its link (web/push.js). Handed
            // straight to the push plugin, because Capacitor's router would drop the tap on
            // the "Answer not sent" notice below, which no push delivered.
            let shell = shell
            shell?.loadViewIfNeeded()
            shell?.bridge?.notificationRouter.pushNotificationHandler?.didReceive(response: response)
            return completionHandler()
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["nonce": nonce, "option_index": index])
        // The login in front of the daemon is a cookie the webview holds. No Origin is sent:
        // the daemon admits a write without one, and could never list an app's origin.
        WKWebsiteDataStore.default().httpCookieStore.getAllCookies { cookies in
            let configuration = URLSessionConfiguration.ephemeral
            cookies.forEach { configuration.httpCookieStorage?.setCookie($0) }
            URLSession(configuration: configuration).dataTask(with: request) { _, reply, _ in
                // An expired login redirects to its sign-in page, which answers 200 from elsewhere.
                if (reply as? HTTPURLResponse)?.statusCode != 200 || reply?.url?.absoluteString != url.absoluteString {
                    let content = UNMutableNotificationContent()
                    content.title = "Answer not sent"
                    content.body = "Tap to open the session and answer there."
                    content.userInfo = info
                    center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
                }
                completionHandler()
            }.resume()
        }
    }
}
